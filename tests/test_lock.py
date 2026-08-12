"""Блокировка партии (persistence/lock.py).

Без неё две копии игры открывают один сейв, обе автосохраняются, и последняя
запись молча уносит чужой прогресс. Тесты здесь проверяют не «блокировка
берётся», а то, ради чего она вообще нужна: что второй запуск получает отказ, и
что отказ не переживает смерть процесса.
"""

from __future__ import annotations

import pytest

from module_sim.cli import main
from module_sim.persistence import paths
from module_sim.persistence.lock import SaveLock, SaveLocked


def test_second_open_of_the_same_game_is_refused():
    save = paths.save_path()
    with SaveLock(save), pytest.raises(SaveLocked):
        SaveLock(save).acquire()


def test_lock_is_released_on_exit():
    save = paths.save_path()
    with SaveLock(save):
        pass
    # Второй заход обязан пройти — иначе один запуск игры закрывал бы партию
    # навсегда до перезагрузки.
    with SaveLock(save):
        pass


def test_release_is_idempotent():
    lock = SaveLock(paths.save_path())
    lock.acquire()
    lock.release()
    lock.release()
    assert not lock.is_held_by_us()


def test_different_saves_do_not_block_each_other(tmp_path):
    """Две разные партии — законный сценарий: отладка и несколько профилей."""
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    with SaveLock(first), SaveLock(second):
        pass


def test_stale_lock_file_does_not_block_forever(tmp_path):
    """Файл, оставшийся от убитого процесса, не должен запирать партию.

    Ровно поэтому блокировка сделана на ``flock``, а не на pid-файле: ядро
    снимает её при завершении процесса, как бы он ни завершился. Здесь файл
    существует и содержит чужой pid, но никем не удерживается.
    """
    save = tmp_path / "save.json"
    stale = save.with_name("save.json.lock")
    stale.write_text("999999", encoding="utf-8")

    with SaveLock(save):
        pass


def test_lock_file_survives_release(tmp_path):
    """Файл не удаляется намеренно: гонка между снятием блокировки и удалением
    позволила бы двум процессам держать разные иноды под одним именем."""
    save = tmp_path / "save.json"
    lock = SaveLock(save)
    lock.acquire()
    lock.release()
    assert lock.path.exists()


def test_is_locked_reports_state():
    save = paths.save_path()
    assert not SaveLock.is_locked(save)
    with SaveLock(save):
        assert SaveLock.is_locked(save)
    assert not SaveLock.is_locked(save)


def test_holder_pid_is_in_the_message():
    """Сообщение обязано подсказать, кого закрывать."""
    import os

    save = paths.save_path()
    with SaveLock(save), pytest.raises(SaveLocked, match=str(os.getpid())):
        SaveLock(save).acquire()


# -- поведение игры --------------------------------------------------------


def test_game_refuses_to_start_twice(capsys):
    """Второй запуск не открывает партию, а объясняет почему."""
    with SaveLock(paths.save_path()):
        code = main(["run", "--headless"])

    assert code == 1
    assert "уже открыта" in capsys.readouterr().err


def test_game_starts_normally_when_free(capsys):
    assert main(["run", "--headless", "--new", "--seed", "1"]) == 0
    assert "Новая партия" in capsys.readouterr().out


def test_lock_is_released_after_the_game_exits():
    """Иначе вторая партия за сессию не запустилась бы."""
    assert main(["run", "--headless", "--new", "--seed", "1"]) == 0
    assert main(["run", "--headless"]) == 0
