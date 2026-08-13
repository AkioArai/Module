"""Топливо: расход, цена, лаг поставки, автозакупка (economy/fuel.py).

Главное, что здесь охраняется, — лаг. Цена фиксируется в момент заказа, а
платят через четыре игровых месяца по цене заказа, и никакое движение рынка за
это время на подписанный контракт не влияет. Если это сломается, топливо
перестанет быть решением и станет ещё одной строкой расходов.
"""

from __future__ import annotations

import pytest
from support.state_compare import assert_states_equivalent, relative_difference

from module_sim.core.economy import finance, fuel
from module_sim.core.physics.reactor import CAMPAIGN_HOURS, NOMINAL_ELECTRIC_MW
from module_sim.core.sim import Simulation
from module_sim.core.state import FuelState, GameState

EPOCH = 1_767_225_600.0


def make(power: float = 1.0, seed: int = 4242, *, ramped: bool = False) -> Simulation:
    """Партия с блоком на заданной мощности.

    ``ramped=False`` ставит блок сразу на уставку: разгон занимает часы и
    съедает топлива меньше, чем ровная работа, а в тестах расхода это лишний
    источник расхождения.
    """
    sim = Simulation.new_game(seed=seed, epoch=EPOCH)
    sim.state.reactor.power_setpoint = power
    if not ramped:
        sim.state.reactor.power_level = power
    return sim


# -- расход ----------------------------------------------------------------


def test_campaign_burns_the_campaign_load():
    """Номинал в течение кампании съедает ровно загрузку кампании.

    Это определение расхода, а не совпадение: расход задан через выработку
    именно так, чтобы физика фазы 5 получила уже согласованные числа.
    """
    burned = NOMINAL_ELECTRIC_MW * CAMPAIGN_HOURS * fuel.TONS_PER_MWH
    assert relative_difference(burned, fuel.TONS_PER_CAMPAIGN) < 1e-12


def test_stopped_block_eats_nothing():
    sim = make(power=0.0)
    stock = sim.state.fuel.stock_tons
    sim.run(24 * 90)
    assert sim.state.fuel.stock_tons == stock


def test_half_power_eats_half():
    full = make(power=1.0)
    half = make(power=0.5)
    full.run(24 * 30)
    half.run(24 * 30)

    spent_full = 24.0 - full.state.fuel.stock_tons
    spent_half = 24.0 - half.state.fuel.stock_tons
    assert relative_difference(spent_full, 2.0 * spent_half) < 1e-9


def test_stock_never_goes_negative():
    """Склад упирается в ноль, а не уходит в минус.

    Физического следствия у пустого склада пока нет — оно придёт с перегрузкой
    зоны в фазе 5. Но отрицательный запас на балансе означал бы отрицательный
    актив, а это уже не «не успели заказать», а сломанный учёт.
    """
    sim = make(power=1.0)
    sim.state.fuel.stock_tons = 0.5
    sim.run(24 * 60)
    assert sim.state.fuel.stock_tons == 0.0


# -- цена ------------------------------------------------------------------


def test_price_is_positive_at_any_shock():
    """Цена ходит по логарифму и потому не может стать отрицательной."""
    for noise in (-10.0, -1.0, 0.0, 1.0, 10.0):
        assert fuel.price_cents_per_ton(FuelState(price_noise=noise)) > 0


def test_price_moves_once_a_month():
    """Цена топлива — тоже случайность, и тоже только в событии.

    ``normal()`` тратит два значения счётчика, значит за год их должно уйти
    двадцать четыре, а не по два на каждый час.
    """
    sim = make()
    sim.run(12 * 30 * 24)
    assert sim.rng.stream(fuel.PRICE_STREAM).counter == 12 * 2


def test_price_returns_to_the_mean():
    """За сто лет цена обязана остаться ценой, а не уйти в бесконечность."""
    sim = make(power=1.0)
    sim.catch_up(100 * 365 * 24)
    price = fuel.price_cents_per_ton(sim.state.fuel)
    assert fuel.PRICE_BASE_CENTS_PER_TON / 10 < price < fuel.PRICE_BASE_CENTS_PER_TON * 10


# -- поставки --------------------------------------------------------------


def test_order_arrives_after_the_lag_and_is_paid_on_arrival():
    sim = make(power=0.0)
    stock = sim.state.fuel.stock_tons

    arrival = fuel.order(sim, 5.0)
    assert arrival == fuel.DELIVERY_LAG_HOURS
    assert fuel.in_transit_tons(sim) == 5.0

    sim.catch_up(arrival - 1)
    assert sim.state.fuel.stock_tons == stock, "топливо приехало раньше срока"

    sim.catch_up(1)
    assert sim.state.fuel.stock_tons == stock + 5.0
    assert fuel.in_transit_tons(sim) == 0.0


def test_delivery_is_paid_at_the_agreed_price_not_the_market_one():
    """Смысл контракта: цена зафиксирована при заказе.

    Рынок за четыре месяца может уйти куда угодно — платить придётся по
    подписанному. Без этого лаг перестал бы быть ставкой на будущее.
    """
    sim = make(power=0.0)
    agreed = fuel.price_cents_per_ton(sim.state.fuel)
    fuel.order(sim, 5.0)

    sim.state.fuel.price_noise = 2.0  # рынок улетел втрое
    assert fuel.price_cents_per_ton(sim.state.fuel) > agreed * 2

    before = sim.state.company.cash_cents
    debt_before = sim.state.finance.debt_cents
    sim.catch_up(fuel.DELIVERY_LAG_HOURS)
    # Приёмка попадает в те же сутки, что и месячные расходы, поэтому сверяем
    # не кассу, а то, что списано ровно на стоимость по цене заказа.
    spent = (before - sim.state.company.cash_cents) + (sim.state.finance.debt_cents - debt_before)
    assert spent >= round(5.0 * agreed)
    assert spent < round(5.0 * agreed) + finance.OPEX_MONTHLY_CENTS * 5


def test_procurement_keeps_the_stock_covered():
    """Автозакупка не даёт складу опустеть, пока игрок не вмешался.

    Покрытие считается вместе с тем, что уже едет: заказанное на четыре месяца
    вперёд — это и есть запас, просто ещё не на складе. Сам склад в равновесии
    держится ниже цели ровно на объём, находящийся в пути, и это не недостача,
    а нормальная работа схемы с лагом.
    """
    sim = make(power=1.0)
    sim.catch_up(24 * 365 * 2)

    target = fuel.TONS_PER_MONTH_NOMINAL * fuel.COVERAGE_TARGET_MONTHS
    covered = sim.state.fuel.stock_tons + fuel.in_transit_tons(sim)

    assert sim.state.fuel.stock_tons > 0.0, "склад опустел при работающей автозакупке"
    assert covered > target * 0.9


def test_procurement_does_not_buy_for_a_stopped_block():
    """Остановленный блок топлива не ест, значит и закупать нечего.

    Автозакупка смотрит на склад, а не на календарь: иначе простой оплачивался
    бы ещё и топливом, которое никто не сожжёт.
    """
    sim = make(power=0.0)
    sim.catch_up(24 * 365)
    assert fuel.in_transit_tons(sim) == 0.0
    assert sim.state.fuel.stock_tons == 24.0


def test_order_must_be_positive():
    sim = make()
    with pytest.raises(ValueError, match="положительным"):
        fuel.order(sim, 0.0)


def test_delivery_survives_save():
    """Заказ живёт в очереди событий, а очередь уходит в сейв."""
    sim = make(power=0.0)
    fuel.order(sim, 3.0)
    sim.run(24)

    reloaded = Simulation(GameState.from_dict(sim.sync_state().to_dict()))
    assert fuel.in_transit_tons(reloaded) == 3.0

    reloaded.catch_up(fuel.DELIVERY_LAG_HOURS)
    assert reloaded.state.fuel.stock_tons > 24.0


# -- учёт и эквивалентность -------------------------------------------------


def test_stock_counts_as_an_asset():
    """Закупка впрок не должна выглядеть обеднением компании.

    Иначе осторожность наказывалась бы приближением к банкротству, и игрок
    учился бы не заказывать заранее — ровно наоборот тому, чего требует лаг.
    """
    sim = make(power=0.0)
    before = finance.equity_cents(sim)
    fuel.order(sim, 5.0)
    sim.catch_up(fuel.DELIVERY_LAG_HOURS)

    stock_value = fuel.inventory_value_cents(sim.state.fuel)
    assert stock_value > 0
    # Капитал упал только на расходы месяцев, а не на стоимость топлива.
    months = fuel.DELIVERY_LAG_HOURS / (30 * 24)
    assert finance.equity_cents(sim) > before - finance.OPEX_MONTHLY_CENTS * (months + 1)


def test_fuel_is_equivalent_across_paths():
    stepwise = make(power=0.8)
    stepwise.run(24 * 200)

    batched = make(power=0.8)
    batched.catch_up(24 * 200)

    assert_states_equivalent(
        batched.sync_state().to_dict(),
        stepwise.sync_state().to_dict(),
    )
