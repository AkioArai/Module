"""Часы и догон пропущенного времени (инварианты И3, И6)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from module_sim.core.clock import MAX_CATCHUP_TICKS, Clock
from module_sim.core.state import GameState, Speed

EPOCH = datetime(2026, 1, 1, tzinfo=UTC).timestamp()


def make_clock(tick: int = 0, speed: str = Speed.X1) -> Clock:
    return Clock(GameState(seed=1, tick=tick, epoch=EPOCH, speed=speed))


def test_tick_is_one_game_hour():
    clock = make_clock()
    start = clock.game_datetime()
    clock.advance(1)
    assert (clock.game_datetime() - start).total_seconds() == 3600


def test_date_is_derived_not_stored():
    """Сутки тиков — ровно сутки календаря."""
    clock = make_clock(tick=24)
    assert clock.game_datetime() == datetime(2026, 1, 2, tzinfo=UTC)


def test_date_is_utc_regardless_of_machine_timezone(monkeypatch):
    """Игровая дата обязана быть функцией только сейва, а не зоны машины."""
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    assert make_clock(tick=5).game_datetime().tzinfo is UTC


def test_time_never_goes_backwards():
    with pytest.raises(ValueError):
        make_clock().advance(-1)


def test_unknown_speed_rejected():
    with pytest.raises(ValueError):
        make_clock().set_speed("x1000")


def test_catchup_one_hour_per_minute():
    """Курс догона: игровой час за реальную минуту (DESIGN.md, §Р5)."""
    clock = make_clock()
    assert clock.missed(saved_at=1000.0, now=1000.0 + 3600.0).ticks == 60


def test_a_week_of_exams_does_not_cost_decades():
    """Главное следствие §Р5, ради которого курс и менялся.

    При прежнем курсе неделя без игры превращалась в 69 игровых лет, и партия
    гарантированно приходила к банкротству, которое ещё и тащит долг дальше.
    Тест держит границу: неделя отсутствия — порядка топливной кампании, а не
    геологической эпохи.
    """
    week = 7 * 24 * 3600.0
    ticks = make_clock().missed(saved_at=0.0, now=week).ticks
    months = ticks / (24 * 30)

    assert 6 < months < 24, f"неделя отсутствия дала {months:.1f} игровых месяцев"


def test_pause_freezes_the_world():
    """Сохранился на паузе — вернулся в тот же мир."""
    clock = make_clock(speed=Speed.PAUSED)
    assert clock.missed(saved_at=0.0, now=86_400.0).ticks == 0


def test_clock_backwards_is_not_a_rollback():
    """Часы машины ушли назад — это ноль тиков, а не откат симуляции."""
    clock = make_clock()
    assert clock.missed(saved_at=5000.0, now=1000.0).ticks == 0


def test_catchup_is_capped():
    """Сломанные системные часы не превращают партию в мусор молча."""
    clock = make_clock()
    absurd = clock.missed(saved_at=0.0, now=1e12)
    assert absurd.ticks == MAX_CATCHUP_TICKS


def test_capping_is_reported_not_silent():
    """Упор в потолок обязан быть виден: чаще всего это сбитые системные часы,
    и игрок должен понимать, почему партия скакнула на век вперёд."""
    capped = make_clock().missed(saved_at=0.0, now=1e12)
    assert capped.capped
    assert capped.dropped > 0

    normal = make_clock().missed(saved_at=0.0, now=3600.0)
    assert not normal.capped
    assert normal.dropped == 0


def test_partial_tick_is_dropped():
    """Догон начисляет целые часы; недобранный остаток пропадает.

    При курсе «час за минуту» неполной оказывается любая отлучка короче
    минуты — вышел за чаем, вернулся в тот же час.
    """
    clock = make_clock()
    assert clock.missed(saved_at=0.0, now=59.0).ticks == 0
    assert clock.missed(saved_at=0.0, now=90.0).ticks == 1


def test_clock_state_lives_in_game_state():
    """У часов нет собственного состояния — иначе оно не попадёт в сейв."""
    state = GameState(seed=1, epoch=EPOCH)
    clock = Clock(state)
    clock.advance(10)
    clock.set_speed(Speed.X50)
    assert state.tick == 10
    assert state.speed == Speed.X50
