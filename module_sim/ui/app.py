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

from module_sim.core.sim import Simulation
from module_sim.core.state import SPEED_MULTIPLIER, Speed
from module_sim.persistence import save as save_mod
from module_sim.ui.widgets.time_panel import TimePanel

__all__ = ["ModuleApp"]

#: Частота кадров UI. К симуляции отношения не имеет (И3).
FRAME_INTERVAL = 1.0 / 20.0

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

    # -- разметка --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield TimePanel(id="time-panel")
        with Container(id="body"):
            yield Static(
                "[b]Пусто.[/b]\n\n"
                "Фаза 0: каркас, часы и сохранения.\n"
                "Реактор, рынок и персонал появятся в следующих фазах.",
                id="placeholder",
            )
        yield Static(self.notice_text, id="notice")
        yield Footer()

    def on_mount(self) -> None:
        self._panel = self.query_one(TimePanel)
        self._frame_at = time.monotonic()
        self._last_autosave_at = self._frame_at
        self.set_interval(FRAME_INTERVAL, self._on_frame)
        self._refresh_panel()

    # -- петля -----------------------------------------------------------

    def _on_frame(self) -> None:
        now = time.monotonic()
        elapsed = now - self._frame_at
        self._frame_at = now

        multiplier = SPEED_MULTIPLIER[self.simulation.state.speed]
        if multiplier > 0.0:
            self._tick_debt += elapsed * multiplier
            whole = int(self._tick_debt)
            if whole:
                self._tick_debt -= whole
                self.simulation.run(whole)

        self._refresh_panel()
        self._maybe_autosave(now)

    def _refresh_panel(self) -> None:
        panel = self._panel
        if panel is None:
            return
        state = self.simulation.state
        panel.company = state.company.name
        panel.game_date = self.simulation.clock.format_datetime()
        panel.speed = state.speed
        panel.tick = state.tick

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
        self._refresh_panel()

    def action_save_now(self) -> None:
        self._save()
        self.query_one("#notice", Static).update("Сохранено.")

    def on_unmount(self) -> None:
        # Выход обязан оставить партию на диске: инвариант И6 запрещает любую
        # активность после закрытия, значит последний шанс — здесь.
        self._panel = None
        self._save()
