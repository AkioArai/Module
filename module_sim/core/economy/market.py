"""Продажа энергии (DESIGN.md, §3.2).

Фаза 1 — фиксированная цена. Суточный профиль, сезонность, споты и PPA придут в
фазе 2; здесь важна не цена, а **способ расчёта с игроком**.

Деньги в игре целые (SAVEFORMAT.md, §2), а выработка непрерывна. Значит,
где-то происходит округление, и место это выбрано не случайно.

Наивный вариант — начислять деньги на каждом шаге интегрирования — сломал бы
контракт И4а: потиковый путь округлял бы 24 раза в сутки, батчевый один раз, и
дискретное состояние (касса) разошлось бы. Касса же обязана совпадать точно.

Поэтому выручка **начисляется только в дискретном событии расчёта**, раз в
игровые сутки. Планировщик гарантирует, что расчёт происходит на одних и тех же
тиках в обоих путях, а значит и округление — в одних и тех же точках.

Второй приём: платится не «выручка за период», а разница между тем, сколько
причитается по накопленной выработке, и тем, сколько уже выплачено. Такой
расчёт самокорректирующийся — ошибка округления не накапливается по периодам,
сколько бы их ни прошло.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from module_sim.core.events import registry
from module_sim.core.events.scheduler import ScheduledEvent

if TYPE_CHECKING:  # pragma: no cover - подсистемы не тянут sim во время работы
    from module_sim.core.sim import Simulation

__all__ = ["PRICE_PER_MWH_CENTS", "SETTLEMENT_INTERVAL_HOURS", "SETTLEMENT_KIND", "settle"]

#: BALANCE.md, §3: 3000 ₽ за МВт·ч. В фазе 1 цена не ходит.
PRICE_PER_MWH_CENTS = 300_000

#: Расчёт раз в игровые сутки — так же, как на настоящем рынке на сутки вперёд.
SETTLEMENT_INTERVAL_HOURS = 24

SETTLEMENT_KIND = "market.settlement"


def amount_due_cents(energy_mwh: float) -> int:
    """Сколько причитается за всю выработку партии, копеек."""
    return round(energy_mwh * PRICE_PER_MWH_CENTS)


def settle(simulation: Simulation) -> int:
    """Выплатить разницу между причитающимся и уже выплаченным. Вернуть её."""
    market = simulation.state.market
    due = amount_due_cents(market.energy_sold_mwh)
    payment = due - market.revenue_paid_cents
    market.revenue_paid_cents = due
    simulation.state.company.cash_cents += payment
    return payment


@registry.handler(SETTLEMENT_KIND)
def _on_settlement(simulation: Simulation, event: ScheduledEvent) -> None:
    settle(simulation)
    simulation.schedule_in(SETTLEMENT_INTERVAL_HOURS, SETTLEMENT_KIND)
