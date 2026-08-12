"""Общие фикстуры.

Главное здесь — изоляция от домашнего каталога. Тесты сохранений пишут файлы, и
ни один из них не имеет права коснуться настоящей партии игрока.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from module_sim.persistence import paths

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_SAVES = Path(__file__).resolve().parent / "fixtures" / "saves"


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
