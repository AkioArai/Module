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

from module_sim.core.events import registry
from module_sim.core.sim import MAX_EVENT_CASCADE, Simulation
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


def test_idle_game_is_exact():
    """Остановленная станция обязана совпадать точно.

    Ноль эволюционирует в ноль без всякой погрешности, и если этот тест
    когда-нибудь упадёт — значит завелась величина, которая шевелится сама по
    себе, без причины в состоянии мира (И8).
    """
    stepwise = make_sim()
    stepwise.run(1000)

    batched = make_sim()
    batched.catch_up(1000)

    assert_states_exact(snapshot(batched), snapshot(stepwise))


def test_running_reactor_is_where_the_tolerance_starts_working():
    """Первая настоящая проверка И4а: работающий блок разъезжает выгорание.

    Именно ради этого случая инвариант расщепляли. Разбиение на блоки всегда
    разное — оно задаётся моментами событий, — и накопленное выгорание в двух
    путях отличается в последних разрядах. Требовать здесь точного равенства
    было бы требованием, которое невозможно выполнить.

    А вот касса обязана совпасть **точно**: она меняется только в дискретном
    событии расчёта, и округление происходит на одних и тех же тиках
    (economy/market.py).
    """
    stepwise = make_sim()
    stepwise.state.reactor.power_setpoint = 1.0
    stepwise.run(2000)

    batched = make_sim()
    batched.state.reactor.power_setpoint = 1.0
    batched.catch_up(2000)

    assert_states_equivalent(snapshot(batched), snapshot(stepwise))
    assert batched.state.company.cash_cents == stepwise.state.company.cash_cents
    assert batched.state.reactor.burnup != stepwise.state.reactor.burnup, (
        "выгорание совпало точно — либо реактор не работал, либо вернулась "
        "формула, не зависящая от разбиения; тогда допуск больше ничего не стережёт"
    )


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


# -- эквивалентность на реально срабатывающих событиях ---------------------
#
# Всё выше проверяет эквивалентность на пустой симуляции, где батч сводится к
# сложению счётчиков. Настоящая проверка начинается здесь: события обязаны
# срабатывать одинаково, когда время идёт по часу и когда оно прыгает через
# недели. Ошибка в планировщике проявилась бы именно тут — и только тут.

#: Интервал между срабатываниями. Нарочно не делитель суток и не делитель
#: горизонтов из тестов: ровный период спрятал бы ошибку на границе блока.
TICKER_INTERVAL = 37


def register_ticker() -> None:
    """Подсистема-образец: меняет деньги, тянет случайность, ставит себя снова.

    Три свойства, которые есть у любой настоящей подсистемы фаз 2+: побочный
    эффект на состояние, обращение к своему потоку RNG и перепланирование.
    """

    @registry.handler("test.ticker")
    def _ticker(sim: Simulation, event) -> None:
        payload_step = event.payload.get("step", 0)
        sim.state.company.cash_cents += 1_000 + payload_step
        sim.state.company.cash_cents += sim.rng.stream("test.ticker").randint(1, 6)
        sim.schedule_in(TICKER_INTERVAL, "test.ticker", {"step": payload_step + 1})


def make_ticking_sim(seed: int = 4242) -> Simulation:
    sim = make_sim(seed)
    sim.schedule_in(TICKER_INTERVAL, "test.ticker", {"step": 0})
    return sim


@pytest.mark.parametrize("ticks", [1, 37, 38, 100, 1_000, 8_760])
def test_events_fire_identically_stepwise_and_batched(ticks):
    register_ticker()

    stepwise = make_ticking_sim()
    stepwise.run(ticks)

    batched = make_ticking_sim()
    batched.catch_up(ticks)

    assert_states_equivalent(snapshot(batched), snapshot(stepwise))


def test_the_equivalence_check_is_not_vacuous():
    """Контроль: события действительно срабатывают и меняют состояние.

    Без этого предыдущий тест мог бы сравнивать две партии, в которых не
    произошло ничего, и радостно проходить.
    """
    register_ticker()
    sim = make_ticking_sim()
    before = sim.state.company.cash_cents
    sim.catch_up(1_000)

    assert sim.state.company.cash_cents != before
    assert sim.rng.stream("test.ticker").counter == 1_000 // TICKER_INTERVAL


def test_event_exactly_at_the_target_tick_fires():
    """Граница: событие на последнем тике интервала обязано сработать в нём,
    а не остаться на следующий запуск."""
    register_ticker()
    sim = make_sim()
    sim.schedule_in(10, "test.ticker", {"step": 0})
    before = sim.state.company.cash_cents

    sim.catch_up(10)

    assert sim.state.company.cash_cents != before


def test_event_one_tick_later_does_not_fire_early():
    register_ticker()
    sim = make_sim()
    sim.schedule_in(11, "test.ticker", {"step": 0})
    before = sim.state.company.cash_cents

    sim.catch_up(10)

    assert sim.state.company.cash_cents == before


def test_queue_survives_save_and_the_event_still_fires():
    """Запланированное переживает выход из игры.

    Иначе ремонт, поставка топлива и проверка надзора тихо исчезали бы при
    каждом закрытии терминала.
    """
    register_ticker()
    sim = make_ticking_sim()
    sim.run(10)

    reloaded = Simulation(GameState.from_dict(snapshot(sim)))
    before = reloaded.state.company.cash_cents
    reloaded.catch_up(TICKER_INTERVAL)

    assert reloaded.state.company.cash_cents != before


def test_unknown_event_kind_is_skipped_not_fatal():
    """Вид события из чужой версии не имеет права ронять партию (registry.py)."""
    sim = make_sim()
    sim.schedule_in(5, "removed.in.some.future.version", {})

    sim.catch_up(10)

    assert sim.unknown_events == ["removed.in.some.future.version"]
    assert sim.state.tick == 10


def test_event_cascade_is_stopped():
    """Обработчик, бесконечно ставящий себя на тот же тик, обязан упасть.

    Зависший наглухо процесс хуже исключения: игрок не поймёт, что случилось,
    и потеряет несохранённое.
    """

    @registry.handler("test.loop")
    def _loop(sim: Simulation, event) -> None:
        sim.schedule(sim.state.tick, "test.loop")

    sim = make_sim()
    sim.schedule_in(1, "test.loop")

    with pytest.raises(RuntimeError, match="каскад"):
        sim.catch_up(5)


def test_cascade_guard_does_not_trip_on_honest_work():
    """Много событий на одном тике — это нормально, если каскад конечен."""

    @registry.handler("test.burst")
    def _burst(sim: Simulation, event) -> None:
        remaining = event.payload.get("remaining", 0)
        if remaining:
            sim.schedule(sim.state.tick, "test.burst", {"remaining": remaining - 1})

    sim = make_sim()
    sim.schedule_in(1, "test.burst", {"remaining": MAX_EVENT_CASCADE // 2})
    sim.catch_up(2)

    assert sim.state.tick == 2
