"""Общие фикстуры.

Главное здесь — изоляция от домашнего каталога. Тесты сохранений пишут файлы, и
ни один из них не имеет права коснуться настоящей партии игрока.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from module_sim.core.events import registry
from module_sim.persistence import paths

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_SAVES = Path(__file__).resolve().parent / "fixtures" / "saves"


@pytest.fixture(autouse=True)
def isolated_event_handlers():
    """Вернуть реестр обработчиков как было после каждого теста.

    Реестр глобален, а тесты в него пишут. ``autouse`` по той же причине, что и
    изоляция каталога данных: забытая уборка проявилась бы не в том тесте,
    который её забыл, а в случайном следующем.
    """
    saved = registry.snapshot_handlers()
    yield
    registry.restore_handlers(saved)


@pytest.fixture(autouse=True)
def isolated_data_home(tmp_path, monkeypatch):
    """Увести все пути игры во временный каталог — для каждого теста свой.

    ``autouse``, а не явная зависимость: забытая фикстура в новом тесте
    означала бы запись в ``~/.local/share/module``, то есть порчу партии
    разработчика. Такую ошибку нельзя оставлять возможной.
    """
    home = tmp_path / "data"
    monkeypatch.setenv(paths.ENV_DATA_HOME, str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    paths.ensure_dirs()
    return home
