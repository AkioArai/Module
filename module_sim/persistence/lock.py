"""Блокировка партии: одну и ту же партию нельзя открыть дважды.

Без неё две копии игры читают один сейв, обе автосохраняются, и последняя
запись затирает прогресс первой — молча, без единого сообщения. Для проекта,
где требование номер один «никогда не терять прогресс», это самый дешёвый
способ его нарушить.

Блокировка привязана к **файлу сейва**, а не к приложению: две разные партии
(``--save`` с другим путём) должны открываться одновременно, это законный
сценарий отладки и нескольких профилей.

Механизм — ``fcntl.flock`` на отдельном файле рядом с сейвом. Свойство, ради
которого выбран именно он: блокировка снимается ядром при завершении процесса,
как бы он ни завершился. Файл с pid внутри такого не умеет — после ``kill -9``
он останется лежать и заблокирует партию навсегда. Pid внутрь всё же пишется,
но только чтобы было что показать игроку.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
from types import TracebackType

__all__ = ["SaveLock", "SaveLocked"]


class SaveLocked(RuntimeError):
    """Партия уже открыта другим процессом."""


class SaveLock:
    """Эксклюзивная блокировка одной партии.

    Используется как контекстный менеджер:

    ```python
    with SaveLock(save_path):
        ...  # партия открыта только здесь
    ```
    """

    __slots__ = ("_fd", "path")

    def __init__(self, save_path: Path) -> None:
        #: Отдельный файл, а не сам сейв: блокировать файл, который
        #: пересоздаётся при каждой атомарной записи, бессмысленно — вместе с
        #: инодом исчезнет и блокировка.
        self.path = save_path.with_name(f"{save_path.name}.lock")
        self._fd: int | None = None

    # -- захват и освобождение -------------------------------------------

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = self._read_holder(fd)
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise SaveLocked(
                    f"партия уже открыта{holder}. Закройте её или запустите другую через --save"
                ) from exc
            # Файловые системы без flock (некоторые сетевые) не повод не дать
            # играть: без блокировки рискованно, но играбельно.
            raise
        self._fd = fd
        self._write_holder()

    def release(self) -> None:
        if self._fd is None:
            return
        # Файл не удаляем: гонка между «снял блокировку» и «удалил» позволила
        # бы двум процессам держать разные иноды под одним именем и считать,
        # что каждый владеет партией.
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    # -- диагностика ------------------------------------------------------

    @staticmethod
    def _read_holder(fd: int) -> str:
        """Кто держит блокировку — для сообщения игроку, не для логики."""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            content = os.read(fd, 64).decode("utf-8", "replace").strip()
        except OSError:
            return ""
        return f" (процесс {content})" if content.isdigit() else ""

    def _write_holder(self) -> None:
        if self._fd is None:
            return
        try:
            os.ftruncate(self._fd, 0)
            os.write(self._fd, str(os.getpid()).encode())
        except OSError:
            # Не смогли записать pid — блокировка от этого не перестаёт
            # работать, потеряется только подсказка в сообщении.
            pass

    def is_held_by_us(self) -> bool:
        return self._fd is not None

    @classmethod
    def is_locked(cls, save_path: Path) -> bool:
        """Занята ли партия. Только для диагностики (``module doctor``).

        Между проверкой и захватом всегда есть гонка, поэтому решение «можно
        играть» принимает ``acquire``, а не этот метод.
        """
        probe = cls(save_path)
        try:
            probe.acquire()
        except SaveLocked:
            return True
        except OSError:
            return False
        probe.release()
        return False

    # -- контекстный менеджер --------------------------------------------

    def __enter__(self) -> SaveLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
