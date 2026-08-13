"""Реактор: мощность, набор и сброс, выгорание топлива (DESIGN.md, §4).

Фаза 1 — скелет. Здесь нет ни кинетики, ни ксенона, ни температурных
коэффициентов: мощность просто идёт к уставке с постоянной скоростью. Смысл
модуля сейчас в другом — он вводит **первую непрерывную величину состояния** и
тем самым впервые по-настоящему нагружает контракт И4а.

Главное требование к любой формуле здесь: она обязана считаться **замкнуто на
интервал**, а не циклом по часам (И4). Поэтому и набор мощности, и выработанная
за интервал энергия выводятся аналитически, включая случай, когда уставка
достигается где-то в середине интервала.
"""

from __future__ import annotations

from dataclasses import dataclass

from module_sim.core.state import ReactorState

__all__ = [
    "CAMPAIGN_HOURS",
    "EFFICIENCY_NOMINAL",
    "NOMINAL_ELECTRIC_MW",
    "RAMP_RATE_PER_HOUR",
    "Ramp",
    "advance",
    "electric_mw",
    "plan",
]

#: BALANCE.md, §2.1.
NOMINAL_ELECTRIC_MW = 1000.0
EFFICIENCY_NOMINAL = 0.333

#: Скорость набора и сброса мощности, доля номинала в час (BALANCE.md, §2.1).
#: Десять процентов в час — блок идёт с нуля на номинал за десять часов. Это
#: осознанно медленно: мгновенная мощность обесценила бы ксеноновую яму, ради
#: которой физика в игре вообще есть (§4.3).
RAMP_RATE_PER_HOUR = 0.10

#: Длительность топливной кампании в часах (BALANCE.md, §2.5: 18 месяцев).
CAMPAIGN_HOURS = 18 * 30 * 24


def electric_mw(state: ReactorState) -> float:
    """Текущая электрическая мощность, МВт."""
    return state.power_level * NOMINAL_ELECTRIC_MW


@dataclass(frozen=True, slots=True)
class Ramp:
    """Траектория мощности от текущего момента: наклон и излом.

    Отдельным объектом — потому что траекторию читает не только реактор. Рынок
    интегрирует по ней ``цена · мощность`` (economy/market.py), и считать
    мощность **дважды двумя формулами** нельзя: разойдутся отпущенная энергия и
    оплаченная, причём тихо и на копейки.

    Время ``u`` отсчитывается от начала блока, в часах, и может быть дробным:
    излом почти никогда не попадает на границу часа.
    """

    start: float
    target: float
    #: Доля номинала в час, со знаком. Ноль — уставка уже достигнута.
    rate: float
    #: Сколько часов длится наклон. Ноль — наклона нет.
    hours: float

    def level_at(self, u: float) -> float:
        """Уровень мощности через ``u`` часов от начала блока, доля номинала."""
        if u >= self.hours:
            return self.target
        return self.start + self.rate * u


def plan(state: ReactorState) -> Ramp:
    """Куда пойдёт мощность, если ничего не менять. Состояние не трогает."""
    start = state.power_level
    target = state.power_setpoint
    delta = target - start
    if delta == 0.0:
        return Ramp(start=start, target=target, rate=0.0, hours=0.0)
    rate = RAMP_RATE_PER_HOUR if delta > 0 else -RAMP_RATE_PER_HOUR
    return Ramp(start=start, target=target, rate=rate, hours=abs(delta) / RAMP_RATE_PER_HOUR)


def advance(state: ReactorState, hours: int) -> float:
    """Проэволюционировать реактор на ``hours`` часов. Вернуть выработку, МВт·ч.

    Замкнутая формула на интервал, включая излом: если уставка достигается
    внутри интервала, площадь под графиком мощности считается двумя участками —
    наклонным и горизонтальным. Наивное ``level * hours`` дало бы расхождение
    между потиковым и батчевым путём, которое допуск И4а уже не покрыл бы.
    """
    if hours <= 0:
        return 0.0

    ramp = plan(state)
    knee = min(ramp.hours, hours)
    end = ramp.level_at(hours)
    # Трапеция до излома плюс прямоугольник после него. Когда излома внутри
    # интервала нет, одно из слагаемых обращается в ноль само.
    area = (ramp.start + ramp.level_at(knee)) * 0.5 * knee + ramp.target * (hours - knee)

    state.power_level = end

    produced_mwh = area * NOMINAL_ELECTRIC_MW
    thermal_mwh = produced_mwh / EFFICIENCY_NOMINAL
    # Выгорание не имеет обрыва на единице: в фазе 1 топливо не кончается
    # внезапно, оно просто выработано. Момент исчерпания станет событием
    # планировщика в фазе 5, когда появится реактивность и её обратные связи —
    # тогда же понадобится решать уравнение на время пересечения.
    state.burnup += thermal_mwh / (NOMINAL_ELECTRIC_MW / EFFICIENCY_NOMINAL * CAMPAIGN_HOURS)
    return produced_mwh
