"""Финансы: долг, проценты, ковенанты, банкротство (DESIGN.md, §3.5, §3.8).

Здесь же реализуется первая половина решения §Р5 — та, что про «партия не
заканчивается в отсутствие игрока». Вторая половина (что именно делать при
возвращении) живёт на границе с реальным временем, в ``cli``: ядро о
существовании игрока не знает и знать не должно.

**Все деньги целые и меняются только в дискретных событиях.** Проценты,
операционные расходы, проверка ковенантов, условие банкротства — всё раз в
игровой месяц, событием планировщика. Причина та же, что и у расчётов за
энергию: непрерывное начисление округлялось бы в разных точках у батчевого и
потикового пути, и касса разошлась бы, а она обязана совпадать точно (И4а,
уровень 2). Проценты, начисляемые периодически, — заодно и то, как это устроено
в жизни.

Кредитная линия автоматическая: ушли в минус по кассе — ушли в долг. Отдельного
действия «взять кредит» нет намеренно. Игрок управляет не займами, а тем,
покрывает ли выручка расходы; заём — следствие, а не решение.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from module_sim.core.economy import bankruptcy_discount
from module_sim.core.events import registry
from module_sim.core.events.scheduler import ScheduledEvent

if TYPE_CHECKING:  # pragma: no cover - подсистемы не тянут sim во время работы
    from module_sim.core.sim import Simulation

__all__ = [
    "GRACE_HOURS",
    "MONTH_HOURS",
    "STATUS_BANKRUPT",
    "STATUS_GRACE",
    "STATUS_NORMAL",
    "discharge",
    "equity_cents",
    "start_company",
]

MONTH_KIND = "finance.month"

#: Игровой месяц. Тридцать суток — то же округление, что у топливной кампании
#: (BALANCE.md, §2.5), чтобы периоды в игре были соизмеримы.
MONTH_HOURS = 30 * 24

#: BALANCE.md, §5. Станция заложена: балансовая стоимость много меньше, чем
#: стоила бы постройка, потому что игрок получает её уже в кредит.
STATION_VALUE_CENTS = 500_000_000_000
STARTING_DEBT_CENTS = 400_000_000_000

#: Операционные расходы: топливо, персонал, обслуживание. Не зависят от
#: мощности — остановленный блок обходится почти так же дорого, как рабочий, и
#: в этом вся суть простоя.
OPEX_MONTHLY_CENTS = 50_000_000_000

#: Базовая ставка и надбавка за каждый месяц нарушения ковенанта (BALANCE.md, §5).
RATE_BASE_ANNUAL = 0.09
RATE_PENALTY_PER_BREACH = 0.02
RATE_MAX_ANNUAL = 0.40

#: Сколько месяцев подряд можно не покрывать проценты, прежде чем начнётся
#: предбанкротное состояние (BALANCE.md, §5: DSCR_COVENANT).
COVENANT_BREACH_LIMIT = 2

#: Срок на исправление после введения предбанкротного состояния — полгода.
GRACE_HOURS = 6 * MONTH_HOURS

#: Последствия процедуры списания: три игровых года дорогих денег.
DISCHARGE_PENALTY_HOURS = 3 * 12 * MONTH_HOURS
DISCHARGE_RATE_PENALTY = 0.15

STATUS_NORMAL = "норма"
STATUS_GRACE = "предбанкротство"
STATUS_BANKRUPT = "банкрот"


def start_company(simulation: Simulation, carried_debt_cents: int = 0) -> None:
    """Выдать новой компании стартовый долг и то, что осталось от прошлой.

    Переходящий долг — не штраф ради штрафа. Он делает провал тем, что тянется,
    и потому единственное, что игрок обязан не допустить (DESIGN.md, §3.8).
    """
    finance = simulation.state.finance
    finance.debt_cents = STARTING_DEBT_CENTS + carried_debt_cents
    finance.assets_cents = STATION_VALUE_CENTS


def equity_cents(simulation: Simulation) -> int:
    """Капитал: активы минус обязательства. Отрицательный — компания банкрот
    по балансу, даже если на счету ещё есть деньги."""
    state = simulation.state
    return state.company.cash_cents + state.finance.assets_cents - state.finance.debt_cents


def annual_rate(simulation: Simulation) -> float:
    """Ставка по долгу. Растёт с каждым нарушением ковенанта и после списания.

    Дорогие деньги — главный механизм, которым прошлые ошибки давят на
    настоящее: не запрет, а налог на неосторожность.
    """
    finance = simulation.state.finance
    rate = RATE_BASE_ANNUAL + RATE_PENALTY_PER_BREACH * finance.covenant_breaches
    if simulation.state.tick < finance.discharge_penalty_until_tick:
        rate += DISCHARGE_RATE_PENALTY
    return min(rate, RATE_MAX_ANNUAL)


def _charge_interest(simulation: Simulation) -> int:
    finance = simulation.state.finance
    if finance.debt_cents <= 0:
        return 0
    monthly = annual_rate(simulation) / 12.0
    interest = round(finance.debt_cents * monthly)
    finance.debt_cents += interest
    return interest


def _pay_and_borrow(simulation: Simulation, amount_cents: int) -> None:
    """Списать расход. Не хватило кассы — разница уходит в долг.

    Касса не уходит в минус никогда: отрицательные деньги на счету — это уже
    заём, и честнее называть его заёмом.
    """
    company = simulation.state.company
    company.cash_cents -= amount_cents
    if company.cash_cents < 0:
        simulation.state.finance.debt_cents += -company.cash_cents
        company.cash_cents = 0


def _update_covenant(simulation: Simulation, interest_cents: int, opex_cents: int) -> None:
    """Ковенант: месячная выручка обязана покрывать проценты и расходы.

    Считается по факту месяца, а не по прогнозу: игрок должен видеть причину в
    том, что уже случилось (И8).
    """
    finance = simulation.state.finance
    market = simulation.state.market

    earned = market.revenue_paid_cents - finance.revenue_at_last_month_cents
    finance.revenue_at_last_month_cents = market.revenue_paid_cents

    if earned >= interest_cents + opex_cents:
        finance.months_below_covenant = 0
        return

    finance.months_below_covenant += 1
    if finance.months_below_covenant >= COVENANT_BREACH_LIMIT:
        finance.covenant_breaches += 1
        finance.months_below_covenant = 0


def _check_bankruptcy(simulation: Simulation) -> None:
    """Условие банкротства и срок на исправление.

    Само по себе отрицательное сальдо месяца — не банкротство. Банкротство
    наступает, когда обязательства превысили активы: компания не может
    расплатиться, даже продав всё. Тогда даётся срок, и только его истечение
    заканчивает партию (DESIGN.md, §3.8).
    """
    finance = simulation.state.finance
    if finance.status == STATUS_BANKRUPT:
        return

    if equity_cents(simulation) >= 0:
        # Выкарабкались: срок снимается, но нарушения ковенанта остаются —
        # ставка так просто не возвращается.
        if finance.status == STATUS_GRACE:
            finance.status = STATUS_NORMAL
            finance.grace_ends_tick = 0
        return

    if finance.status == STATUS_NORMAL:
        finance.status = STATUS_GRACE
        finance.grace_ends_tick = simulation.state.tick + GRACE_HOURS
        return

    if simulation.state.tick >= finance.grace_ends_tick:
        finance.status = STATUS_BANKRUPT


@registry.handler(MONTH_KIND)
def _on_month(simulation: Simulation, event: ScheduledEvent) -> None:
    interest = _charge_interest(simulation)
    _pay_and_borrow(simulation, OPEX_MONTHLY_CENTS)
    _update_covenant(simulation, interest, OPEX_MONTHLY_CENTS)
    _check_bankruptcy(simulation)
    simulation.schedule_in(MONTH_HOURS, MONTH_KIND)


# -- процедура банкротства -------------------------------------------------


def carried_debt_cents(simulation: Simulation) -> int:
    """Сколько долга переживёт эту партию.

    Активы продаются с дисконтом — распродажа под принуждением всегда дешевле
    (BALANCE.md, §5: BANKRUPTCY_ASSET_DISCOUNT).
    """
    finance = simulation.state.finance
    recovered = simulation.state.company.cash_cents + round(
        finance.assets_cents * bankruptcy_discount()
    )
    return max(0, finance.debt_cents - recovered)


def discharge(simulation: Simulation) -> int:
    """Официальная процедура: долг списывается, репутация — нет.

    Выход существует затем, чтобы переходящий долг не превращал игру в
    математически непроходимую после трёх неудач. Цена — три игровых года
    дорогих денег, и это ощутимо больнее, чем кажется на бумаге.

    Возвращает списанную сумму.
    """
    finance = simulation.state.finance
    written_off = finance.debt_cents
    finance.debt_cents = 0
    finance.status = STATUS_NORMAL
    finance.grace_ends_tick = 0
    finance.months_below_covenant = 0
    finance.discharge_penalty_until_tick = simulation.state.tick + DISCHARGE_PENALTY_HOURS
    finance.discharges += 1
    return written_off


def grant_return_grace(simulation: Simulation) -> None:
    """Дать вернувшемуся игроку новый срок вместо проигрыша (DESIGN.md, §Р5).

    Вызывается **только с границы с реальным временем** (``cli``), и только
    когда банкротство наступило, пока игры не было. Ядро о существовании игрока
    не знает: реши мы это внутри симуляции, догон перестал бы совпадать с
    потиковым прогоном, и весь контракт И4а посыпался бы.

    Партия, проигранная за выключенным экраном, ничему не учит — игрок не видел
    ни одного решения, которое привело к провалу.
    """
    finance = simulation.state.finance
    finance.status = STATUS_GRACE
    finance.grace_ends_tick = simulation.state.tick + GRACE_HOURS
