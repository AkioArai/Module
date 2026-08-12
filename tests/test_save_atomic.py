"""Атомарность записи и восстановление партии.

Проверяется одно обещание: **битого сейва получить нельзя**, а партия не
теряется молча. Тесты имитируют обрыв в каждой точке записи и порчу файлов на
диске.
"""

from __future__ import annotations

import os

import pytest

from module_sim.core.sim import Simulation
from module_sim.core.state import GameState
from module_sim.persistence import paths
from module_sim.persistence import save as save_mod
from module_sim.persistence.save import MetaState, SaveError

EPOCH = 1_767_225_600.0


def make_state(tick: int = 0, cash: int = 1_000_000) -> GameState:
    state = GameState(seed=7, tick=tick, epoch=EPOCH)
    state.company.cash_cents = cash
    return state


# -- базовое поведение -----------------------------------------------------


def test_round_trip():
    save_mod.save_game(make_state(tick=42, cash=123_456), now=1000.0)
    result = save_mod.load_game()
    assert result.state.tick == 42
    assert result.state.company.cash_cents == 123_456
    assert result.saved_at == 1000.0
    assert result.recovered is False


def test_created_at_survives_resaving():
    """Дата создания партии не должна ползти при каждом сохранении."""
    save_mod.save_game(make_state(), now=1000.0)
    first = save_mod.load_game()
    save_mod.save_game(make_state(tick=5), now=9999.0, created_at=first.created_at)
    assert save_mod.load_game().created_at == first.created_at


def test_missing_save_reports_clearly():
    with pytest.raises(SaveError, match="не найден"):
        save_mod.load_game()


def test_has_save_sees_backups_only():
    save_mod.save_game(make_state(), now=1.0)
    save_mod.save_game(make_state(tick=1), now=2.0)
    paths.save_path().unlink()
    assert save_mod.has_save() is True


# -- атомарность -----------------------------------------------------------


def test_no_temp_files_left_behind():
    save_mod.save_game(make_state(), now=1.0)
    leftovers = list(paths.data_home().glob("save.json.tmp-*"))
    assert leftovers == []


def test_serialization_failure_does_not_touch_disk(monkeypatch):
    """Ошибка сериализации обязана случиться до первого касания файла."""
    save_mod.save_game(make_state(tick=1), now=1.0)
    before = paths.save_path().read_bytes()

    class Exploding:
        def dumps(self, data):
            raise RuntimeError("бум")

        def loads(self, raw):
            raise AssertionError("не должно вызываться")

    with pytest.raises(RuntimeError, match="бум"):
        save_mod.save_game(make_state(tick=2), now=2.0, codec=Exploding())

    assert paths.save_path().read_bytes() == before
    assert list(paths.data_home().glob("save.json.tmp-*")) == []


def test_crash_during_replace_leaves_old_file_intact(monkeypatch):
    """Обрыв ровно на подмене: на диске остаётся целиком старая версия."""
    save_mod.save_game(make_state(tick=1, cash=111), now=1.0)
    before = paths.save_path().read_bytes()

    real_replace = os.replace

    def failing_replace(src, dst):
        # Проворот кольца бэкапов тоже идёт через replace — ломаем только
        # финальную подмену самого сейва.
        if str(dst).endswith("save.json"):
            raise OSError("обрыв питания")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError, match="обрыв питания"):
        save_mod.save_game(make_state(tick=2, cash=222), now=2.0)

    # Патч не снимаем: чтение сейва через os.replace не идёт, а monkeypatch.undo()
    # снял бы заодно и изоляцию каталога данных из conftest.
    assert paths.save_path().read_bytes() == before
    assert save_mod.load_game().state.tick == 1
    assert list(paths.data_home().glob("save.json.tmp-*")) == []


def test_stale_temp_files_are_cleaned_up():
    stale = paths.data_home() / "save.json.tmp-999999"
    stale.write_bytes("мусор от прерванной записи".encode())
    save_mod.save_game(make_state(), now=1.0)
    assert not stale.exists()


def test_save_is_never_absent_between_writes():
    """Проворот кольца не создаёт окна, в котором сейва не существует.

    Кольцо провачивается жёсткой ссылкой именно ради этого; тест ловит
    возврат к переименованию.
    """
    save_mod.save_game(make_state(tick=1), now=1.0)

    observed: list[bool] = []
    real_link = os.link

    def observing_link(src, dst):
        result = real_link(src, dst)
        observed.append(paths.save_path().exists())
        return result

    original = os.link
    os.link = observing_link
    try:
        save_mod.save_game(make_state(tick=2), now=2.0)
    finally:
        os.link = original

    assert observed == [True]


# -- кольцо бэкапов --------------------------------------------------------


def test_backup_ring_rotates():
    for tick in range(1, 8):
        save_mod.save_game(make_state(tick=tick), now=float(tick))

    # .1 — предыдущее сохранение, .2 — позапрошлое, и так далее.
    assert save_mod.load_game().state.tick == 7
    for index in range(1, paths.BACKUP_RING + 1):
        backup = paths.backup_path(index)
        assert backup.exists(), f"нет бэкапа .{index}"
        assert save_mod.load_game(backup).state.tick == 7 - index


def test_backup_ring_does_not_grow():
    for tick in range(1, 20):
        save_mod.save_game(make_state(tick=tick), now=float(tick))
    extra = paths.save_path().with_name("save.json.6")
    assert not extra.exists()


def test_restore_backup():
    save_mod.save_game(make_state(tick=1), now=1.0)
    save_mod.save_game(make_state(tick=2), now=2.0)
    save_mod.restore_backup(1)
    assert save_mod.load_game().state.tick == 1


def test_restore_missing_backup_fails_loudly():
    save_mod.save_game(make_state(), now=1.0)
    with pytest.raises(SaveError, match="не найден"):
        save_mod.restore_backup(4)


def test_backup_index_out_of_range():
    with pytest.raises(ValueError):
        paths.backup_path(0)
    with pytest.raises(ValueError):
        paths.backup_path(paths.BACKUP_RING + 1)


# -- восстановление после порчи -------------------------------------------


def test_corrupted_save_falls_back_to_backup():
    save_mod.save_game(make_state(tick=1), now=1.0)
    save_mod.save_game(make_state(tick=2), now=2.0)

    paths.save_path().write_bytes(b"\x00\x01" + "не json вовсе".encode())

    result = save_mod.load_game()
    assert result.recovered is True
    assert result.state.tick == 1
    assert result.source == paths.backup_path(1)


def test_truncated_save_falls_back():
    """Обрезанный файл — то, что получилось бы без атомарной записи."""
    save_mod.save_game(make_state(tick=1), now=1.0)
    save_mod.save_game(make_state(tick=2), now=2.0)

    data = paths.save_path().read_bytes()
    paths.save_path().write_bytes(data[: len(data) // 2])

    assert save_mod.load_game().state.tick == 1


def test_all_files_corrupted_reports_every_reason():
    save_mod.save_game(make_state(tick=1), now=1.0)
    save_mod.save_game(make_state(tick=2), now=2.0)
    for path in (paths.save_path(), paths.backup_path(1)):
        path.write_bytes("мусор".encode())

    with pytest.raises(SaveError, match="не удалось прочитать"):
        save_mod.load_game()


def test_explicit_path_does_not_silently_use_backups():
    """Если путь указан явно, подмена бэкапом была бы сюрпризом."""
    save_mod.save_game(make_state(tick=1), now=1.0)
    save_mod.save_game(make_state(tick=2), now=2.0)
    paths.save_path().write_bytes("мусор".encode())

    with pytest.raises(SaveError):
        save_mod.load_game(paths.save_path())


# -- продолжение партии после загрузки ------------------------------------


def test_simulation_continues_after_load():
    sim = Simulation.new_game(seed=5, epoch=EPOCH)
    sim.run(100)
    save_mod.save_game(sim.sync_state(), now=1.0)

    restored = Simulation(save_mod.load_game().state)
    restored.run(50)

    reference = Simulation.new_game(seed=5, epoch=EPOCH)
    reference.run(150)

    assert restored.sync_state().to_dict() == reference.sync_state().to_dict()


# -- метапрогресс ----------------------------------------------------------


def test_meta_round_trip():
    save_mod.save_meta(MetaState(carried_debt_cents=-500_000, bankruptcies=2, games_played=3))
    meta = save_mod.load_meta()
    assert meta.carried_debt_cents == -500_000
    assert meta.bankruptcies == 2
    assert meta.games_played == 3


def test_missing_meta_is_first_game():
    assert save_mod.load_meta() == MetaState()


def test_broken_meta_does_not_kill_the_game():
    """Метапрогресс ценен, но партию терять из-за него нельзя."""
    paths.meta_path().write_bytes("не json".encode())
    assert save_mod.load_meta() == MetaState()


def test_meta_survives_deleted_save():
    save_mod.save_meta(MetaState(carried_debt_cents=-42))
    save_mod.save_game(make_state(), now=1.0)
    paths.save_path().unlink()
    for index in range(1, paths.BACKUP_RING + 1):
        paths.backup_path(index).unlink(missing_ok=True)

    assert save_mod.load_meta().carried_debt_cents == -42
