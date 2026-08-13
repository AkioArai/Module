"""Договор PPA: цена, срок, окончание (economy/contracts.py).

Экономику договора — выручку, недопоставку и штрафы — проверяет
``test_market.py``: она живёт в интегрировании. Здесь проверяется сам договор
как объект партии: на каких условиях его дают, когда он кончается и что он
переживает выход из игры.
"""

from __future__ import annotations

import pytest

from module_sim.core.economy import contracts, prices
from module_sim.core.sim import Simulation
from module_sim.core.state import GameState, PpaContract

EPOCH = 1_767_225_600.0


def make(seed: int = 4242) -> Simulation:
    return Simulation.new_game(seed=seed, epoch=EPOCH)


def test_price_is_discounted_to_the_market():
    """Цена договора ниже средней рыночной: риск берёт на себя покупатель."""
    sim = make()
    market = sim.state.market
    fair = prices.fair_price_cents(market.noise, market.demand_deviation)
    assert contracts.offered_price_cents(sim) == round(fair * contracts.PPA_DISCOUNT_TO_SPOT)
    assert contracts.offered_price_cents(sim) < fair


def test_price_does_not_depend_on_the_season():
    """Подписать в январе годовой договор по зимней цене нельзя.

    Иначе выбор «когда подписывать» решался бы календарём, а не рынком, и
    перестал бы быть выбором вовсе.
    """
    winter = make()
    summer = make()
    summer.catch_up(24 * 180)
    summer.state.market.noise = winter.state.market.noise
    summer.state.market.demand_deviation = winter.state.market.demand_deviation

    assert contracts.offered_price_cents(summer) == contracts.offered_price_cents(winter)


def test_price_does_depend_on_todays_market():
    """Контроль: то, чего контрагент не знает, торговать всё-таки можно."""
    calm = make()
    hot = make()
    hot.state.market.noise = 0.3
    assert contracts.offered_price_cents(hot) > contracts.offered_price_cents(calm)


def test_signing_records_the_contract():
    sim = make()
    contract = contracts.sign_ppa(sim, volume_mwh=400.0, months=6)

    assert sim.state.market.ppa is contract
    assert contract.volume_mwh == 400.0
    assert contract.signed_tick == 0
    assert contract.price_cents > 0


def test_term_ends_on_a_day_boundary():
    """Договор кончается на границе суток — там, где рынок и так пересчитывает
    всё (economy/market.py)."""
    sim = make()
    sim.run(7)  # подписываем посреди суток
    contract = contracts.sign_ppa(sim, volume_mwh=100.0, months=1)

    assert contract.ends_tick % 24 == 0
    assert contract.ends_tick >= sim.state.tick + 30 * 24


def test_only_one_contract_at_a_time():
    sim = make()
    contracts.sign_ppa(sim, volume_mwh=100.0)
    with pytest.raises(contracts.ContractError, match="уже есть"):
        contracts.sign_ppa(sim, volume_mwh=100.0)


@pytest.mark.parametrize("volume", [0.0, -10.0, 1_500.0])
def test_impossible_volumes_are_refused(volume):
    """Объём больше номинала — не хедж, а гарантированный штраф."""
    sim = make()
    with pytest.raises(contracts.ContractError):
        contracts.sign_ppa(sim, volume_mwh=volume)


def test_zero_term_is_refused():
    sim = make()
    with pytest.raises(contracts.ContractError):
        contracts.sign_ppa(sim, volume_mwh=100.0, months=0)


def test_contract_ends_by_itself():
    sim = make()
    contract = contracts.sign_ppa(sim, volume_mwh=100.0, months=1)

    sim.catch_up(contract.ends_tick - 1)
    assert sim.state.market.ppa is not None

    sim.catch_up(1)
    assert sim.state.market.ppa is None


def test_remaining_hours_counts_down():
    sim = make()
    contract = contracts.sign_ppa(sim, volume_mwh=100.0, months=1)
    assert contracts.remaining_hours(sim) == contract.ends_tick

    sim.run(24)
    assert contracts.remaining_hours(sim) == contract.ends_tick - 24


def test_contract_survives_save_and_still_expires():
    """Договор — часть партии, а его окончание — событие в очереди. Оба обязаны
    пережить выход из игры."""
    sim = make()
    contract = contracts.sign_ppa(sim, volume_mwh=100.0, months=1)
    sim.run(48)

    reloaded = Simulation(GameState.from_dict(sim.sync_state().to_dict()))
    assert reloaded.state.market.ppa is not None
    assert reloaded.state.market.ppa.price_cents == contract.price_cents

    reloaded.catch_up(contract.ends_tick - reloaded.state.tick)
    assert reloaded.state.market.ppa is None


def test_expiry_does_not_touch_a_different_contract():
    """Событие окончания сверяется с договором по времени подписания.

    Сегодня перезаключить договор нельзя, но событие от старого может дожить в
    очереди мигрированного сейва. Молча снять чужой договор — худшее, что тут
    можно сделать, и проверить это дешевле, чем однажды объяснять игроку.
    """
    sim = make()
    old = contracts.sign_ppa(sim, volume_mwh=100.0, months=1)
    sim.run(24)

    # Подменяем договор так, как это сделало бы перезаключение: другой момент
    # подписания, то же место в состоянии.
    sim.state.market.ppa = PpaContract(
        volume_mwh=200.0,
        price_cents=old.price_cents,
        signed_tick=sim.state.tick,
        ends_tick=old.ends_tick + 24 * 30,
    )

    sim.catch_up(old.ends_tick - sim.state.tick)

    assert sim.state.market.ppa is not None
    assert sim.state.market.ppa.volume_mwh == 200.0
