"""Направление зависимостей (инвариант И1).

``core/`` не знает об интерфейсе. Если для теста геймплея понадобился Textual —
сломана архитектура, а не тест. Проверяется двумя способами: разбором импортов
в исходниках и настоящим импортом ``core`` при запрещённом Textual.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "module_sim"
CORE = PACKAGE / "core"

#: Что запрещено импортировать из ``core``.
CORE_FORBIDDEN = ("module_sim.ui", "textual", "rich")

#: Слои и то, что каждому нельзя тянуть. Направление — только вниз
#: (CLAUDE.md, И1).
LAYER_RULES = {
    "core": ("module_sim.ui", "module_sim.persistence", "module_sim.cli", "module_sim.console"),
    "persistence": ("module_sim.ui", "module_sim.cli", "module_sim.console"),
    "ipc": ("module_sim.ui",),
}


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module))
    return found


def _files(layer: str) -> list[Path]:
    return sorted((PACKAGE / layer).rglob("*.py"))


CORE_FILES = _files("core")
CORE_IDS = [str(path.relative_to(CORE)) for path in CORE_FILES]


@pytest.mark.parametrize("path", CORE_FILES, ids=CORE_IDS)
def test_core_does_not_import_ui_or_textual(path):
    problems = [
        f"строка {lineno}: {module}"
        for lineno, module in _imports(path)
        if module.split(".")[0] in {"textual", "rich"} or module.startswith(CORE_FORBIDDEN)
    ]
    assert not problems, f"{path.relative_to(CORE)} тянет интерфейс:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("layer", sorted(LAYER_RULES))
def test_layer_dependency_direction(layer):
    forbidden = LAYER_RULES[layer]
    problems: list[str] = []
    for path in _files(layer):
        for lineno, module in _imports(path):
            if module.startswith(forbidden):
                problems.append(f"{path.relative_to(PACKAGE)}:{lineno} → {module}")
    assert not problems, "нарушено направление зависимостей:\n  " + "\n  ".join(problems)


def test_core_imports_without_textual(monkeypatch):
    """Настоящая проверка: ``core`` обязан подниматься без Textual вовсе.

    Разбор исходников не увидит импорт внутри функции — этот тест увидит.

    Из ``sys.modules`` выбрасывается только ``module_sim.core``. Выбросить
    заодно ``textual`` и ``rich`` было бы заманчиво, но тогда их классы
    загрузятся повторно, и в процессе окажется два несовместимых ``rich.Style``
    — падать начнут совсем другие тесты. Блокировки импорта достаточно: она
    срабатывает и на уже загруженных модулях.
    """
    for name in list(sys.modules):
        if name.startswith("module_sim.core"):
            del sys.modules[name]

    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name.split(".")[0] in {"textual", "rich"}:
            raise ImportError(f"{name} недоступен в тесте архитектуры")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking_import)

    # Блокировка обязана работать — иначе тест ниже проходит вхолостую.
    # Проверяется именно инструкцией ``import``: она идёт через
    # ``builtins.__import__``, а ``importlib.import_module`` — мимо него.
    # Модули ``core`` мы грузим через importlib, но их собственные ``import``
    # внутри файлов блокировку увидят — а ровно это тест и ищет.
    with pytest.raises(ImportError):
        exec("import textual")

    for module in ("module_sim.core.rng", "module_sim.core.clock", "module_sim.core.sim"):
        importlib.import_module(module)


def test_simulation_runs_without_textual(monkeypatch):
    """Геймплей тестируется без терминала — это и есть смысл инварианта."""
    monkeypatch.setitem(sys.modules, "textual", None)
    from module_sim.core.sim import Simulation

    sim = Simulation.new_game(seed=1, epoch=0.0)
    sim.run(100)
    assert sim.state.tick == 100
