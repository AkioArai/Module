"""Командная строка: ``module`` запускается, подкоманды работают без терминала.

``run --headless`` существует ровно для этого: проверить загрузку, догон и
сохранение, не поднимая Textual (инвариант И1).
"""

from __future__ import annotations

import pytest

from module_sim.cli import main
from module_sim.core.economy import finance
from module_sim.persistence import paths
from module_sim.persistence import save as save_mod


def test_new_game_creates_save(capsys):
    assert main(["run", "--new", "--seed", "123", "--headless"]) == 0
    assert paths.save_path().exists()
    assert "Новая партия" in capsys.readouterr().out


def test_seed_is_respected():
    main(["run", "--new", "--seed", "999", "--headless"])
    assert save_mod.load_game().state.seed == 999


def test_bare_invocation_means_run():
    """``module`` без аргументов — это ``module run``."""
    assert main(["--headless", "--new", "--seed", "1"]) == 0
    assert paths.save_path().exists()


def test_second_launch_catches_up(monkeypatch, capsys):
    """Пропущенное время досчитывается при запуске — и только там (И6)."""
    main(["run", "--new", "--seed", "42", "--headless"])
    saved = save_mod.load_game()
    assert saved.state.tick == 0

    # Игрока не было три часа реального времени. При курсе «сутки за час»
    # (DESIGN.md, §Р5) это трое игровых суток, а не 10 800 часов.
    monkeypatch.setattr("module_sim.cli.time.time", lambda: saved.saved_at + 3 * 3600)
    main(["run", "--headless"])

    assert save_mod.load_game().state.tick == 3 * 24
    assert "Досчитано" in capsys.readouterr().out


def test_paused_game_does_not_catch_up(monkeypatch, capsys):
    main(["run", "--new", "--seed", "42", "--speed", "paused", "--headless"])
    saved = save_mod.load_game()

    monkeypatch.setattr("module_sim.cli.time.time", lambda: saved.saved_at + 86_400)
    main(["run", "--headless"])

    assert save_mod.load_game().state.tick == 0
    assert "на паузе" in capsys.readouterr().out


def test_save_info(capsys):
    main(["run", "--new", "--seed", "7", "--headless"])
    assert main(["save", "info"]) == 0
    out = capsys.readouterr().out
    assert "Seed:        7" in out
    assert "Компания" in out


def test_save_info_without_save(capsys):
    assert main(["save", "info"]) == 1
    assert "Сейва нет" in capsys.readouterr().out


def test_save_backups_and_restore(capsys):
    main(["run", "--new", "--seed", "7", "--headless"])
    main(["run", "--headless", "--speed", "paused"])

    assert main(["save", "backups"]) == 0
    assert ".1" in capsys.readouterr().out

    assert main(["save", "restore", "1"]) == 0
    assert "восстановлен" in capsys.readouterr().out


def test_save_export_makes_a_fixture(tmp_path, capsys):
    main(["run", "--new", "--seed", "7", "--headless"])
    target = tmp_path / "fixtures" / "v002.json"

    assert main(["save", "export", str(target)]) == 0
    assert target.read_bytes() == paths.save_path().read_bytes(), (
        "фикстура обязана быть байт в байт"
    )


def test_doctor_reports_ok(capsys):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "textual" in out
    assert "Всё в порядке" in out


def test_doctor_flags_broken_save(capsys):
    main(["run", "--new", "--seed", "7", "--headless"])
    paths.save_path().write_bytes("мусор".encode())
    for index in range(1, paths.BACKUP_RING + 1):
        paths.backup_path(index).unlink(missing_ok=True)

    assert main(["doctor"]) == 1
    assert "Есть проблемы" in capsys.readouterr().out


def test_console_is_honest_about_phase(capsys):
    assert main(["console"]) == 0
    assert "фазе 5" in capsys.readouterr().out


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "Module" in capsys.readouterr().out


def test_bankruptcy_while_away_becomes_a_second_chance(monkeypatch, capsys):
    """§Р5: партия не заканчивается за выключенным экраном.

    Компания доводится до банкротства прямо в сейве, затем игра запускается
    так, будто игрока не было. При возвращении он обязан получить срок, а не
    сообщение о проигрыше.
    """
    main(["run", "--new", "--seed", "42", "--headless"])
    saved = save_mod.load_game()
    # Компания в предбанкротном состоянии, срок истекает через час, долг такой,
    # что выкарабкаться уже нечем.
    saved.state.finance.status = finance.STATUS_GRACE
    saved.state.finance.grace_ends_tick = saved.state.tick + 1
    saved.state.finance.debt_cents = finance.STATION_VALUE_CENTS * 3
    save_mod.save_game(saved.state, now=saved.saved_at, created_at=saved.created_at)

    reloaded = save_mod.load_game()
    # Двое суток реального отсутствия — при курсе «сутки за час» этого хватает,
    # чтобы месячное событие финансов успело сработать и срок истёк без игрока.
    monkeypatch.setattr("module_sim.cli.time.time", lambda: reloaded.saved_at + 48 * 3600)
    main(["run", "--headless"])

    out = capsys.readouterr().out
    assert "срок на исправление" in out
    assert save_mod.load_game().state.finance.status == finance.STATUS_GRACE


def test_bankruptcy_seen_by_the_player_ends_the_game(monkeypatch, capsys):
    """А вот банкротство, которое игрок застал, партию заканчивает и оставляет
    долг следующей — иначе провал ничего не стоил бы."""
    main(["run", "--new", "--seed", "42", "--headless"])
    saved = save_mod.load_game()
    saved.state.finance.status = finance.STATUS_BANKRUPT
    saved.state.finance.debt_cents = finance.STATION_VALUE_CENTS * 3
    save_mod.save_game(saved.state, now=saved.saved_at, created_at=saved.created_at)

    reloaded = save_mod.load_game()
    # Времени не прошло: значит статус не мог измениться в догоне.
    monkeypatch.setattr("module_sim.cli.time.time", lambda: reloaded.saved_at)
    main(["run", "--headless"])

    assert "обанкротилась" in capsys.readouterr().out
    assert save_mod.load_meta().carried_debt_cents > 0
    assert save_mod.load_meta().bankruptcies == 1


def test_carried_debt_lands_on_the_next_game(capsys):
    meta = save_mod.load_meta()
    meta.carried_debt_cents = 100_000_000_000
    save_mod.save_meta(meta)

    main(["run", "--new", "--seed", "7", "--headless"])

    assert "перешёл на эту" in capsys.readouterr().out
    state = save_mod.load_game().state
    assert state.finance.debt_cents == finance.STARTING_DEBT_CENTS + 100_000_000_000
