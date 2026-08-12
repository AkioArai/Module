"""Уборка временных файлов сохранения (persistence/save.py).

Перед атомарной записью каталог чистится от временных файлов прерванных
попыток. Опасность в том, что такой файл может принадлежать живому процессу,
который пишет прямо сейчас: удалив его, мы уронили бы чужое сохранение с ENOENT.
"""

from __future__ import annotations

from module_sim.core.state import GameState
from module_sim.persistence import paths
from module_sim.persistence import save as save_mod

#: Заведомо мёртвый процесс: значение выше типичного ``kernel.pid_max``.
DEAD_PID = 9_999_999

#: Заведомо живой и заведомо чужой: init существует всегда, и прав на него у
#: нас нет — путь, на котором проверяется трактовка PermissionError.
LIVE_FOREIGN_PID = 1


def write_save() -> None:
    save_mod.save_game(GameState(seed=1, epoch=0.0), now=1_800_000_000.0)


def temp_named(pid: int):
    save = paths.save_path()
    return save.with_name(f"{save.name}.tmp-{pid}")


def test_dead_process_leftovers_are_removed():
    leftover = temp_named(DEAD_PID)
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_bytes(b"mycop")

    write_save()

    assert not leftover.exists()


def test_live_process_leftovers_are_left_alone():
    """Файл живого процесса не наш мусор, а чужая незавершённая запись."""
    leftover = temp_named(LIVE_FOREIGN_PID)
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_bytes("чужая запись".encode())

    write_save()

    assert leftover.exists(), "удалён временный файл живого процесса"
    leftover.unlink()


def test_unparsable_leftovers_are_removed():
    """Имя без разборчивого pid — мусор из прошлых версий, его убираем."""
    save = paths.save_path()
    leftover = save.with_name(f"{save.name}.tmp-неизвестно")
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_bytes("старый формат".encode())

    write_save()

    assert not leftover.exists()


def test_save_still_works_with_leftovers_around():
    temp_named(DEAD_PID).write_bytes(b"1")
    temp_named(LIVE_FOREIGN_PID).write_bytes(b"2")

    write_save()

    assert save_mod.load_game().state.seed == 1
    temp_named(LIVE_FOREIGN_PID).unlink()
