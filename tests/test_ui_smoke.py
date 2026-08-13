"""Дымовой тест интерфейса: приложение поднимается и реагирует на клавиши.

Проверяется каркас, а не внешний вид: экран рисуется, панель времени
показывает дату из сейва, скорость переключается, выход сохраняет партию.

Тест живёт отдельно от тестов геймплея намеренно — инвариант И1 требует, чтобы
симуляция проверялась без Textual, и этот файл единственный, где Textual нужен.
``asyncio.run`` вместо pytest-asyncio: лишняя зависимость сверх официальных
репозиториев Arch того не стоит (CLAUDE.md, §Зависимости).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from module_sim.core import orders
from module_sim.core.economy import contracts, fuel
from module_sim.core.sim import Simulation
from module_sim.core.state import Speed
from module_sim.persistence import paths
from module_sim.persistence import save as save_mod
from module_sim.ui.app import MAX_FRAME_ELAPSED_S, MAX_TICKS_PER_FRAME, ModuleApp
from module_sim.ui.widgets.station_panel import StationPanel
from module_sim.ui.widgets.time_panel import TimePanel

EPOCH = 1_767_225_600.0  # 01.01.2026 UTC


def make_app(notice: str = "") -> tuple[ModuleApp, Simulation]:
    sim = Simulation.new_game(seed=1, epoch=EPOCH)
    return ModuleApp(sim, created_at=EPOCH, notice=notice), sim


def run(coro):
    return asyncio.run(coro)


def test_app_starts_and_shows_game_date():
    app, _ = make_app()

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            panel = app.query_one(TimePanel)
            assert panel.game_date == "01.01.2026 00:00"
            assert panel.speed == Speed.X1
            assert "Блок" in str(app.query_one(StationPanel).render())

    run(scenario())


def test_notice_is_shown_to_the_player():
    """Догон и восстановление из бэкапа обязаны быть видны, а не молчаливы."""
    app, _ = make_app(notice="Досчитано 3 ч (3 тиков).")

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            assert "Досчитано" in str(app.query_one("#notice").render())

    run(scenario())


def test_speed_keys():
    app, sim = make_app()

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("space")
            assert sim.state.speed == Speed.PAUSED
            await pilot.press("3")
            assert sim.state.speed == Speed.X50
            await pilot.press("1")
            assert sim.state.speed == Speed.X1
            await pilot.press("space")
            assert sim.state.speed == Speed.PAUSED

    run(scenario())


def test_manual_save_writes_file():
    app, _ = make_app()

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("s")
            await pilot.pause()

    run(scenario())
    assert save_mod.load_game().state.seed == 1


def test_quit_saves_the_game():
    """Выход обязан оставить партию на диске: после закрытия игра не работает."""
    app, sim = make_app()

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            sim.run(5)
            await pilot.press("q")
            await pilot.pause()

    run(scenario())
    assert paths.save_path().exists()
    assert save_mod.load_game().state.tick >= 5


def test_time_advances_while_running():
    """Часы идут от кадров UI, но сама симуляция от них не зависит (И3)."""
    app, sim = make_app()

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            app.action_speed(Speed.X50)
            # Кадр считает тики из прошедшего реального времени; подменяем
            # долг напрямую, чтобы тест не зависел от планировщика.
            app._tick_debt = 10.9
            app._on_frame()
            await pilot.pause()

    run(scenario())
    assert sim.state.tick >= 10


# -- потолок работы на кадр ------------------------------------------------
#
# Кадр может прийти сильно позже назначенного: своп, приостановка процесса,
# тяжёлый сосед. Без потолка накопленный интервал превратился бы в сотни тиков,
# посчитанных синхронно в цикле событий, и окно бы замерло (И7).


def test_long_stall_does_not_simulate_proportionally():
    """Затык на минуту не должен считать час игрового времени в одном кадре.

    Приложение не поднимается: нужен ровно один кадр, а живой таймер добавлял
    бы свои и делал тест плавающим.
    """
    app, sim = make_app()
    app.simulation.clock.set_speed(Speed.X50)

    # Кадр «опоздал» на минуту реального времени.
    app._frame_at = time.monotonic() - 60.0
    app._on_frame()

    # На 50x минута дала бы 3000 тиков; потолок обрезает до секунды.
    assert sim.state.tick <= int(MAX_FRAME_ELAPSED_S * 50) + 1


def _count_paths(monkeypatch) -> dict[str, int]:
    """Посчитать, каким путём кадр двигал время.

    Подмена идёт на классе, а не на экземпляре: у ``Simulation`` есть
    ``__slots__``, и методы на объекте не переопределяются.
    """
    calls = {"run": 0, "catch_up": 0}
    monkeypatch.setattr(
        Simulation, "run", lambda self, n: calls.__setitem__("run", calls["run"] + n)
    )
    monkeypatch.setattr(
        Simulation, "catch_up", lambda self, n: calls.__setitem__("catch_up", calls["catch_up"] + n)
    )
    return calls


def test_short_interval_goes_tick_by_tick(monkeypatch):
    """Пока часов немного, игрок видит каждый: считаем потиково."""
    app, _ = make_app()
    calls = _count_paths(monkeypatch)

    app._tick_debt = float(MAX_TICKS_PER_FRAME)
    app._frame_at = time.monotonic()
    app._on_frame()

    assert calls["run"] >= MAX_TICKS_PER_FRAME
    assert calls["catch_up"] == 0


def test_large_interval_goes_through_the_batch_path(monkeypatch):
    """Большой остаток считается батчем, а не линейно по часам."""
    app, _ = make_app()
    calls = _count_paths(monkeypatch)

    app._tick_debt = float(MAX_TICKS_PER_FRAME * 4)
    app._frame_at = time.monotonic()
    app._on_frame()

    assert calls["catch_up"] > 0
    assert calls["run"] == 0


def test_speed_change_does_not_spill_accumulated_debt():
    """Долг тиков накоплен по прежнему множителю; переносить его нельзя."""
    app, _ = make_app()

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            app._tick_debt = 0.99
            app.action_speed(Speed.X50)
            assert app._tick_debt == 0.0

    run(scenario())


# -- управление станцией ---------------------------------------------------


def test_power_keys_move_the_setpoint():
    """Уставка меняется мгновенно, мощность идёт к ней сама (И7)."""
    app, sim = make_app()

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("up")
            await pilot.press("up")
            assert sim.state.reactor.power_setpoint == pytest.approx(0.2)
            await pilot.press("down")
            assert sim.state.reactor.power_setpoint == pytest.approx(0.1)

    run(scenario())


def test_setpoint_stays_within_bounds():
    app, sim = make_app()

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            for _ in range(15):
                await pilot.press("down")
            assert sim.state.reactor.power_setpoint == 0.0
            for _ in range(15):
                await pilot.press("up")
            assert sim.state.reactor.power_setpoint == pytest.approx(1.0)

    run(scenario())


def test_order_key_issues_an_order_and_reports_it():
    app, sim = make_app()

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.press("r")
            await pilot.pause()
            assert len(orders.active_orders(sim)) == 1
            assert "Приказ" in str(app.query_one("#notice").render())

    run(scenario())


def test_station_panel_shows_running_block():
    app, sim = make_app()

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            sim.state.reactor.power_setpoint = 1.0
            sim.run(24)
            app._refresh_panel()
            await pilot.pause()
            text = str(app.query_one(StationPanel).render())
            assert "МВт" in text
            assert "Касса" in text

    run(scenario())


def test_station_panel_shows_the_market():
    """Цена, договор и запас топлива обязаны быть на виду.

    Не для красоты: недопоставка по договору в дорогой час стоит дороже, и
    параметр, по которому игрок мог это предвидеть, показывают **до** события,
    а не в журнале после (И8).
    """
    app, sim = make_app()

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            sim.state.reactor.power_setpoint = 1.0
            contracts.sign_ppa(sim, volume_mwh=500.0)
            fuel.order(sim, 4.0)
            sim.run(24)
            app._refresh_panel()
            await pilot.pause()

            text = str(app.query_one(StationPanel).render())
            assert "₽/МВт·ч" in text
            assert "договор: 500 МВт·ч/ч" in text
            assert "в пути 4.0 т" in text

    run(scenario())


def test_station_panel_says_when_there_is_no_contract():
    app, _ = make_app()

    async def scenario():
        async with app.run_test(size=(100, 24)) as pilot:
            app._refresh_panel()
            await pilot.pause()
            assert "вся выработка на спот" in str(app.query_one(StationPanel).render())

    run(scenario())
