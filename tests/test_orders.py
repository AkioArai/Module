"""Приказы с длительностью (core/orders.py).

Инвариант И7: игрок не ждёт внутри игры. Проверяется, что приказ отдаётся
мгновенно, исполняется в игровом времени, переживает выход из игры и корректно
доигрывается в догоне — последнее и есть причина, по которой завершение приказа
сделано событием планировщика, а не проверкой в цикле.
"""

from __future__ import annotations

import pytest

from module_sim.core import orders
from module_sim.core.sim import Simulation
from module_sim.core.state import GameState

EPOCH = 1_767_225_600.0


def make(power: float = 1.0) -> Simulation:
    sim = Simulation.new_game(seed=4242, epoch=EPOCH)
    sim.state.reactor.power_setpoint = power
    sim.state.reactor.power_level = power
    return sim


# -- выдача ----------------------------------------------------------------


def test_order_returns_immediately():
    """Отдал приказ — управление вернулось, ждать нечего (И7)."""
    sim = make()
    order = orders.issue_order(sim, "maintenance")
    assert order.status == orders.STATUS_RUNNING
    assert order.ends_tick == sim.state.tick + orders.MAINTENANCE_HOURS


def test_unknown_order_is_refused():
    with pytest.raises(ValueError, match="неизвестный вид"):
        orders.issue_order(make(), "накормить кота")


def test_shutdown_order_drops_the_setpoint_not_the_power():
    """Блок разгружается, а не глохнет: мощность падает со своей скоростью."""
    sim = make()
    orders.issue_order(sim, "refuel")
    assert sim.state.reactor.power_setpoint == 0.0
    assert sim.state.reactor.power_level == 1.0


# -- исполнение ------------------------------------------------------------


def test_order_completes_on_time():
    sim = make()
    order = orders.issue_order(sim, "maintenance")

    sim.run(orders.MAINTENANCE_HOURS - 1)
    assert orders.find_order(sim, order.id)["status"] == orders.STATUS_RUNNING

    sim.run(1)
    assert orders.find_order(sim, order.id)["status"] == orders.STATUS_DONE


def test_refuel_resets_burnup():
    sim = make()
    sim.run(24 * 60)
    assert sim.state.reactor.burnup > 0.0

    orders.issue_order(sim, "refuel")
    sim.run(orders.REFUEL_HOURS)

    assert sim.state.reactor.burnup == 0.0


def test_order_completes_the_same_way_in_catch_up():
    """Приказ, завершившийся пока игры не было, обязан завершиться так же."""
    stepwise = make()
    orders.issue_order(stepwise, "refuel")
    stepwise.run(orders.REFUEL_HOURS + 48)

    batched = make()
    orders.issue_order(batched, "refuel")
    batched.catch_up(orders.REFUEL_HOURS + 48)

    assert batched.state.reactor.burnup == stepwise.state.reactor.burnup
    assert orders.active_orders(batched) == orders.active_orders(stepwise)


# -- отмена ----------------------------------------------------------------


def test_cancelled_order_never_completes():
    sim = make()
    order = orders.issue_order(sim, "refuel")
    assert orders.cancel_order(sim, order.id)

    sim.run(orders.REFUEL_HOURS * 2)

    assert orders.find_order(sim, order.id)["status"] == orders.STATUS_CANCELLED


def test_cancelling_twice_reports_failure():
    sim = make()
    order = orders.issue_order(sim, "maintenance")
    assert orders.cancel_order(sim, order.id)
    assert not orders.cancel_order(sim, order.id)


def test_cancelling_unknown_order_is_not_an_error():
    assert not orders.cancel_order(make(), 999)


def test_cancelled_refuel_does_not_reset_burnup():
    """Прерванная перегрузка не даёт свежего топлива — иначе отмена стала бы
    способом чинить реактор бесплатно."""
    sim = make()
    sim.run(24 * 60)
    burnup = sim.state.reactor.burnup

    order = orders.issue_order(sim, "refuel")
    orders.cancel_order(sim, order.id)
    sim.run(orders.REFUEL_HOURS)

    assert sim.state.reactor.burnup > burnup


# -- сохранение ------------------------------------------------------------


def test_order_survives_the_save():
    """Иначе выход из игры отменял бы любой начатый ремонт."""
    sim = make()
    order = orders.issue_order(sim, "refuel")
    sim.run(24)

    reloaded = Simulation(GameState.from_dict(sim.sync_state().to_dict()))
    reloaded.run(orders.REFUEL_HOURS)

    assert orders.find_order(reloaded, order.id)["status"] == orders.STATUS_DONE
    assert reloaded.state.reactor.burnup == 0.0


def test_active_orders_lists_only_running():
    sim = make()
    done = orders.issue_order(sim, "maintenance")
    cancelled = orders.issue_order(sim, "refuel")
    orders.cancel_order(sim, cancelled.id)
    sim.run(orders.MAINTENANCE_HOURS)

    assert orders.find_order(sim, done.id)["status"] == orders.STATUS_DONE
    assert orders.active_orders(sim) == []
