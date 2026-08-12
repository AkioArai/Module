"""Запись и чтение сейва.

Требование номер один: **обновление игры никогда не теряет прогресс**. Отсюда
два свойства, которые этот модуль обязан давать безусловно.

**Битого файла получить нельзя.** В любой момент, включая обрыв питания,
``save.json`` — это либо целиком старая версия, либо целиком новая. Порядок
операций в ``_write_atomic`` подобран под это и менять его нельзя
(SAVEFORMAT.md, §4).

**Партия не теряется молча.** Если текущий файл не читается или не проходит
миграции, загрузка сама перебирает кольцо бэкапов и сообщает, что взяла. Пустой
экран вместо партии — недопустимый исход.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from module_sim.core.state import GameState
from module_sim.persistence import paths
from module_sim.persistence.codec import Codec, codec_for_path, default_codec
from module_sim.persistence.migrations import (
    CURRENT_SCHEMA_VERSION,
    MigrationError,
    Step,
    migrate,
)

__all__ = [
    "LoadResult",
    "MetaState",
    "SaveError",
    "load_game",
    "load_meta",
    "restore_backup",
    "save_game",
    "save_meta",
]


class SaveError(RuntimeError):
    """Сейв не удалось прочитать — ни основной файл, ни бэкапы."""


@dataclass(slots=True)
class LoadResult:
    """Что именно было загружено. Всё это идёт в журнал: игрок должен видеть,
    если игра взяла бэкап или подняла версию его партии."""

    state: GameState
    created_at: float
    saved_at: float
    source: Path
    migrated: list[Step] = field(default_factory=list)
    #: True, если основной файл не читался и партию подняли из бэкапа.
    recovered: bool = False


# --------------------------------------------------------------------------
# Атомарная запись
# --------------------------------------------------------------------------


def _fsync_dir(directory: Path) -> None:
    """Довести до диска саму операцию переименования, а не только данные.

    Без этого ``os.replace`` может пережить обрыв питания как «не было»: данные
    во временном файле есть, а запись каталога о новом имени — нет.
    """
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as exc:
        # Некоторые файловые системы не умеют fsync на каталоге. Это ослабляет
        # гарантию, но не повод терять запись, которая уже на диске.
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF):
            raise
    finally:
        os.close(fd)


def _cleanup_stale_temp(directory: Path, stem: str) -> None:
    """Подчистить временные файлы прерванных записей."""
    for leftover in directory.glob(f"{stem}.tmp-*"):
        # Чужой процесс мог писать свой временный файл прямо сейчас.
        # Уборка мусора не имеет права ломать сохранение.
        with contextlib.suppress(OSError):
            leftover.unlink()


def _rotate_backups(path: Path) -> None:
    """Провернуть кольцо: ``.4→.5``, …, ``.1→.2``, текущий → ``.1``.

    Текущий файл попадает в ``.1`` **жёсткой ссылкой**, а не переименованием.
    Переименование оставило бы окно, в котором ``save.json`` не существует
    вовсе; ссылка не копирует данные и не создаёт такого окна: после
    последующего ``os.replace`` новый файл получает имя ``save.json``, а старый
    инод остаётся жить под именем ``.1``.
    """
    ring = paths.BACKUP_RING
    for index in range(ring - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        if src.exists():
            os.replace(src, path.with_name(f"{path.name}.{index + 1}"))

    if not path.exists():
        return

    first = path.with_name(f"{path.name}.1")
    try:
        os.link(path, first)
    except OSError:
        # Файловая система без жёстких ссылок — тогда честная копия.
        shutil.copy2(path, first)


def _write_atomic(path: Path, payload: bytes, *, rotate: bool) -> None:
    """Порядок шагов зафиксирован в SAVEFORMAT.md, §4. Не менять."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_temp(directory, path.name)

    tmp = directory / f"{path.name}.tmp-{os.getpid()}"
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        if rotate:
            _rotate_backups(path)

        os.replace(tmp, path)
        _fsync_dir(directory)
    except BaseException:
        # Не оставлять мусор после неудачи — и не трогать при этом целевой
        # файл: он либо ещё старый, либо уже новый целиком.
        tmp.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# Сейв партии
# --------------------------------------------------------------------------


def _payload(state: GameState, created_at: float, saved_at: float) -> dict:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "created_at": created_at,
        "saved_at": saved_at,
        "game": state.to_dict(),
    }


def save_game(
    state: GameState,
    *,
    now: float,
    created_at: float | None = None,
    path: Path | None = None,
    codec: Codec | None = None,
) -> Path:
    """Сохранить партию.

    ``now`` приходит параметром, а не берётся из ``time.time()``: системное
    время читается ровно в двух местах игры, и оба — вне ``core`` (И2).
    Тесты подставляют его напрямую.

    Сериализация целиком выполняется до первого касания диска — ошибка в данных
    не должна портить лежащий там файл.
    """
    target = Path(path) if path is not None else paths.save_path()
    active_codec = codec if codec is not None else codec_for_path(target)
    born = created_at if created_at is not None else now
    payload = active_codec.dumps(_payload(state, born, now))
    _write_atomic(target, payload, rotate=True)
    return target


def _read_one(path: Path) -> LoadResult:
    """Прочитать конкретный файл и привести к текущей версии."""
    raw = path.read_bytes()
    data = codec_for_path(path).loads(raw)
    data, applied = migrate(data)
    return LoadResult(
        state=GameState.from_dict(data["game"]),
        created_at=data.get("created_at", 0.0),
        saved_at=data.get("saved_at", 0.0),
        source=path,
        migrated=applied,
    )


def load_game(path: Path | None = None) -> LoadResult:
    """Загрузить партию, при необходимости — из бэкапа.

    Сейв новее текущей версии игры не подменяется бэкапом: это не повреждение,
    а откат игры назад, и игроку надо сказать правду, а не втихую открыть
    старую партию (SAVEFORMAT.md, §5, правило 6).
    """
    target = Path(path) if path is not None else paths.save_path()

    candidates: list[Path] = [target]
    if path is None:
        candidates += [paths.backup_path(i) for i in range(1, paths.BACKUP_RING + 1)]

    problems: list[str] = []
    for index, candidate in enumerate(candidates):
        if not candidate.exists():
            continue
        try:
            result = _read_one(candidate)
        except MigrationError as exc:
            if "новее" in str(exc):
                raise
            problems.append(f"{candidate.name}: {exc}")
            continue
        except Exception as exc:
            problems.append(f"{candidate.name}: {type(exc).__name__}: {exc}")
            continue
        result.recovered = index > 0
        return result

    if problems:
        detail = "; ".join(problems)
        raise SaveError(f"не удалось прочитать ни сейв, ни бэкапы: {detail}")
    raise SaveError(f"сейв не найден: {target}")


def has_save(path: Path | None = None) -> bool:
    target = Path(path) if path is not None else paths.save_path()
    if target.exists():
        return True
    if path is not None:
        return False
    return any(paths.backup_path(i).exists() for i in range(1, paths.BACKUP_RING + 1))


def restore_backup(index: int = 1) -> Path:
    """Сделать бэкап ``.N`` текущим — через ту же атомарную процедуру."""
    source = paths.backup_path(index)
    if not source.exists():
        raise SaveError(f"бэкап не найден: {source}")
    target = paths.save_path()
    _write_atomic(target, source.read_bytes(), rotate=True)
    return target


# --------------------------------------------------------------------------
# Метапрогресс между партиями
# --------------------------------------------------------------------------


@dataclass(slots=True)
class MetaState:
    """Живёт дольше партии (DESIGN.md, §3.8, SAVEFORMAT.md, §1).

    Здесь лежит переходящий долг: остаток обязательств после конца партии
    становится стартовым долгом следующей. Файл никогда не удаляется вместе с
    партией — в этом весь его смысл.

    Своей цепочки миграций у ``meta.json`` пока нет: структура из трёх полей
    того не стоит. Версия проставляется с первого дня, чтобы цепочку можно было
    завести, не ломая уже лежащие файлы.
    """

    schema_version: int = 1
    carried_debt_cents: int = 0
    bankruptcies: int = 0
    games_played: int = 0

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "carried_debt_cents": self.carried_debt_cents,
            "bankruptcies": self.bankruptcies,
            "games_played": self.games_played,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MetaState:
        return cls(
            schema_version=data.get("schema_version", 1),
            carried_debt_cents=data.get("carried_debt_cents", 0),
            bankruptcies=data.get("bankruptcies", 0),
            games_played=data.get("games_played", 0),
        )


def load_meta(path: Path | None = None) -> MetaState:
    """Прочитать метапрогресс. Отсутствие файла — не ошибка: это первая партия."""
    target = Path(path) if path is not None else paths.meta_path()
    if not target.exists():
        return MetaState()
    try:
        return MetaState.from_dict(codec_for_path(target).loads(target.read_bytes()))
    except Exception:
        # Метапрогресс ценен, но партию терять из-за него нельзя. Битый
        # meta.json — это потеря переходящего долга, а не конец игры.
        return MetaState()


def save_meta(meta: MetaState, path: Path | None = None) -> Path:
    target = Path(path) if path is not None else paths.meta_path()
    payload = default_codec().dumps(meta.to_dict())
    # Кольца бэкапов у метапрогресса нет: файл маленький и переписывается
    # редко, а атомарность и здесь обязательна.
    _write_atomic(target, payload, rotate=False)
    return target
