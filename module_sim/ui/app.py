"""Приложение Textual. Фаза 0: панель времени, пустое тело, автосохранение.

Инвариант И3: **UI перерисовывается независимо от тиков.** Кадры идут с
постоянной частотой; сколько тиков просимулировать между кадрами, считается из
прошедшего реального времени и множителя скорости. Частота кадров на симуляцию
не влияет никогда — иначе игра на медленной машине шла бы медленнее не только
на вид.

Инвариант И7: блокирующих ожиданий нет. Здесь это видно в буквальном смысле —
ни одного ``sleep`` и ни одной модалки.

Инвариант И1 соблюдается направлением зависимостей: этот модуль знает про
``core``, ``core`` про него — нет.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Footer, Static

from module_sim.core import orders
from module_sim.core.economy import contracts, finance, fuel, market, prices
from module_sim.core.physics import reactor
from module_sim.core.sim import Simulation
from module_sim.core.state import SPEED_MULTIPLIER, Speed
from module_sim.persistence import save as save_mod
from module_sim.ui.widgets.station_panel import StationPanel
from module_sim.ui.widgets.time_panel import TimePanel

__all__ = ["ModuleApp"]

#: Частота кадров UI. К симуляции отношения не имеет (И3).
FRAME_INTERVAL = 1.0 / 20.0

#: Максимум реального времени, засчитываемого за один кадр (BALANCE.md, §1).
#:
#: Кадр может прийти сильно позже назначенного: своп, приостановка процесса,
#: тяжёлый соседний процесс. Без потолка накопленный интервал превратился бы в
#: сотни тиков, посчитанных синхронно в цикле событий, — интерфейс замер бы,
#: нарушив И7. Цена потолка: во время затыка игровое время отстаёт от
#: реального. Это честнее зависшего окна, и на догон при следующем запуске не
#: влияет — тот считается от ``saved_at``, а не от числа тиков.
MAX_FRAME_ELAPSED_S = 1.0

#: Сколько тиков ещё разумно посчитать потиково прямо в кадре. Больше —
#: батчевым путём: он даёт тот же результат в пределах допуска И4а, но не
#: линейно по числу часов.
MAX_TICKS_PER_FRAME = 16

#: Автосохранение раз в игровые сутки, но не чаще раза в 5 реальных секунд
#: (BALANCE.md, §1).
AUTOSAVE_EVERY_TICKS = 24
AUTOSAVE_MIN_INTERVAL_S = 5.0


class ModuleApp(App):
    """Оболочка игры."""

    CSS_PATH = Path(__file__).with_name("theme.tcss")
    TITLE = "Module"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("space", "toggle_pause", "Пауза"),
        Binding("1", "speed('x1')", "1x"),
        Binding("2", "speed('x5')", "5x"),
        Binding("3", "speed('x50')", "50x"),
        Binding("up", "power(0.1)", "Мощность +"),
        Binding("down", "power(-0.1)", "Мощность −"),
        Binding("r", "order('refuel')", "Перегрузка"),
        Binding("m", "order('maintenance')", "ТО"),
        Binding("s", "save_now", "Сохранить"),
        Binding("d", "toggle_dark", "Тема"),
        Binding("q", "quit", "Выход"),
    ]

    def __init__(
        self,
        simulation: Simulation,
        *,
        created_at: float,
        notice: str = "",
        save_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.simulation = simulation
        self.created_at = created_at
        self.notice_text = notice
        self.save_path = save_path
        self._frame_at = 0.0
        #: Дробный остаток тиков между кадрами. Без него на 1x при 20 кадрах в
        #: секунду не набиралось бы ни одного целого тика и время стояло бы.
        self._tick_debt = 0.0
        self._last_autosave_tick = simulation.state.tick
        self._last_autosave_at = 0.0
        #: Ссылка на панель времени вместо поиска по дереву на каждом кадре.
        #: Не только ради скорости: таймер кадра может сработать в момент, когда
        #: виджеты уже сняты (выход, смена экрана), и поиск упал бы NoMatches.
        self._panel: TimePanel | None = None
        self._station: StationPanel | None = None

    # -- разметка --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield TimePanel(id="time-panel")
        with Container(id="body"):
            yield StationPanel(id="station")
        yield Static(self.notice_text, id="notice")
        yield Footer()

    def on_mount(self) -> None:
        self._panel = self.query_one(TimePanel)
        self._station = self.query_one(StationPanel)
        self._frame_at = time.monotonic()
        self._last_autosave_at = self._frame_at
        self.set_interval(FRAME_INTERVAL, self._on_frame)
        self._refresh_panel()

    # -- петля -----------------------------------------------------------

    def _on_frame(self) -> None:
        now = time.monotonic()
        elapsed = min(now - self._frame_at, MAX_FRAME_ELAPSED_S)
        self._frame_at = now

        multiplier = SPEED_MULTIPLIER[self.simulation.state.speed]
        if multiplier > 0.0:
            self._tick_debt += elapsed * multiplier
            whole = int(self._tick_debt)
            if whole:
                self._tick_debt -= whole
                self._simulate(whole)

        self._refresh_panel()
        self._maybe_autosave(now)

    def _simulate(self, ticks: int) -> None:
        """Посчитать ``ticks`` часов, не подвешивая кадр.

        Короткий интервал идёт потиково — так игрок видит каждый час, если
        смотрит на медленной скорости. Длинный уходит в батч: результат тот же
        в пределах допуска И4а, а время работы не растёт линейно по часам.
        """
        if ticks <= MAX_TICKS_PER_FRAME:
            self.simulation.run(ticks)
        else:
            self.simulation.catch_up(ticks)

    def _refresh_panel(self) -> None:
        panel = self._panel
        if panel is None:
            return
        state = self.simulation.state
        panel.company = state.company.name
        panel.game_date = self.simulation.clock.format_datetime()
        panel.speed = state.speed
        panel.tick = state.tick

        station = self._station
        if station is None:
            return
        station.power_mw = reactor.electric_mw(state.reactor)
        station.setpoint = state.reactor.power_setpoint
        station.level = state.reactor.power_level
        station.burnup = state.reactor.burnup
        station.cash_cents = state.company.cash_cents
        station.energy_mwh = state.market.energy_sold_mwh
        station.price_cents = market.current_price_cents(self.simulation)
        station.price_note = self._price_note()
        station.contract_line = self._contract_line()
        station.fuel_line = self._fuel_line()
        station.debt_cents = state.finance.debt_cents
        station.equity_cents = finance.equity_cents(self.simulation)
        station.rate = finance.annual_rate(self.simulation)
        station.status = state.finance.status
        station.status_note = self._status_note()
        station.orders_line = self._orders_line()

    def _price_note(self) -> str:
        """Полоса цены на сегодня: где мы внутри суточного профиля.

        Одна цена не говорит ничего — важно, дорогой сейчас час или дешёвый.
        Это же и предиктор штрафа по договору: недопоставка в дорогой час стоит
        дороже, и видеть это игрок обязан **до** события (И8).
        """
        state = self.simulation.state
        table = prices.day_table(
            prices.day_index(state.tick), state.market.noise, state.market.demand_deviation
        )
        low = min(table.values) / 100
        high = max(table.values) / 100
        return f"   (сутки {low:,.0f}…{high:,.0f})".replace(",", " ")

    def _contract_line(self) -> str:
        contract = self.simulation.state.market.ppa
        if contract is None:
            return "нет, вся выработка на спот"
        left = contracts.remaining_hours(self.simulation) // 24
        return (
            f"{contract.volume_mwh:,.0f} МВт·ч/ч по {contract.price_cents / 100:,.0f} ₽, "
            f"ещё {left} сут"
        ).replace(",", " ")

    def _fuel_line(self) -> str:
        state = self.simulation.state
        transit = fuel.in_transit_tons(self.simulation)
        line = f"{state.fuel.stock_tons:.1f} т"
        if transit > 0.0:
            line += f", в пути {transit:.1f} т"
        return line

    def _status_note(self) -> str:
        """Срок на исправление всегда на виду: провал обязан быть предсказуем (И8)."""
        state = self.simulation.state
        if state.finance.status != finance.STATUS_GRACE:
            return ""
        left = max(0, state.finance.grace_ends_tick - state.tick)
        return f" (осталось {left // 24} сут)"

    def _orders_line(self) -> str:
        active = orders.active_orders(self.simulation)
        if not active:
            return "нет"
        now = self.simulation.state.tick
        return ", ".join(f"{raw['kind']} (осталось {raw['ends_tick'] - now} ч)" for raw in active)

    def _maybe_autosave(self, now: float) -> None:
        state = self.simulation.state
        if state.tick - self._last_autosave_tick < AUTOSAVE_EVERY_TICKS:
            return
        if now - self._last_autosave_at < AUTOSAVE_MIN_INTERVAL_S:
            return
        self._save()

    def _save(self) -> None:
        save_mod.save_game(
            self.simulation.sync_state(),
            now=time.time(),
            created_at=self.created_at,
            path=self.save_path,
        )
        self._last_autosave_tick = self.simulation.state.tick
        self._last_autosave_at = time.monotonic()

    # -- действия --------------------------------------------------------

    def action_toggle_pause(self) -> None:
        state = self.simulation.state
        target = Speed.X1 if state.speed == Speed.PAUSED else Speed.PAUSED
        self.simulation.clock.set_speed(target)
        # Долг тиков сбрасывается: иначе снятие с паузы выплюнуло бы разом
        # часы, «накопившиеся» на паузе.
        self._tick_debt = 0.0
        self._refresh_panel()

    def action_speed(self, speed: str) -> None:
        self.simulation.clock.set_speed(speed)
        # Долг тиков накоплен по прежнему множителю; переносить его на новую
        # скорость значило бы выплюнуть лишний час при переключении.
        self._tick_debt = 0.0
        self._refresh_panel()

    def action_power(self, delta: float) -> None:
        """Сдвинуть уставку мощности. Блок пойдёт к ней сам, ждать не нужно."""
        reactor_state = self.simulation.state.reactor
        target = min(1.0, max(0.0, reactor_state.power_setpoint + delta))
        reactor_state.power_setpoint = target
        self._refresh_panel()

    def action_order(self, kind: str) -> None:
        """Отдать приказ. Управление возвращается сразу (И7)."""
        try:
            order = orders.issue_order(self.simulation, kind)
        except ValueError as exc:
            self.query_one("#notice", Static).update(str(exc))
            return
        hours = order.ends_tick - order.issued_tick
        self.query_one("#notice", Static).update(
            f"Приказ «{order.kind}» принят, завершится через {hours} ч."
        )
        self._refresh_panel()

    def action_save_now(self) -> None:
        self._save()
        self.query_one("#notice", Static).update("Сохранено.")

    def on_unmount(self) -> None:
        # Выход обязан оставить партию на диске: инвариант И6 запрещает любую
        # активность после закрытия, значит последний шанс — здесь.
        self._panel = None
        self._station = None
        self._save()
