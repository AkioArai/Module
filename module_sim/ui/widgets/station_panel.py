"""Панель станции: мощность, топливо, деньги, приказы (DESIGN.md, §11).

Виджет только отображает. Он не читает файлы, не считает формулы и не двигает
симуляцию — данные приходят снаружи готовыми, а управление уходит приказами
(И1: интерфейс не мутирует симуляцию).

Цветов здесь нет намеренно: всё оформление в ``theme.tcss``.
"""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static

__all__ = ["StationPanel"]

#: Ширина шкалы мощности в символах.
BAR_WIDTH = 24


def bar(fraction: float, width: int = BAR_WIDTH) -> str:
    """Горизонтальная шкала. Значения вне 0…1 обрезаются, а не ломают вёрстку."""
    filled = max(0, min(width, round(fraction * width)))
    return "█" * filled + "·" * (width - filled)


def money(cents: int) -> str:
    """Деньги в рублях с разделителями. Копейки не показываем: на масштабе
    станции они шум, а вот целостность разряда важна."""
    return f"{cents / 100:,.0f} ₽".replace(",", " ")


class StationPanel(Static):
    """Сводка по блоку и компании."""

    power_mw: reactive[float] = reactive(0.0)
    setpoint: reactive[float] = reactive(0.0)
    level: reactive[float] = reactive(0.0)
    burnup: reactive[float] = reactive(0.0)
    cash_cents: reactive[int] = reactive(0)
    energy_mwh: reactive[float] = reactive(0.0)
    price_cents: reactive[float] = reactive(0.0)
    price_note: reactive[str] = reactive("")
    contract_line: reactive[str] = reactive("нет")
    fuel_line: reactive[str] = reactive("")
    debt_cents: reactive[int] = reactive(0)
    equity_cents: reactive[int] = reactive(0)
    rate: reactive[float] = reactive(0.0)
    status: reactive[str] = reactive("норма")
    status_note: reactive[str] = reactive("")
    orders_line: reactive[str] = reactive("нет")

    def render(self) -> str:
        return "\n".join(
            (
                f"[b]Блок[/b]   {self.power_mw:>7,.0f} МВт   {bar(self.level)}".replace(",", " "),
                f"        уставка {self.setpoint * 100:>3.0f}%   кампания "
                f"выработана на {self.burnup * 100:.1f}%",
                f"        топливо {self.fuel_line}",
                "",
                f"[b]Рынок[/b]  {self.price_cents / 100:,.0f} ₽/МВт·ч{self.price_note}".replace(
                    ",", " "
                ),
                f"        договор: {self.contract_line}",
                "",
                f"[b]Касса[/b]  {money(self.cash_cents)}",
                f"        отпущено {self.energy_mwh:,.0f} МВт·ч".replace(",", " "),
                "",
                f"[b]Долг[/b]   {money(self.debt_cents)}   ставка {self.rate * 100:.0f}%",
                f"        капитал {money(self.equity_cents)}   "
                f"состояние: {self.status}{self.status_note}",
                "",
                f"[b]Приказы[/b] {self.orders_line}",
            )
        )
