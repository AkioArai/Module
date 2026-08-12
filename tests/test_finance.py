"""Финансы, ковенанты и банкротство (economy/finance.py).

Проверяется три вещи, каждая из которых важнее предыдущей:

1. деньги остаются целыми и точными в обоих путях времени (И4а, уровень 2);
2. провал наступает по понятной причине и не сваливается внезапно (И8);
3. партия не заканчивается, пока игрока нет, и из долгов есть выход (§Р5).
"""

from __future__ import annotations

from module_sim.core.economy import finance
from module_sim.core.sim import Simulation

EPOCH = 1_767_225_600.0
YEAR = 12 * finance.MONTH_HOURS


def make(power: float = 0.0, carried: int = 0) -> Simulation:
    sim = Simulation.new_game(seed=4242, epoch=EPOCH)
    finance.start_company(sim, carried)
    sim.state.reactor.power_setpoint = power
    return sim


# -- деньги ----------------------------------------------------------------


def test_company_starts_in_debt():
    """Станцию не покупают за наличные. Долг — стартовое условие, а не провал."""
    sim = make()
    assert sim.state.finance.debt_cents == finance.STARTING_DEBT_CENTS
    assert finance.equity_cents(sim) > 0


def test_carried_debt_lands_on_the_new_company():
    sim = make(carried=100_000_000_000)
    assert sim.state.finance.debt_cents == finance.STARTING_DEBT_CENTS + 100_000_000_000


def test_interest_accrues_monthly_not_continuously():
    """Долг обязан стоять на месте между событиями начисления.

    Непрерывное начисление округлялось бы в разных точках у батчевого и
    потикового пути, и касса разошлась бы.
    """
    sim = make()
    start = sim.state.finance.debt_cents

    sim.run(finance.MONTH_HOURS - 1)
    assert sim.state.finance.debt_cents == start

    sim.run(1)
    assert sim.state.finance.debt_cents > start


def test_money_is_identical_stepwise_and_batched():
    """Ключевая гарантия уровня 2: деньги совпадают точно, а не по допуску."""
    stepwise = make(power=1.0)
    stepwise.run(YEAR)

    batched = make(power=1.0)
    batched.catch_up(YEAR)

    assert batched.state.company.cash_cents == stepwise.state.company.cash_cents
    assert batched.state.finance.debt_cents == stepwise.state.finance.debt_cents


def test_cash_never_goes_negative():
    """Отрицательные деньги на счету — это заём, и называть его надо заёмом."""
    sim = make()
    sim.run(YEAR)
    assert sim.state.company.cash_cents >= 0
    assert sim.state.finance.debt_cents > finance.STARTING_DEBT_CENTS


# -- работа окупается ------------------------------------------------------


def test_running_at_full_power_is_profitable():
    sim = make(power=1.0)
    sim.run(6 * finance.MONTH_HOURS)
    assert finance.equity_cents(sim) > finance.STATION_VALUE_CENTS


def test_idle_station_bleeds():
    """Простой не бесплатен: расходы от мощности не зависят."""
    sim = make(power=0.0)
    sim.run(6 * finance.MONTH_HOURS)
    assert finance.equity_cents(sim) < finance.STATION_VALUE_CENTS


def test_covenant_breach_makes_money_more_expensive():
    """Прошлые ошибки давят на настоящее не запретом, а ставкой."""
    sim = make(power=0.0)
    before = finance.annual_rate(sim)
    sim.run(6 * finance.MONTH_HOURS)
    assert finance.annual_rate(sim) > before


# -- провал ----------------------------------------------------------------


def test_bankruptcy_is_preceded_by_a_warning_period():
    """Провал не сваливается внезапно: сначала предбанкротное состояние.

    Между ним и концом партии — полгода игрового времени, за которые игрок
    может всё исправить.
    """
    sim = make(power=0.0)
    seen_grace = False
    for _ in range(24):
        sim.run(finance.MONTH_HOURS)
        if sim.state.finance.status == finance.STATUS_GRACE:
            seen_grace = True
        if sim.state.finance.status == finance.STATUS_BANKRUPT:
            break

    assert sim.state.finance.status == finance.STATUS_BANKRUPT
    assert seen_grace, "банкротство наступило без предупреждения"


def test_recovery_cancels_the_grace_period():
    """Выкарабкался — срок снят. Иначе игроку незачем было бы исправляться."""
    sim = make(power=0.0)
    while sim.state.finance.status == finance.STATUS_NORMAL:
        sim.run(finance.MONTH_HOURS)
    assert sim.state.finance.status == finance.STATUS_GRACE

    sim.state.reactor.power_setpoint = 1.0
    sim.run(6 * finance.MONTH_HOURS)

    assert sim.state.finance.status == finance.STATUS_NORMAL


def test_carried_debt_accounts_for_the_fire_sale():
    """Активы при банкротстве продаются с дисконтом — потому долг и остаётся."""
    sim = make(power=0.0)
    sim.state.finance.debt_cents = finance.STATION_VALUE_CENTS * 2
    carried = finance.carried_debt_cents(sim)
    assert 0 < carried < sim.state.finance.debt_cents


# -- выход из долгов -------------------------------------------------------


def test_discharge_clears_the_debt():
    sim = make()
    written = finance.discharge(sim)
    assert written > 0
    assert sim.state.finance.debt_cents == 0
    assert sim.state.finance.status == finance.STATUS_NORMAL


def test_discharge_is_paid_for_with_expensive_money():
    """Иначе списание было бы бесплатным, и долг перестал бы что-то значить."""
    sim = make()
    before = finance.annual_rate(sim)
    finance.discharge(sim)
    assert finance.annual_rate(sim) > before


def test_discharge_penalty_expires():
    """Наказание за списание конечно — три игровых года, и деньги дешевеют.

    Блок работает на мощности намеренно: иначе за три года набежали бы
    нарушения ковенанта, ставка упёрлась бы в потолок, и тест проверял бы не
    истечение наказания, а этот потолок.
    """
    sim = make(power=1.0)
    finance.discharge(sim)
    penalised = finance.annual_rate(sim)

    sim.run(finance.DISCHARGE_PENALTY_HOURS + finance.MONTH_HOURS)

    assert finance.annual_rate(sim) < penalised
    assert sim.state.finance.covenant_breaches == 0, "ставку подняли нарушения, а не списание"


def test_return_grace_replaces_a_loss_the_player_never_saw():
    """§Р5: банкротство за выключенным экраном превращается в новый срок."""
    sim = make(power=0.0)
    sim.state.finance.status = finance.STATUS_BANKRUPT

    finance.grant_return_grace(sim)

    assert sim.state.finance.status == finance.STATUS_GRACE
    assert sim.state.finance.grace_ends_tick == sim.state.tick + finance.GRACE_HOURS
