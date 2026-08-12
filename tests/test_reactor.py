"""Реактор фазы 1 (physics/reactor.py).

Проверяется не физика — её здесь пока нет, — а замкнутость формулы на интервал.
Наивное ``мощность × часы`` прошло бы потиково и развалилось бы в батче, потому
что при наборе мощности площадь под графиком не прямоугольник. Это тот самый
класс ошибок, ради которого в И4 записано требование интегрировать замкнуто.
"""

from __future__ import annotations

import pytest
from support.state_compare import REL_TOL, relative_difference

from module_sim.core.physics import reactor
from module_sim.core.state import ReactorState

#: На 0.1 доли номинала в час блок идёт с нуля на полную за десять часов.
FULL_RAMP_HOURS = int(1.0 / reactor.RAMP_RATE_PER_HOUR)


def running(setpoint: float = 1.0, level: float = 0.0) -> ReactorState:
    return ReactorState(power_setpoint=setpoint, power_level=level)


# -- набор мощности --------------------------------------------------------


def test_power_climbs_at_the_declared_rate():
    state = running()
    reactor.advance(state, 1)
    assert state.power_level == pytest.approx(reactor.RAMP_RATE_PER_HOUR)


def test_power_stops_at_the_setpoint():
    state = running()
    reactor.advance(state, FULL_RAMP_HOURS * 3)
    assert state.power_level == pytest.approx(1.0)


def test_power_falls_back_to_zero():
    """Разгрузка идёт с той же скоростью — на этом держатся приказы (orders)."""
    state = running(setpoint=0.0, level=1.0)
    reactor.advance(state, FULL_RAMP_HOURS)
    assert state.power_level == pytest.approx(0.0)


# -- выработка -------------------------------------------------------------


def test_ramp_energy_is_the_area_under_the_curve():
    """Треугольник, а не прямоугольник: за набор до номинала снимается половина.

    Если этот тест упадёт в сторону увеличения — значит вернулось наивное
    ``level * hours``, и игра начала печатать деньги на разгоне.
    """
    state = running()
    produced = reactor.advance(state, FULL_RAMP_HOURS)
    expected = 0.5 * reactor.NOMINAL_ELECTRIC_MW * FULL_RAMP_HOURS
    assert produced == pytest.approx(expected)


def test_energy_after_the_setpoint_is_reached():
    """Излом внутри интервала: трапеция плюс прямоугольник."""
    state = running()
    hours = FULL_RAMP_HOURS * 2
    produced = reactor.advance(state, hours)
    expected = reactor.NOMINAL_ELECTRIC_MW * (0.5 * FULL_RAMP_HOURS + FULL_RAMP_HOURS)
    assert produced == pytest.approx(expected)


def test_idle_reactor_produces_nothing():
    state = running(setpoint=0.0)
    assert reactor.advance(state, 1_000) == 0.0
    assert state.burnup == 0.0


def test_zero_interval_changes_nothing():
    state = running(level=0.5)
    assert reactor.advance(state, 0) == 0.0
    assert state.power_level == 0.5


# -- замкнутость на интервал -----------------------------------------------


@pytest.mark.parametrize("split", [(1, 19), (7, 13), (10, 10), (19, 1)])
def test_one_block_equals_two_blocks(split):
    """Разбиение интервала не меняет ни мощности, ни выработки.

    Здесь и ломалась бы наивная формула: граница блока попадает на середину
    набора мощности, и прямоугольная площадь разъезжается с настоящей.
    """
    first, second = split

    whole = running()
    produced_whole = reactor.advance(whole, first + second)

    pieces = running()
    produced_pieces = reactor.advance(pieces, first) + reactor.advance(pieces, second)

    assert relative_difference(produced_whole, produced_pieces) <= REL_TOL
    assert relative_difference(whole.power_level, pieces.power_level) <= REL_TOL


def test_burnup_tracks_produced_energy():
    """Выгорание пропорционально тепловой выработке, а не времени."""
    idle = running(setpoint=0.0)
    reactor.advance(idle, 1_000)

    working = running(setpoint=1.0, level=1.0)
    reactor.advance(working, 1_000)

    assert idle.burnup == 0.0
    assert working.burnup > 0.0


def test_full_campaign_burns_the_whole_load():
    """Кампания на номинале выжигает топливо ровно на единицу (BALANCE.md §2.5)."""
    state = running(setpoint=1.0, level=1.0)
    reactor.advance(state, reactor.CAMPAIGN_HOURS)
    assert state.burnup == pytest.approx(1.0, rel=1e-9)
