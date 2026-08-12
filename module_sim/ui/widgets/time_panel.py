"""Панель времени — всегда на виду (DESIGN.md, §11).

Показывает игровую дату, скорость и номер тика. Единственный виджет фазы 0:
всё остальное появится вместе с подсистемами, а панель времени нужна с первого
дня — по ней видно, что часы идут и что догон отработал.

Цвета берутся из ``theme.tcss``. В коде виджетов цветов нет (CLAUDE.md,
§«Как добавить»).
"""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static

from module_sim.core.state import SPEED_MULTIPLIER, Speed

__all__ = ["TimePanel"]

_SPEED_LABEL = {
    Speed.PAUSED: "⏸ пауза",
    Speed.X1: "▶ 1x",
    Speed.X5: "▶▶ 5x",
    Speed.X50: "▶▶▶ 50x",
}


class TimePanel(Static):
    """Строка состояния времени."""

    game_date: reactive[str] = reactive("—")
    speed: reactive[str] = reactive(Speed.X1)
    tick: reactive[int] = reactive(0)
    company: reactive[str] = reactive("")

    def render(self) -> str:
        label = _SPEED_LABEL.get(self.speed, self.speed)
        multiplier = SPEED_MULTIPLIER.get(self.speed, 0.0)
        rate = "мир стоит" if multiplier == 0.0 else f"{multiplier:g} ч/с"
        # Цвета — переменными темы (``$accent``, ``$text-muted``), а не именами
        # вроде ``accent``: последние Rich молча проглатывает и рисует обычным
        # текстом, так что ошибка в разметке была бы не видна.
        return (
            f"[b]{self.company}[/b]   "
            f"[$accent]{self.game_date}[/]   "
            f"{label} ({rate})   "
            f"[$text-muted]тик {self.tick}[/]"
        )
