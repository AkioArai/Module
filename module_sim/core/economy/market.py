"""Продажа энергии: спот, договор PPA, расчёты (DESIGN.md, §3.2, §3.3).

Фаза 2б заменила фиксированную цену рыночной, и вместе с ценой сюда пришла
ловушка, ради которой фаза вообще выделена в отдельную. Цена меняется каждый
час. Если считать её честно по часам, догон превращается в потиковый цикл, а
инвариант И4 (не меньше 200 000 тиков/с) — в разговоры. Если считать её раз в
блок, потиковый прогон и батчевый разойдутся, потому что блоки у них разные, и
рухнет И4а.

Выход — разделить цену по природе её частей (economy/prices.py):

* **случайное** (шум цены, отклонение спроса) меняется **раз в игровые сутки**,
  дискретным событием ``market.settlement``. Случайность обязана разыгрываться
  в обоих путях в одних и тех же точках, а такая точка в игре ровно одна —
  событие планировщика;
* **почасовой рельеф** остаётся детерминированной функцией тика и берётся
  замкнутым интегралом по префикс-суммам, за O(1) на блок любой длины.

Оттого блок в сутки и блок в час дают арифметически одно и то же, а цена при
этом честно ходит по часам.

**Где здесь деньги.** Начисленное копится дробными копейками
(``*_accrued_cents``) и превращается в целые только в событии расчёта, раз в
игровые сутки. Правило то же, что и в фазе 1: непрерывное начисление округлялось
бы в разных точках у батчевого и потикового пути, и касса разошлась бы, а она
обязана совпадать точно (И4а, уровень 2). И платится не «выручка за период», а
разница между причитающимся и уже выплаченным — такой расчёт
самокорректирующийся, ошибка округления не копится по периодам.

Чего здесь ещё нет: рынка на сутки вперёд с заявками и балансирующего рынка
(DESIGN.md, §3.2). Они требуют от игрока прогноза собственной выработки, а
предсказывать пока нечего — выработка детерминирована. Их время придёт вместе с
физикой и отказами оборудования, в фазе 5.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from module_sim.core.economy import apply_cash, mean_reverting_step
from module_sim.core.economy.prices import (
    HOURS_PER_DAY,
    HourTable,
    day_table,
    shortfall_table,
    spot_price_cents,
)
from module_sim.core.events import registry
from module_sim.core.events.scheduler import ScheduledEvent
from module_sim.core.physics.reactor import NOMINAL_ELECTRIC_MW, Ramp, plan
from module_sim.core.state import MarketState, PpaContract

if TYPE_CHECKING:  # pragma: no cover - подсистемы не тянут sim во время работы
    from module_sim.core.sim import Simulation

__all__ = [
    "K_PPA_PENALTY",
    "PPA_FIXED_PENALTY_CENTS",
    "SETTLEMENT_INTERVAL_HOURS",
    "SETTLEMENT_KIND",
    "accrue",
    "amount_due_cents",
    "current_price_cents",
    "settle",
]

SETTLEMENT_KIND = "market.settlement"

#: Расчёт и переигровка случайной части — раз в игровые сутки. Сутки выбраны не
#: из удобства: это период суточного профиля, и на границе суток таблица цен
#: всё равно сменилась бы.
SETTLEMENT_INTERVAL_HOURS = 24

#: Потоки случайности (И2). Имена постоянны: переименование потока сдвинуло бы
#: последовательность и изменило бы будущее всех сохранённых партий.
NOISE_STREAM = "market.noise"
DEMAND_STREAM = "market.demand"

#: Возврат к среднему и разброс, за один шаг = за игровые сутки (BALANCE.md, §3).
NOISE_THETA_PER_DAY = 0.25
NOISE_SIGMA = 0.12
DEMAND_THETA_PER_DAY = 0.20
DEMAND_SIGMA = 0.08

#: Множитель штрафа за недопоставку по договору (BALANCE.md, §4).
K_PPA_PENALTY = 1.3

#: Фиксированная часть штрафа — за сутки, в которых был срыв поставки.
PPA_FIXED_PENALTY_CENTS = 50_000_000

#: Недопоставка меньше этого за сутки — не срыв поставки, а погрешность.
#:
#: Порог нужен по существу, а не для красоты: «был ли срыв» — это ветвление по
#: непрерывной величине, а она в батчевом и потиковом пути отличается в
#: последних разрядах (И4а, уровень 3). Сравнение с нулём означало бы, что два
#: пути изредка расходятся на целый фиксированный штраф. Мегаватт-час за сутки
#: при обязательстве в сотни — это заведомо ниже любой осмысленной недопоставки
#: и заведомо выше любой погрешности.
SHORTFALL_TOLERANCE_MWH = 1.0

#: Ключ производного кэша в ``Simulation.derived``.
DAY_CACHE_KEY = "market.day"


# -- цена ------------------------------------------------------------------


def current_price_cents(simulation: Simulation) -> float:
    """Цена спота в текущий час, копеек за МВт·ч. Для интерфейса и консоли."""
    market = simulation.state.market
    return spot_price_cents(simulation.state.tick, market.noise, market.demand_deviation)


def _tables(
    simulation: Simulation, day: int, contract: PpaContract | None
) -> tuple[HourTable, HourTable | None]:
    """Таблицы цен на игровые сутки: спот и цена замещения недопоставки.

    Кэш живёт в ``Simulation.derived`` — он производный, в сейв не уходит и
    после загрузки собирается заново. Ключ включает всё, от чего таблица
    зависит, включая шум: подставить шум руками может тест или консоль, и
    молча отдать им таблицу от прошлых суток было бы хуже, чем пересобрать её.
    """
    market = simulation.state.market
    price = contract.price_cents if contract is not None else None
    key = (day, price, market.noise, market.demand_deviation)

    cached = simulation.derived.get(DAY_CACHE_KEY)
    if cached is not None and cached[0] == key:
        return cached[1], cached[2]

    prices = day_table(day, market.noise, market.demand_deviation)
    replacement = shortfall_table(prices, price) if price is not None else None
    simulation.derived[DAY_CACHE_KEY] = (key, prices, replacement)
    return prices, replacement


# -- начисление ------------------------------------------------------------


def _split_points(start: int, end: int, ramp: Ramp, volume_mwh: float) -> list[float]:
    """Точки, в которых блок обязан быть разрезан.

    Их ровно три вида, и каждый — место, где какая-то из функций под интегралом
    перестаёт быть той, какой была:

    * **граница суток** — сменилась таблица цен;
    * **излом мощности** — уставка достигнута, наклон кончился;
    * **пересечение с объёмом договора** — мощность сравнялась с обязательством,
      и ``min(выработка, объём)`` сменил ветку.

    Внутри куска цена ступенчата, а мощность линейна — то, что интегрируется
    замкнуто. Излом и пересечение почти всегда дробные, границы суток целые.

    Обычно точек нет вовсе: расчёт стоит на каждой границе суток, поэтому блок
    и так короче суток. Разбиение по суткам оставлено на случай, когда события
    расчёта в очереди почему-то нет — тогда ответ обязан быть верным, пусть и
    не за одну итерацию.
    """
    points = [float(start), float(end)]

    first_boundary = (start // HOURS_PER_DAY + 1) * HOURS_PER_DAY
    points.extend(float(tick) for tick in range(first_boundary, end, HOURS_PER_DAY))

    span = float(end - start)
    if 0.0 < ramp.hours < span:
        points.append(start + ramp.hours)

    if ramp.rate != 0.0 and volume_mwh > 0.0:
        crossing = (volume_mwh / NOMINAL_ELECTRIC_MW - ramp.start) / ramp.rate
        if 0.0 < crossing < min(span, ramp.hours):
            points.append(start + crossing)

    points.sort()
    return points


def accrue(simulation: Simulation, hours: int) -> None:
    """Начислить выручку и штрафы за блок ``[tick, tick + hours)``.

    Единственное место, где рынок трогает непрерывные величины. Вызывается из
    ``Simulation._integrate`` **до** хода реактора: нужна мощность на начало
    блока и траектория, по которой она пойдёт, а не та, что уже получилась.
    """
    if hours <= 0:
        return

    state = simulation.state
    market = state.market
    ramp = plan(state.reactor)
    contract = market.ppa
    volume = contract.volume_mwh if contract is not None else 0.0

    # Остановленный блок без договора не зарабатывает и не должен: интегралы
    # от нуля дали бы ровно ноль, и пропуск ничего не меняет — кроме скорости
    # догона простаивающей партии, а простой в этой игре долгий.
    if volume == 0.0 and ramp.start == 0.0 and ramp.rate == 0.0:
        return

    start = state.tick
    points = _split_points(start, start + hours, ramp, volume)
    price_cents = contract.price_cents if contract is not None else 0

    for index in range(len(points) - 1):
        left = points[index]
        right = points[index + 1]
        if right <= left:
            continue

        day = int(left) // HOURS_PER_DAY
        prices, replacement = _tables(simulation, day, contract)

        # Локальные координаты: цена — в часах от начала суток, мощность — в
        # часах от начала блока. Смещение постоянно, поэтому наклон один и тот
        # же в обеих системах.
        day_start = day * HOURS_PER_DAY
        u0 = left - day_start
        u1 = right - day_start

        # Все ветвления на куске решаются по его **середине**, а не по краю.
        # Это не перестраховка: точки излома и пересечения уже вырезаны, значит
        # внутри куска ветка не меняется, — а вот на краю сравнение врёт.
        # Излом на 5.5000000000000004 часа, сложенный с тиком 5, даёт ровно
        # 5.5: край куска оказывается на волос **раньше** излома, и участок
        # после него досчитывается как наклонный. Середина от такого промаха
        # отстоит на полчаса.
        middle = 0.5 * (left + right) - start
        elapsed = left - start

        if middle >= ramp.hours:
            power, slope = ramp.target * NOMINAL_ELECTRIC_MW, 0.0
        else:
            power = ramp.level_at(elapsed) * NOMINAL_ELECTRIC_MW
            slope = ramp.rate * NOMINAL_ELECTRIC_MW

        if power + slope * (middle - elapsed) >= volume:
            delivered, delivered_slope = volume, 0.0
        else:
            delivered, delivered_slope = power, slope

        market.spot_accrued_cents += prices.integrate(
            u0, u1, power - delivered, slope - delivered_slope
        )

        if contract is None:
            continue

        span = u1 - u0
        delivered_mwh = delivered * span + delivered_slope * span * span * 0.5
        market.ppa_accrued_cents += delivered_mwh * price_cents

        shortfall_mwh = volume * span - delivered_mwh
        if shortfall_mwh > 0.0 and replacement is not None:
            market.ppa_shortfall_mwh += shortfall_mwh
            market.penalty_accrued_cents += K_PPA_PENALTY * replacement.integrate(
                u0, u1, volume - delivered, -delivered_slope
            )


# -- расчёт ----------------------------------------------------------------


def amount_due_cents(market: MarketState) -> int:
    """Сколько причитается за всю партию, копеек.

    Каждая копилка округляется отдельно: игроку показывают выручку спота,
    выручку договора и штрафы по отдельности, и сумма показанного обязана
    сходиться с тем, что пришло в кассу.
    """
    return (
        round(market.spot_accrued_cents)
        + round(market.ppa_accrued_cents)
        - round(market.penalty_accrued_cents)
    )


def _charge_missed_delivery(market: MarketState) -> None:
    """Фиксированная часть штрафа — раз в сутки, в которых был срыв поставки."""
    missed = market.ppa_shortfall_mwh - market.ppa_shortfall_settled_mwh
    market.ppa_shortfall_settled_mwh = market.ppa_shortfall_mwh
    if missed > SHORTFALL_TOLERANCE_MWH:
        market.penalty_accrued_cents += PPA_FIXED_PENALTY_CENTS


def settle(simulation: Simulation) -> int:
    """Выплатить разницу между причитающимся и уже выплаченным. Вернуть её."""
    market = simulation.state.market
    _charge_missed_delivery(market)

    due = amount_due_cents(market)
    payment = due - market.revenue_paid_cents
    market.revenue_paid_cents = due
    apply_cash(simulation, payment)
    return payment


def roll_day(simulation: Simulation) -> None:
    """Разыграть случайную часть цены на новые сутки.

    Оба процесса — возврат к среднему (economy/__init__.py). Без возврата цена
    ушла бы в бесконечность на длинном догоне: за сто лет отсутствия случайное
    блуждание уходит куда угодно, а рынок обязан остаться рынком.
    """
    market = simulation.state.market
    market.noise = mean_reverting_step(
        market.noise,
        NOISE_THETA_PER_DAY,
        NOISE_SIGMA,
        simulation.rng.stream(NOISE_STREAM).normal(),
    )
    market.demand_deviation = mean_reverting_step(
        market.demand_deviation,
        DEMAND_THETA_PER_DAY,
        DEMAND_SIGMA,
        simulation.rng.stream(DEMAND_STREAM).normal(),
    )


@registry.handler(SETTLEMENT_KIND)
def _on_settlement(simulation: Simulation, event: ScheduledEvent) -> None:
    settle(simulation)
    roll_day(simulation)
    simulation.schedule_in(SETTLEMENT_INTERVAL_HOURS, SETTLEMENT_KIND)
