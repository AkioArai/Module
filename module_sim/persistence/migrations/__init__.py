"""Цепочка миграций сейва (SAVEFORMAT.md, §5).

Это код, охраняющий требование номер один: **обновление игры никогда не теряет
прогресс**.

Устройство. Каждый модуль ``vNNN`` поднимает версию ровно на единицу и работает
с сырым ``dict``. Цепочка собирается автоматическим сканированием каталога, а
её целостность (версии идут подряд, последняя совпадает с текущей) проверяется
здесь же — при импорте пакета миграций, а не при загрузке чужого сейва. Дыра в
нумерации обязана падать в тестах на машине разработчика, а не у игрока.

Три правила, которые нельзя нарушать (полностью — в SAVEFORMAT.md):

1. Выпущенную миграцию никогда не редактируют, только добавляют новую.
2. Миграция не импортирует ``core.state``: dataclass-ы меняются, а миграция
   обязана вечно работать с тем, что было в файле на её момент.
3. Миграция не имеет права упасть на данных своей версии. Отсутствующее поле —
   значение по умолчанию, а не исключение.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MigrationError",
    "Step",
    "chain",
    "migrate",
    "version_of",
]

#: Текущая версия схемы. Поднимается вместе с добавлением ``vNNN.py``.
CURRENT_SCHEMA_VERSION = 2

#: Версия сейва, в котором поля ``schema_version`` ещё не было. Такие файлы
#: писал прототип до введения версионирования; ``v001`` существует именно для
#: них. Ноль как «версия до начала времён» — единственный способ не потерять их.
UNVERSIONED = 0

_MODULE_RE = re.compile(r"^v(\d{3})$")


class MigrationError(RuntimeError):
    """Сейв не может быть приведён к текущей версии."""


@dataclass(frozen=True, slots=True)
class Step:
    """Одна ступень цепочки."""

    from_version: int
    to_version: int
    description: str
    apply: Callable[[dict], dict]


def _discover() -> list[Step]:
    steps: list[Step] = []
    for info in pkgutil.iter_modules(__path__):
        match = _MODULE_RE.match(info.name)
        if match is None:
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        number = int(match.group(1))
        from_version = module.FROM_VERSION
        to_version = module.TO_VERSION
        if to_version != number:
            raise MigrationError(
                f"{info.name}: TO_VERSION={to_version} не совпадает с именем файла"
            )
        if to_version != from_version + 1:
            raise MigrationError(
                f"{info.name}: миграция обязана поднимать версию ровно на единицу, "
                f"а поднимает {from_version} → {to_version}"
            )
        steps.append(
            Step(
                from_version=from_version,
                to_version=to_version,
                description=getattr(module, "DESCRIPTION", ""),
                apply=module.migrate,
            )
        )
    steps.sort(key=lambda step: step.to_version)
    _validate(steps)
    return steps


def _validate(steps: list[Step]) -> None:
    """Цепочка обязана быть непрерывной и доходить ровно до текущей версии."""
    expected = UNVERSIONED
    for step in steps:
        if step.from_version != expected:
            raise MigrationError(
                f"разрыв в цепочке миграций: после версии {expected} ожидалась "
                f"миграция с неё, а найдена с {step.from_version}"
            )
        expected = step.to_version
    if expected != CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            f"цепочка миграций доходит до версии {expected}, "
            f"а CURRENT_SCHEMA_VERSION={CURRENT_SCHEMA_VERSION}"
        )


_CHAIN: list[Step] = _discover()


def chain() -> list[Step]:
    """Все ступени по возрастанию версии."""
    return list(_CHAIN)


def version_of(data: dict) -> int:
    """Версия сейва. Отсутствие поля — версия ``UNVERSIONED``, а не ошибка."""
    version = data.get("schema_version", UNVERSIONED)
    if not isinstance(version, int) or isinstance(version, bool):
        raise MigrationError(f"schema_version обязан быть целым, получено {version!r}")
    return version


def migrate(data: dict) -> tuple[dict, list[Step]]:
    """Привести сейв к текущей версии. Возвращает данные и применённые ступени.

    Сейв версии выше текущей не трогаем: это откат игры назад, и молча портить
    файл нельзя (SAVEFORMAT.md, §5, правило 6).
    """
    version = version_of(data)
    if version > CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            f"сейв версии {version} новее, чем понимает эта версия игры "
            f"({CURRENT_SCHEMA_VERSION}). Обновите игру — файл не тронут."
        )

    applied: list[Step] = []
    for step in _CHAIN:
        if step.from_version < version:
            continue
        data = step.apply(data)
        data["schema_version"] = step.to_version
        applied.append(step)
        version = step.to_version

    if version != CURRENT_SCHEMA_VERSION:
        raise MigrationError(f"после миграций версия {version}, ожидалась {CURRENT_SCHEMA_VERSION}")
    return data, applied
