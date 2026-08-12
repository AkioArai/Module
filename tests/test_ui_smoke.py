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

from module_sim.core.sim import Simulation
from module_sim.core.state import Speed
from module_sim.persistence import paths
from module_sim.persistence import save as save_mod
from module_sim.ui.app import ModuleApp
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
            assert "Пусто" in str(app.query_one("#placeholder").render())

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
