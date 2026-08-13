"""Цепочка миграций — главный тест репозитория.

Проверяет обещание **«обновление игры никогда не теряет прогресс»**: каждый
настоящий сейв каждой версии обязан загрузиться в текущей игре и продолжить
считаться.

Тест сам находит фикстуры в ``tests/fixtures/saves/``. Добавили миграцию и
фикстуру — покрытие расширилось само; забыли фикстуру — тест это заметит.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from module_sim.core.sim import Simulation
from module_sim.core.state import GameState
from module_sim.persistence import paths
from module_sim.persistence import save as save_mod
from module_sim.persistence.migrations import (
    CURRENT_SCHEMA_VERSION,
    MigrationError,
    chain,
    migrate,
    version_of,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_SAVES = Path(__file__).resolve().parent / "fixtures" / "saves"

FIXTURES = sorted(FIXTURE_SAVES.glob("v*.json"))
FIXTURE_IDS = [path.stem for path in FIXTURES]


def load_fixture(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# -- целостность самой цепочки --------------------------------------------


def test_chain_is_contiguous():
    """Разрывов в нумерации нет, цепочка доходит ровно до текущей версии."""
    steps = chain()
    assert steps, "цепочка миграций пуста"
    expected = 0
    for step in steps:
        assert step.from_version == expected
        assert step.to_version == expected + 1
        expected = step.to_version
    assert expected == CURRENT_SCHEMA_VERSION


def test_every_step_has_a_description():
    """Описание ступени читают через год, разбираясь в чужом сейве."""
    for step in chain():
        assert step.description, f"v{step.to_version:03d} без DESCRIPTION"


def test_every_schema_version_has_a_fixture():
    """Версия без фикстуры — непроверенное обещание совместимости."""
    versions = {version_of(load_fixture(path)) for path in FIXTURES}
    missing = set(range(0, CURRENT_SCHEMA_VERSION + 1)) - versions
    assert not missing, f"нет фикстур для версий: {sorted(missing)}"


def test_current_version_fixture_exists():
    versions = {version_of(load_fixture(path)) for path in FIXTURES}
    assert CURRENT_SCHEMA_VERSION in versions


# -- прогон фикстур --------------------------------------------------------


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_fixture_migrates_to_current(fixture):
    data, applied = migrate(load_fixture(fixture))
    assert data["schema_version"] == CURRENT_SCHEMA_VERSION
    start = version_of(load_fixture(fixture))
    assert [step.to_version for step in applied] == list(
        range(start + 1, CURRENT_SCHEMA_VERSION + 1)
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_fixture_parses_into_state(fixture):
    data, _ = migrate(load_fixture(fixture))
    state = GameState.from_dict(data["game"])
    assert isinstance(state.company.cash_cents, int)
    assert state.tick >= 0


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_fixture_simulation_continues(fixture):
    """Партия обязана не просто загрузиться, а пойти дальше."""
    data, _ = migrate(load_fixture(fixture))
    sim = Simulation(GameState.from_dict(data["game"]))
    before = sim.state.tick
    sim.run(48)
    assert sim.state.tick == before + 48


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_fixture_round_trips_through_save(fixture):
    """Мигрированную партию можно сохранить и прочитать без изменений."""
    data, _ = migrate(load_fixture(fixture))
    state = GameState.from_dict(data["game"])

    save_mod.save_game(state, now=1_800_000_000.0)
    reloaded = save_mod.load_game()

    assert reloaded.state.to_dict() == state.to_dict()
    assert reloaded.migrated == []  # свежий сейв мигрировать не надо


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_fixture_loads_through_public_api(fixture):
    """Полный путь игрока: файл лежит на месте сейва, игра его открывает."""
    shutil.copy(fixture, paths.save_path())
    result = save_mod.load_game()
    assert result.state.company.name
    assert result.recovered is False


def test_migration_is_idempotent_at_current_version():
    """Повторный прогон уже актуального сейва ничего не меняет."""
    data, _ = migrate(load_fixture(FIXTURE_SAVES / "v002.json"))
    again, applied = migrate(dict(data))
    assert applied == []
    assert again == data


# -- что миграции обязаны сохранить ---------------------------------------


def test_v001_preserves_progress():
    data, _ = migrate(load_fixture(FIXTURE_SAVES / "v000.json"))
    game = data["game"]
    assert game["seed"] == 20250101
    assert game["tick"] == 720
    assert game["company"]["name"] == "Первый блок"


def test_v002_converts_money_to_integer_kopecks():
    data, _ = migrate(load_fixture(FIXTURE_SAVES / "v000.json"))
    cash = data["game"]["company"]["cash_cents"]
    assert cash == 500_000_050  # 5 000 000.50 ₽
    assert isinstance(cash, int)
    assert "balance" not in data["game"]["company"]


def test_v006_does_not_claw_back_earned_revenue():
    """Самая дорогая ошибка, которую могла бы сделать миграция фазы 2б.

    До неё выручка считалась как «энергия × фиксированная цена», после —
    из копилок начисленного, и расчёт платит **разницу** между причитающимся и
    уже выплаченным. Оставь миграция копилки нулевыми — в первом же расчёте
    разница оказалась бы равной минус всей выручке партии, и игра честно
    списала бы с компании всё, что она заработала за годы.

    Проверяется на настоящем сейве фазы 2а, а не на синтетике: в нём выручка
    накоплена по-старому.
    """
    old = load_fixture(FIXTURE_SAVES / "v005.json")
    paid = old["game"]["market"]["revenue_paid_cents"]
    assert paid > 0, "фикстура ничего не заработала — тест проверял бы пустоту"

    data, _ = migrate(old)
    sim = Simulation(GameState.from_dict(data["game"]))
    cash_before = sim.state.company.cash_cents

    # Ровно до первого суточного расчёта и чуть дальше.
    sim.catch_up(48)

    assert sim.state.company.cash_cents > cash_before - paid // 100, (
        "миграция потеряла выручку старой партии в первом же расчёте"
    )
    assert sim.state.market.revenue_paid_cents >= paid


def test_migrations_do_not_import_core_state():
    """Миграция обязана работать с сырым dict вечно (SAVEFORMAT.md, §5)."""
    migrations_dir = REPO_ROOT / "module_sim" / "persistence" / "migrations"
    for module in sorted(migrations_dir.glob("v*.py")):
        source = module.read_text(encoding="utf-8")
        assert "core.state" not in source, f"{module.name} импортирует core.state"
        assert "GameState" not in source, f"{module.name} использует GameState"


# -- границы ---------------------------------------------------------------


def test_future_version_is_refused_not_mangled():
    """Сейв из более новой игры не портим и не подменяем бэкапом."""
    data = load_fixture(FIXTURE_SAVES / "v002.json")
    data["schema_version"] = CURRENT_SCHEMA_VERSION + 5
    with pytest.raises(MigrationError, match="новее"):
        migrate(data)


def test_future_version_survives_load_attempt():
    data = load_fixture(FIXTURE_SAVES / "v002.json")
    data["schema_version"] = CURRENT_SCHEMA_VERSION + 1
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    paths.save_path().write_bytes(payload)

    with pytest.raises(MigrationError, match="новее"):
        save_mod.load_game()

    assert paths.save_path().read_bytes() == payload, "файл был изменён"


def test_non_integer_version_is_rejected():
    with pytest.raises(MigrationError, match="целым"):
        version_of({"schema_version": "2"})
