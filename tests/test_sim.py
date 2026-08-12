"""Симуляция: детерминизм и эквивалентность батчевого догона (инварианты И4, И4а).

``test_catchup_equivalence`` — страж требования, из-за которого игру вообще
можно оставлять закрытой: догон обязан приводить партию туда же, куда честный
потиковый прогон. Он заведён в фазе 0, на пустой симуляции, специально: правило
должно существовать раньше формул, которые ему обязаны подчиняться.

Сравнение идёт через ``support.state_compare``, а не через ``==``. Пока всё
состояние дискретно, разницы нет; она появится с первой непрерывной величиной,
и тогда голое равенство стало бы невыполнимым требованием (CLAUDE.md, §И4а).
Проверку самого компаратора на непрерывной величине держит
``test_catchup_drift.py``.
"""

from __future__ import annotations

import time

import pytest
from support.state_compare import (
    assert_states_equivalent,
    assert_states_exact,
    compare_states,
)

from module_sim.core.sim import Simulation
from module_sim.core.state import GameState

EPOCH = 1_767_225_600.0  # 01.01.2026 UTC


def make_sim(seed: int = 4242) -> Simulation:
    return Simulation.new_game(seed=seed, epoch=EPOCH)


def snapshot(sim: Simulation) -> dict:
    return sim.sync_state().to_dict()


@pytest.mark.parametrize("ticks", [0, 1, 7, 100, 8760])
def test_catchup_equivalence(ticks):
    """Батч приводит партию туда же, куда потиковый прогон (И4а)."""
    stepwise = make_sim()
    stepwise.run(ticks)

    batched = make_sim()
    batched.catch_up(ticks)

    assert_states_equivalent(snapshot(batched), snapshot(stepwise))


def test_catchup_equivalence_in_pieces():
    """Разбиение на произвольные блоки тоже не меняет результата.

    Именно так работает настоящий догон: он прыгает не одним куском, а от
    события к событию. Разбиение всегда разное — оно задаётся моментами
    событий, — и потому непрерывные величины сравниваются по допуску.
    """
    stepwise = make_sim()
    stepwise.run(1000)

    batched = make_sim()
    for block in (13, 200, 1, 486, 300):
        batched.catch_up(block)

    assert_states_equivalent(snapshot(batched), snapshot(stepwise))


def test_discrete_state_is_exact_while_it_stays_discrete():
    """Пока непрерывных величин нет, догон обязан совпадать точно.

    Тест перестанет быть тавтологией ровно в тот момент, когда в состоянии
    появится первый эволюционирующий ``float`` — и тогда он должен упасть,
    заставив автора осознанно перевести проверку на допуск, а не сделать это
    по недосмотру.
    """
    stepwise = make_sim()
    stepwise.run(1000)

    batched = make_sim()
    batched.catch_up(1000)

    assert_states_exact(snapshot(batched), snapshot(stepwise))


def test_same_seed_same_world():
    a = make_sim(seed=17)
    b = make_sim(seed=17)
    a.run(500)
    b.run(500)
    assert_states_exact(snapshot(a), snapshot(b))


def test_different_seed_different_world():
    a = make_sim(seed=17)
    b = make_sim(seed=18)
    a.run(500)
    b.run(500)
    assert compare_states(snapshot(a), snapshot(b))


def test_resume_from_saved_state_continues_identically():
    """Партия, продолженная из сейва, идёт так же, как непрерывная."""
    continuous = make_sim(seed=99)
    continuous.run(300)

    interrupted = make_sim(seed=99)
    interrupted.run(120)
    restored = Simulation(GameState.from_dict(snapshot(interrupted)))
    restored.run(180)

    # Путь исполнения тот же самый, разбиения на блоки нет — ослаблять
    # сравнение здесь нечем и незачем.
    assert_states_exact(snapshot(restored), snapshot(continuous))


def test_time_never_goes_backwards():
    sim = make_sim()
    with pytest.raises(ValueError):
        sim.run(-1)
    with pytest.raises(ValueError):
        sim.catch_up(-1)


@pytest.mark.slow
def test_catchup_meets_speed_target():
    """Нижняя граница скорости догона (BALANCE.md, §1: 200 000 тиков/с).

    Проверяется путь ``catch_up``, а не ``run``: потиковый цикл на такое и не
    рассчитан, требование И4 адресовано именно батчингу. Тест помечен ``slow``
    и служит защитой от деградации — если он упадёт, значит в батч пробрался
    цикл по часам.
    """
    ticks = 10 * 365 * 24  # 10 игровых лет
    sim = make_sim()

    started = time.perf_counter()
    sim.catch_up(ticks)
    elapsed = time.perf_counter() - started

    rate = ticks / elapsed if elapsed > 0 else float("inf")
    assert rate >= 200_000, f"догон {rate:,.0f} тиков/с, требуется не меньше 200 000"
