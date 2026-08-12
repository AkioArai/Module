"""Пути по XDG (SAVEFORMAT.md, §1).

Всё вычисляется на каждый вызов, ничего не кэшируется в модуле: тесты
переопределяют ``MODULE_DATA_HOME`` через окружение, и закэшированный путь
делал бы их зависимыми от порядка запуска.
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir, user_state_dir

__all__ = [
    "APP_NAME",
    "BACKUP_RING",
    "backup_path",
    "data_home",
    "log_path",
    "meta_path",
    "runtime_dir",
    "save_path",
    "scripts_dir",
    "socket_path",
    "state_home",
]

APP_NAME = "module"

#: Глубина кольца бэкапов (BALANCE.md, §1).
BACKUP_RING = 5

#: Переопределение всех путей разом. Нужно тестам и нескольким профилям игры.
ENV_DATA_HOME = "MODULE_DATA_HOME"


def data_home() -> Path:
    """Каталог данных: сейв, метапрогресс, скрипты игрока."""
    override = os.environ.get(ENV_DATA_HOME)
    if override:
        return Path(override).expanduser()
    # platformdirs сам уважает XDG_DATA_HOME; appauthor=False убирает лишний
    # уровень вложенности, которого на Linux быть не должно.
    return Path(user_data_dir(APP_NAME, appauthor=False))


def state_home() -> Path:
    """Каталог состояния: журналы. Отделён от данных намеренно — журнал можно
    потерять без потери партии."""
    override = os.environ.get(ENV_DATA_HOME)
    if override:
        return Path(override).expanduser() / "state"
    return Path(user_state_dir(APP_NAME, appauthor=False))


def save_path() -> Path:
    return data_home() / "save.json"


def backup_path(index: int) -> Path:
    """``save.json.N``, где 1 — самый свежий бэкап."""
    if not 1 <= index <= BACKUP_RING:
        raise ValueError(f"индекс бэкапа вне 1..{BACKUP_RING}: {index}")
    save = save_path()
    return save.with_name(f"{save.name}.{index}")


def meta_path() -> Path:
    """Метапрогресс между партиями. Переживает удаление партии — в этом смысл
    файла (DESIGN.md, §3.8)."""
    return data_home() / "meta.json"


def scripts_dir() -> Path:
    """Скрипты автоматизации игрока (DESIGN.md, §7)."""
    return data_home() / "scripts"


def log_path() -> Path:
    return state_home() / "module.log"


def runtime_dir() -> Path:
    """Каталог для сокета. ``XDG_RUNTIME_DIR`` может отсутствовать (не-systemd
    сессии, контейнеры) — тогда откатываемся в каталог состояния, а не в общий
    ``/tmp``, где путь предсказуем для других пользователей."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / APP_NAME
    return state_home() / "run"


def socket_path() -> Path:
    return runtime_dir() / "console.sock"


def ensure_dirs() -> None:
    """Создать каталоги, которые нужны игре для записи."""
    data_home().mkdir(parents=True, exist_ok=True)
    state_home().mkdir(parents=True, exist_ok=True)
    scripts_dir().mkdir(parents=True, exist_ok=True)
