"""Расчёты за энергию (economy/market.py).

Главное здесь — не цена, а место округления. Деньги целые, выработка
непрерывна, и если начислять выручку на каждом шаге интегрирования, потиковый
путь округлит двадцать четыре раза в сутки, а батчевый один. Касса разъедется,
а она обязана совпадать точно (И4а, уровень 2).
"""

from __future__ import annotations

from support.state_compare import assert_states_equivalent

from module_sim.core.economy import market
from module_sim.core.sim import Simulation

EPOCH = 1_767_225_600.0


def make(power: float = 1.0) -> Simulation:
    sim = Simulation.new_game(seed=4242, epoch=EPOCH)
    sim.state.reactor.power_setpoint = power
    return sim


def test_settlement_pays_for_produced_energy():
    sim = make()
    before = sim.state.company.cash_cents
    sim.run(48)
    assert sim.state.company.cash_cents > before


def test_idle_station_earns_nothing():
    """Выручки нет. Касса при этом падает — простой оплачивается из кармана,
    расходы от мощности не зависят (economy/finance.py)."""
    sim = make(power=0.0)
    sim.run(24 * 30)
    assert sim.state.market.revenue_paid_cents == 0
    assert sim.state.market.energy_sold_mwh == 0.0


def test_payment_is_the_difference_not_the_period():
    """Платится разница между причитающимся и уже выплаченным.

    Такой расчёт самокорректирующийся: ошибка округления одного периода
    гасится следующим, а не копится. Проверяется прямо: выплаченное всегда
    равно округлённой стоимости всей выработки партии.
    """
    sim = make()
    for _ in range(10):
        sim.run(24)
        due = market.amount_due_cents(sim.state.market.energy_sold_mwh)
        assert sim.state.market.revenue_paid_cents == due


def test_rounding_error_does_not_accumulate():
    """Сто периодов подряд не уводят кассу от точной стоимости выработки."""
    sim = make(power=0.317)  # некруглая мощность — некруглая выручка
    sim.run(24 * 100)

    expected = market.amount_due_cents(sim.state.market.energy_sold_mwh)
    assert sim.state.market.revenue_paid_cents == expected


def test_cash_is_identical_stepwise_and_batched():
    """Ключевая гарантия: деньги совпадают **точно**, а не в пределах допуска.

    Расчёт привязан к дискретному событию, поэтому округление происходит на
    одних и тех же тиках в обоих путях.
    """
    stepwise = make()
    stepwise.run(24 * 40)

    batched = make()
    batched.catch_up(24 * 40)

    assert batched.state.company.cash_cents == stepwise.state.company.cash_cents
    assert batched.state.market.revenue_paid_cents == stepwise.state.market.revenue_paid_cents


def test_settlement_reschedules_itself():
    """Расчёт должен идти всю партию, а не один раз."""
    sim = make()
    sim.run(24 * 5)
    kinds = [event.kind for event in sim.scheduler.pending()]
    assert market.SETTLEMENT_KIND in kinds


def test_whole_state_stays_equivalent_across_paths():
    stepwise = make()
    stepwise.run(24 * 40)

    batched = make()
    batched.catch_up(24 * 40)

    assert_states_equivalent(
        batched.sync_state().to_dict(),
        stepwise.sync_state().to_dict(),
    )
