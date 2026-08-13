"""Рынок: почасовая цена, расчёты, договор PPA (economy/market.py).

Тесты этого файла стерегут два разных обещания.

**Про деньги.** Выручка начисляется дробной, а платится целой, и место
округления выбрано так, чтобы касса батчевого и потикового пути совпадала
точно (И4а, уровень 2).

**Про цену.** Она обязана честно ходить по часам — и при этом не превращать
догон в потиковый цикл. Отсюда главный тест файла,
``test_accrual_matches_hour_by_hour_reference``: замкнутый интеграл сверяется с
независимым посчётом по часам. Без такой сверки все остальные тесты проверяли
бы, что код согласован сам с собой, а не что он считает верно.
"""

from __future__ import annotations

import time

import pytest
from support.state_compare import assert_states_equivalent, relative_difference

from module_sim.core.economy import contracts, market, prices
from module_sim.core.sim import Simulation

EPOCH = 1_767_225_600.0  # 01.01.2026 UTC


def make(power: float = 1.0, seed: int = 4242) -> Simulation:
    sim = Simulation.new_game(seed=seed, epoch=EPOCH)
    sim.state.reactor.power_setpoint = power
    return sim


# -- расчёты ---------------------------------------------------------------


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
    равно причитающемуся по всей партии, а не сумме округлений по периодам.
    """
    sim = make()
    for _ in range(10):
        sim.run(24)
        assert sim.state.market.revenue_paid_cents == market.amount_due_cents(sim.state.market)


def test_rounding_error_does_not_accumulate():
    """Сто периодов подряд не уводят кассу от причитающегося."""
    sim = make(power=0.317)  # некруглая мощность — некруглая выручка
    sim.run(24 * 100)
    assert sim.state.market.revenue_paid_cents == market.amount_due_cents(sim.state.market)


def test_cash_is_identical_stepwise_and_batched():
    """Ключевая гарантия: деньги совпадают **точно**, а не в пределах допуска.

    Расчёт привязан к дискретному событию, поэтому округление происходит на
    одних и тех же тиках в обоих путях — даже теперь, когда цена под интегралом
    меняется каждый час.
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


# -- цена ------------------------------------------------------------------


def test_price_has_an_evening_peak():
    """Вечерний пик дороже ночи — иначе суточного профиля просто нет."""
    sim = make()
    night = market.current_price_cents(sim)  # час 0
    sim.run(18)
    evening = market.current_price_cents(sim)  # час 18, вечерний максимум
    assert evening > night * 1.5


def test_randomness_is_drawn_once_a_day_not_once_an_hour():
    """Ловушка фазы, пойманная в лоб.

    Случайная часть цены обязана обновляться дискретным событием раз в сутки.
    Разыгрывай её игра каждый час — догон превратился бы в потиковый цикл, а
    батчевый путь взял бы другие числа, чем потиковый, и И4а рухнул бы вместе
    с И4. Счётчик потока — самый прямой способ это увидеть: ``normal()`` тратит
    ровно два значения, значит за десять суток их должно уйти двадцать.
    """
    sim = make()
    sim.run(24 * 10)
    assert sim.rng.stream(market.NOISE_STREAM).counter == 10 * 2
    assert sim.rng.stream(market.DEMAND_STREAM).counter == 10 * 2


def test_noise_holds_still_inside_the_day():
    sim = make()
    sim.run(1)
    noise = sim.state.market.noise
    sim.run(22)
    assert sim.state.market.noise == noise
    sim.run(1)  # граница суток
    assert sim.state.market.noise != noise


def test_accrual_matches_hour_by_hour_reference():
    """Замкнутый интеграл против честного посчёта по часам.

    Независимая проверка: цена берётся функцией часа, энергия — тем, что за
    этот час отпустил реактор, и никакой префикс-арифметики. Расхождение здесь
    означало бы, что быстрая формула считает не то, что медленная, — и никакие
    тесты на эквивалентность батча этого бы не поймали, потому что они сверяют
    быструю формулу с ней же самой.

    Блок в интервале разгоняется, так что проверяется и наклонный участок.
    """
    sim = make(power=1.0)
    reference = 0.0

    for _ in range(24 * 3):
        # Ноль тиков — это «дать сработать событиям текущего тика, не двигая
        # время». Без этого на границе суток тест взял бы цену от шума, который
        # разыгран уже **после** оплаченного часа: событие расчёта срабатывает
        # в начале тика, а час считается тем, что осталось после него.
        sim.advance(0)
        tick = sim.state.tick
        price = prices.spot_price_cents(
            tick, sim.state.market.noise, sim.state.market.demand_deviation
        )
        before = sim.state.market.energy_sold_mwh
        sim.run(1)
        reference += price * (sim.state.market.energy_sold_mwh - before)

    assert relative_difference(sim.state.market.spot_accrued_cents, reference) < 1e-12


def test_price_shape_actually_matters():
    """Контроль: почасовая форма не сводится к средней цене.

    Без этого предыдущий тест мог бы сойтись на плоской цене и ничего не
    проверить. Выручка за сутки на постоянной мощности обязана отличаться от
    «энергия × средняя цена базы» — профиль на то и профиль.
    """
    sim = make()
    sim.run(24 * 2)
    flat = sim.state.market.energy_sold_mwh * prices.P_BASE_CENTS
    assert relative_difference(sim.state.market.spot_accrued_cents, flat) > 0.05


# -- договор ---------------------------------------------------------------


def with_ppa(volume_mwh: float = 500.0, power: float = 1.0) -> Simulation:
    sim = make(power=power, seed=777)
    contracts.sign_ppa(sim, volume_mwh=volume_mwh)
    return sim


def test_contract_volume_is_paid_by_the_contract_not_the_spot():
    """Объём договора уходит по фиксированной цене, излишек — на спот."""
    sim = with_ppa(volume_mwh=500.0)
    sim.run(24 * 10)
    contract = sim.state.market.ppa
    assert contract is not None

    # Мощность выше объёма почти всё время, значит договор выбран полностью.
    hours_at_power = 24 * 10 - 10  # первые десять часов блок разгоняется
    expected = contract.volume_mwh * hours_at_power * contract.price_cents
    assert sim.state.market.ppa_accrued_cents > expected * 0.99
    assert sim.state.market.spot_accrued_cents > 0.0


def test_stopped_block_still_owes_the_contract():
    """Простой под договором — прямой убыток, а не просто отсутствие выручки.

    Это и есть цена хеджа: обязательство поставлять не знает, что блок стоит.
    """
    hedged = with_ppa(volume_mwh=500.0, power=0.0)
    hedged.run(24 * 10)

    bare = make(power=0.0, seed=777)
    bare.run(24 * 10)

    assert hedged.state.market.penalty_accrued_cents > 0.0
    assert hedged.state.company.cash_cents < bare.state.company.cash_cents


def test_penalty_is_zero_while_the_contract_is_covered():
    sim = with_ppa(volume_mwh=200.0)
    sim.run(24 * 10)
    # Первые часы разгона недопоставка есть, дальше — нет: проверяем, что она
    # перестала расти, а не что её не было вовсе.
    missed = sim.state.market.ppa_shortfall_mwh
    sim.run(24 * 10)
    assert sim.state.market.ppa_shortfall_mwh == missed


def test_contract_expires_by_itself():
    sim = with_ppa()
    contract = sim.state.market.ppa
    assert contract is not None
    sim.catch_up(contract.ends_tick - sim.state.tick)
    assert sim.state.market.ppa is None


def test_contract_equivalence_across_paths():
    """Договор не ломает И4а: min(выработка, объём) разрезает блок в тех же
    точках, в каких его разрезал бы потиковый прогон."""
    stepwise = with_ppa(volume_mwh=600.0, power=0.55)
    stepwise.run(24 * 30)

    batched = with_ppa(volume_mwh=600.0, power=0.55)
    batched.catch_up(24 * 30)

    assert_states_equivalent(
        batched.sync_state().to_dict(),
        stepwise.sync_state().to_dict(),
    )
    assert batched.state.company.cash_cents == stepwise.state.company.cash_cents


@pytest.mark.slow
def test_catchup_with_a_live_market_meets_speed_target():
    """Бенчмарк догона на работающей станции с договором (BALANCE.md, §1).

    Отдельно от ``test_sim.py``: там станция стоит, и рынок сразу выходит из
    начисления. Здесь считается всё — цена по часам, договор, штрафы, — и
    именно этот путь обязан держать 200 000 тиков/с. Упадёт этот тест —
    значит в блок пробрался цикл по часам.
    """
    ticks = 10 * 365 * 24
    sim = with_ppa(volume_mwh=400.0)

    started = time.perf_counter()
    sim.catch_up(ticks)
    elapsed = time.perf_counter() - started

    rate = ticks / elapsed if elapsed > 0 else float("inf")
    assert rate >= 200_000, f"догон {rate:,.0f} тиков/с, требуется не меньше 200 000"
