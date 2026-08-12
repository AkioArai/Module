"""Сравнение двух состояний симуляции по контракту И4а (CLAUDE.md).

Зачем отдельный модуль вместо ``assert a == b``. Догон обязан приводить партию
туда же, куда потиковый прогон, но «то же самое» имеет три разных смысла:

* **последовательность событий** — точно, это то, что игрок реально видит;
* **дискретное состояние** (целые, строки, счётчики RNG, деньги в копейках, а
  также не эволюционирующие ``float`` вроде ``epoch``) — точно;
* **непрерывные величины** (ксенон, износ, усталость) — в пределах допуска.

Третий пункт не поблажка, а арифметический факт: сложение с плавающей точкой не
ассоциативно, а границы блоков догона задаются моментами событий и потому
всегда разные. Обоснование с числами — DESIGN.md, §Р2.

Компаратор возвращает **все** расхождения сразу, а не падает на первом: при
отладке интегрирования важно видеть, одна величина разъехалась или все.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "ABS_TOL",
    "DRIFT_HORIZON_TICKS",
    "EXACT_FLOAT_PATHS",
    "EXACT_SUBTREES",
    "REL_TOL",
    "assert_states_equivalent",
    "assert_states_exact",
    "compare_states",
    "relative_difference",
]

#: BALANCE.md, §1. Продублировано здесь, а не импортировано из игры: тест,
#: берущий эталон из проверяемого кода, проверяет сам себя.
REL_TOL = 1e-9
ABS_TOL = 1e-12
DRIFT_HORIZON_TICKS = 87_600  # 10 игровых лет

#: ``float``-поля, которые не эволюционируют, а значит обязаны совпадать точно.
#: ``epoch`` задаётся при создании партии и дальше только читается: расхождение
#: в нём означает не погрешность интегрирования, а сдвиг всего календаря.
EXACT_FLOAT_PATHS: frozenset[str] = frozenset({"epoch"})

#: Поддеревья, которые целиком сравниваются точно, включая вложенные ``float``.
#: Журнал событий — уровень 1 контракта: другой набор или порядок событий это
#: не погрешность, а другая партия. Ключей ещё нет (журнал появится в фазе 1),
#: и это намеренно: правило заводится раньше данных, которым оно адресовано.
EXACT_SUBTREES: tuple[str, ...] = ("events", "log")


def relative_difference(left: float, right: float) -> float:
    """Относительное расхождение, устойчивое около нуля.

    Делить на величину, стремящуюся к нулю, нельзя: концентрация ксенона после
    долгой остановки уходит именно туда, и относительная мера там теряет смысл.
    """
    if left == right:
        return 0.0
    scale = max(abs(left), abs(right))
    if scale <= ABS_TOL:
        return 0.0
    return abs(left - right) / scale


def _is_exact_path(path: str) -> bool:
    if path in EXACT_FLOAT_PATHS:
        return True
    head = path.split(".", 1)[0].split("[", 1)[0]
    return head in EXACT_SUBTREES


def _describe(value: Any) -> str:
    return f"{type(value).__name__} {value!r}"


def _walk(
    left: Any,
    right: Any,
    path: str,
    rel_tol: float,
    problems: list[str],
) -> None:
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else str(key)
            if key not in left:
                problems.append(f"{child}: нет слева, справа {_describe(right[key])}")
            elif key not in right:
                problems.append(f"{child}: нет справа, слева {_describe(left[key])}")
            else:
                _walk(left[key], right[key], child, rel_tol, problems)
        return

    if isinstance(left, list | tuple) and isinstance(right, list | tuple):
        if len(left) != len(right):
            problems.append(f"{path}: разная длина, {len(left)} против {len(right)}")
            return
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            _walk(a, b, f"{path}[{index}]", rel_tol, problems)
        return

    # bool — подкласс int, и «True вместо 1» обязано быть расхождением:
    # молчаливая подмена типа в сейве ломает миграции тише, чем хотелось бы.
    if type(left) is not type(right):
        problems.append(f"{path}: разные типы, {_describe(left)} против {_describe(right)}")
        return

    if isinstance(left, float) and not _is_exact_path(path):
        if math.isnan(left) and math.isnan(right):
            return
        difference = relative_difference(left, right)
        if difference > rel_tol:
            problems.append(
                f"{path}: {left!r} против {right!r}, "
                f"относительное расхождение {difference:.3e} > {rel_tol:.0e}"
            )
        return

    if left != right:
        problems.append(f"{path}: {left!r} против {right!r}")


def compare_states(left: dict, right: dict, *, rel_tol: float = REL_TOL) -> list[str]:
    """Все расхождения между состояниями по контракту И4а.

    Пустой список означает эквивалентность. ``rel_tol`` переопределяется только
    в тестах самого компаратора: игровые тесты обязаны пользоваться значением
    из BALANCE.md, иначе допуск разъедется по репозиторию.
    """
    problems: list[str] = []
    _walk(left, right, "", rel_tol, problems)
    return problems


def _fail(problems: list[str], header: str) -> None:
    lines = "\n".join(f"  · {item}" for item in problems)
    raise AssertionError(f"{header} ({len(problems)}):\n{lines}")


def assert_states_equivalent(left: dict, right: dict, *, rel_tol: float = REL_TOL) -> None:
    """Состояния эквивалентны по И4а."""
    problems = compare_states(left, right, rel_tol=rel_tol)
    if problems:
        _fail(problems, "состояния разошлись")


def assert_states_exact(left: dict, right: dict) -> None:
    """Состояния совпадают точно, включая непрерывные величины.

    Применимо, пока всё состояние дискретно, и к тем тестам, где разбиения на
    блоки нет — там ослаблять сравнение нечем и незачем.
    """
    problems = compare_states(left, right, rel_tol=0.0)
    if problems:
        _fail(problems, "состояния не совпадают точно")
