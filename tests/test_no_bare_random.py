"""Запрет голого ``random`` и системного времени в ``core/`` (инвариант И2).

Тест разбирает исходники в AST, а не ищет подстроки: регулярка спотыкается о
слово «random» в комментарии и молчит про ``from random import choice as pick``.
Детерминизм — обещание, которое стоит настоящего разбора.

Запрещено в ``core/``:

* ``random``, ``secrets``, ``numpy.random``, ``uuid`` — источники случайности
  мимо ``rng.stream()``. Новая партия с тем же seed обязана повторяться;
  один голый ``random.random()`` это ломает необратимо и незаметно.
* ``time.time()``, ``datetime.now()``, ``time.monotonic()`` — системное время.
  Момент «сейчас» приходит в ``core`` параметром; иначе ход партии зависел бы
  от того, когда её запустили.
* ``id()``, ``hash()`` объектов в горячем пути — не проверяется автоматически,
  но помните: ``hash`` строк рандомизирован между запусками Python.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent / "module_sim" / "core"

FORBIDDEN_MODULES = {
    "random": "используйте rng.stream(...)",
    "secrets": "криптослучайность недетерминирована",
    "uuid": "uuid4 недетерминирован; используйте счётчики",
    "numpy.random": "используйте rng.stream(...)",
}

FORBIDDEN_CALLS = {
    ("time", "time"): "системное время в core запрещено, передавайте now параметром",
    ("time", "monotonic"): "системное время в core запрещено",
    ("time", "perf_counter"): "системное время в core запрещено",
    ("datetime", "now"): "используйте Clock.game_datetime()",
    ("datetime", "today"): "используйте Clock.game_datetime()",
    ("datetime", "utcnow"): "используйте Clock.game_datetime()",
}

CORE_FILES = sorted(CORE.rglob("*.py"))
CORE_IDS = [str(path.relative_to(CORE)) for path in CORE_FILES]


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_MODULES:
                    found.append(
                        f"строка {node.lineno}: import {alias.name} — {FORBIDDEN_MODULES[root]}"
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            if root in FORBIDDEN_MODULES:
                names = ", ".join(alias.name for alias in node.names)
                found.append(
                    f"строка {node.lineno}: from {module} import {names} — "
                    f"{FORBIDDEN_MODULES[root]}"
                )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                key = (func.value.id, func.attr)
                if key in FORBIDDEN_CALLS:
                    found.append(
                        f"строка {node.lineno}: {func.value.id}.{func.attr}() — "
                        f"{FORBIDDEN_CALLS[key]}"
                    )

    return found


@pytest.mark.parametrize("path", CORE_FILES, ids=CORE_IDS)
def test_core_module_is_deterministic(path):
    problems = _violations(path)
    assert not problems, f"{path.relative_to(CORE)}:\n  " + "\n  ".join(problems)


def test_the_check_itself_catches_violations(tmp_path):
    """Тест, который ничего не ловит, хуже отсутствующего."""
    offender = tmp_path / "bad.py"
    offender.write_text(
        "import random\nimport time\n\n\ndef f():\n    return random.random(), time.time()\n",
        encoding="utf-8",
    )
    problems = _violations(offender)
    assert any("import random" in p for p in problems)
    assert any("time.time()" in p for p in problems)


def test_the_check_catches_aliased_imports(tmp_path):
    """``from random import choice as pick`` — то, что пропустила бы регулярка."""
    offender = tmp_path / "sneaky.py"
    offender.write_text("from random import choice as pick\n", encoding="utf-8")
    assert _violations(offender)


def test_core_actually_scanned():
    """Защита от «зелёных» тестов на пустом списке файлов."""
    assert len(CORE_FILES) >= 5
