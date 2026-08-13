"""Договор на поставку по фиксированной цене — PPA (DESIGN.md, §3.3).

Хеджирование — центральный экономический выбор игры. Доля выработки под
договором сглаживает доход, но отрезает от ценовых пиков и превращает любой
простой в прямой убыток: обязательство поставлять действует и тогда, когда блок
стоит, а недостающее приходится покупать на рынке дороже (economy/market.py).

Оптимальная доля зависит от того, насколько надёжно станция держит мощность, —
то есть от физики и от людей, которых ещё нет. Поэтому цифра «сколько брать под
договор» в этой фазе неответима, и это правильно: она обязана стать ответимой
только к фазе 5, вместе с отказами оборудования.

Договор один. Не потому, что несколько сложно посчитать, а потому, что доля
хеджа полностью выражается объёмом одного договора, а два одновременных
потребовали бы правила, кому из них достаётся выработка при нехватке. Правило
это — экономика распределения, а не хеджирования, и вводить её вместе с первым
договором значит спрятать главный выбор за второстепенным.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from module_sim.core.clock import MONTH_HOURS
from module_sim.core.economy.prices import HOURS_PER_DAY, fair_price_cents
from module_sim.core.events import registry
from module_sim.core.events.scheduler import ScheduledEvent
from module_sim.core.physics.reactor import NOMINAL_ELECTRIC_MW
from module_sim.core.state import PpaContract

if TYPE_CHECKING:  # pragma: no cover - подсистемы не тянут sim во время работы
    from module_sim.core.sim import Simulation

__all__ = [
    "EXPIRE_KIND",
    "PPA_DISCOUNT_TO_SPOT",
    "PPA_TERM_DEFAULT_MONTHS",
    "ContractError",
    "offered_price_cents",
    "remaining_hours",
    "sign_ppa",
]

EXPIRE_KIND = "ppa.expire"

#: BALANCE.md, §4.
PPA_TERM_DEFAULT_MONTHS = 12
PPA_DISCOUNT_TO_SPOT = 0.92

#: Больше номинала блока продавать нельзя. Это не бухгалтерия, а игровое
#: правило: договор на объём, который станция физически не выдаёт даже
#: идеально работая, — не хедж, а гарантированный штраф, и подписывать его
#: игрок может только по ошибке.
MAX_VOLUME_MWH = NOMINAL_ELECTRIC_MW


class ContractError(ValueError):
    """Договор нельзя заключить на таких условиях."""


def offered_price_cents(simulation: Simulation) -> int:
    """Цена, которую контрагент даёт сегодня, копеек за МВт·ч.

    Дисконт к средней цене — плата за то, что риск берёт на себя покупатель
    (BALANCE.md, §4: PPA_DISCOUNT_TO_SPOT). Цена целая: она уходит в состояние
    и в деньги, а деньги в игре целые.
    """
    market = simulation.state.market
    fair = fair_price_cents(market.noise, market.demand_deviation)
    return round(fair * PPA_DISCOUNT_TO_SPOT)


def remaining_hours(simulation: Simulation) -> int:
    """Сколько часов действует договор. Ноль — договора нет."""
    contract = simulation.state.market.ppa
    if contract is None:
        return 0
    return max(0, contract.ends_tick - simulation.state.tick)


def sign_ppa(
    simulation: Simulation,
    volume_mwh: float,
    months: int = PPA_TERM_DEFAULT_MONTHS,
    price_cents: int | None = None,
) -> PpaContract:
    """Заключить договор на ``volume_mwh`` в каждый час на ``months`` месяцев.

    Срок округляется вверх до границы суток: суточная граница — это момент,
    когда рынок и так пересчитывает всё (economy/market.py), и договор,
    кончающийся посреди суток, добавил бы ровно одну лишнюю точку разрыва
    ради ничего.
    """
    market = simulation.state.market
    if market.ppa is not None:
        raise ContractError("договор уже есть; одновременно действует только один")
    if volume_mwh <= 0.0:
        raise ContractError("объём договора должен быть положительным")
    if volume_mwh > MAX_VOLUME_MWH:
        raise ContractError(
            f"объём {volume_mwh:.0f} МВт·ч/ч больше номинала блока ({MAX_VOLUME_MWH:.0f})"
        )
    if months <= 0:
        raise ContractError("срок договора должен быть положительным")

    tick = simulation.state.tick
    ends = tick + months * MONTH_HOURS
    ends += (-ends) % HOURS_PER_DAY

    contract = PpaContract(
        volume_mwh=volume_mwh,
        price_cents=offered_price_cents(simulation) if price_cents is None else price_cents,
        signed_tick=tick,
        ends_tick=ends,
    )
    market.ppa = contract
    simulation.schedule(ends, EXPIRE_KIND, {"signed_tick": tick})
    return contract


@registry.handler(EXPIRE_KIND)
def _on_expire(simulation: Simulation, event: ScheduledEvent) -> None:
    """Срок вышел — договор кончается сам.

    Событие сверяется с договором по времени подписания: в очереди может
    лежать событие от договора, которого уже нет (в этой фазе такого не
    случается, но перезаключение появится, а миграции старых очередей
    останутся). Молча снять чужой договор — худшее, что тут можно сделать.
    """
    contract = simulation.state.market.ppa
    if contract is None:
        return
    if contract.signed_tick != event.payload.get("signed_tick"):
        return
    simulation.state.market.ppa = None
