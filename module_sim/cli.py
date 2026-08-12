"""Точка входа: ``module`` (и короткий алиас ``mdl``).

Подкоманды: ``run`` (умолчание), ``console``, ``save``, ``doctor``.

Здесь — одно из двух мест в игре, которым разрешено читать системное время
(второе — сохранение). Инвариант И2 держится именно на том, что ``core``
получает момент «сейчас» параметром, а добывается он тут.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from module_sim import __version__
from module_sim.core.economy import finance
from module_sim.core.sim import Simulation
from module_sim.core.state import Speed
from module_sim.persistence import paths
from module_sim.persistence import save as save_mod
from module_sim.persistence.lock import SaveLock, SaveLocked
from module_sim.persistence.migrations import CURRENT_SCHEMA_VERSION

__all__ = ["main"]


# --------------------------------------------------------------------------
# Запуск игры
# --------------------------------------------------------------------------


def _plural_hours(ticks: int) -> str:
    """Человеческая формулировка догона: часы, сутки или годы."""
    if ticks < 48:
        return f"{ticks} ч"
    if ticks < 24 * 365:
        return f"{ticks / 24:.1f} сут"
    return f"{ticks / (24 * 365):.1f} лет"


def _load_or_create(args: argparse.Namespace) -> tuple[Simulation, float, str]:
    """Вернуть симуляцию, время создания партии и текст уведомления игроку."""
    now = time.time()
    save_path = Path(args.save) if args.save else None

    if args.new or not save_mod.has_save(save_path):
        seed = args.seed if args.seed is not None else int(now * 1000) & 0x7FFF_FFFF
        simulation = Simulation.new_game(seed=seed, epoch=now)
        meta = save_mod.load_meta()
        finance.start_company(simulation, meta.carried_debt_cents)
        note = f"Новая партия. Seed {seed}."
        if meta.carried_debt_cents:
            note += (
                f" Долг прошлой компании — {meta.carried_debt_cents / 100:,.0f} ₽ — перешёл на эту."
            ).replace(",", " ")
        return simulation, now, note

    result = save_mod.load_game(save_path)
    simulation = Simulation(result.state)

    notes: list[str] = []
    if result.recovered:
        # Молча подменять партию бэкапом нельзя — игрок обязан узнать.
        notes.append(f"Основной сейв не читался, партия поднята из {result.source.name}.")
    if result.migrated:
        first = result.migrated[0].from_version
        last = result.migrated[-1].to_version
        notes.append(f"Сейв обновлён со схемы v{first} до v{last}.")

    status_before = simulation.state.finance.status

    missed = simulation.clock.missed(result.saved_at, now)
    if missed.ticks:
        started = time.monotonic()
        simulation.catch_up(missed.ticks)
        spent = time.monotonic() - started
        rate = missed.ticks / spent if spent > 0 else float("inf")
        notes.append(
            f"Досчитано {_plural_hours(missed.ticks)} ({missed.ticks} тиков, {rate:,.0f} тик/с)."
        )
    elif simulation.clock.paused:
        notes.append("Партия была на паузе — время не шло.")

    if missed.capped:
        # Молчать здесь нельзя: чаще всего это не «долго не играл», а сбитые
        # системные часы, и игрок должен понимать, что произошло.
        notes.append(
            f"Отброшено {_plural_hours(missed.dropped)} сверх потолка догона — "
            f"проверьте системное время."
        )

    became_bankrupt_away = (
        simulation.state.finance.status == finance.STATUS_BANKRUPT
        and status_before != finance.STATUS_BANKRUPT
    )
    if became_bankrupt_away:
        # DESIGN.md, §Р5: партия не заканчивается, пока игрока нет.
        finance.grant_return_grace(simulation)
        notes.append(
            "Пока вас не было, компания дошла до банкротства. "
            f"Дан срок на исправление — {finance.GRACE_HOURS // 24} суток."
        )
    elif simulation.state.finance.status == finance.STATUS_BANKRUPT:
        carried = finance.carried_debt_cents(simulation)
        meta = save_mod.load_meta()
        meta.carried_debt_cents = carried
        meta.bankruptcies += 1
        save_mod.save_meta(meta)
        notes.append(
            f"Компания обанкротилась. Непогашенный долг — {carried / 100:,.0f} ₽ — "
            "перейдёт на следующую партию. Начните новую: module --new.".replace(",", " ")
        )
    elif simulation.state.finance.status == finance.STATUS_GRACE:
        left = max(0, simulation.state.finance.grace_ends_tick - simulation.state.tick)
        notes.append(f"Предбанкротное состояние: на исправление осталось {left // 24} суток.")

    if simulation.unknown_events:
        kinds = ", ".join(sorted(set(simulation.unknown_events)))
        notes.append(f"Пропущены незнакомые события из сейва: {kinds}.")

    return simulation, result.created_at, " ".join(notes)


def cmd_run(args: argparse.Namespace) -> int:
    paths.ensure_dirs()
    save_target = Path(args.save) if args.save else paths.save_path()

    # Блокировка берётся до чтения сейва и держится до конца игры. Две копии,
    # открывшие одну партию, автосохраняются поверх друг друга, и последняя
    # запись молча уносит чужой прогресс (persistence/lock.py).
    try:
        with SaveLock(save_target):
            return _run_locked(args)
    except SaveLocked as exc:
        print(f"{exc}", file=sys.stderr)
        return 1


def _run_locked(args: argparse.Namespace) -> int:
    simulation, created_at, notice = _load_or_create(args)

    if args.speed:
        simulation.clock.set_speed(args.speed)

    if args.headless:
        # Отладочный режим: досчитать и выйти, не поднимая терминал. Нужен,
        # чтобы проверять догон и сейвы без Textual (инвариант И1).
        save_mod.save_game(
            simulation.sync_state(),
            now=time.time(),
            created_at=created_at,
            path=Path(args.save) if args.save else None,
        )
        print(notice or "Партия загружена.")
        print(f"Тик {simulation.state.tick}, дата {simulation.clock.format_datetime()}.")
        return 0

    # Импорт Textual отложен: подкоманды save/doctor обязаны работать даже
    # если с интерфейсом что-то не так.
    from module_sim.ui.app import ModuleApp

    app = ModuleApp(
        simulation,
        created_at=created_at,
        notice=notice,
        save_path=Path(args.save) if args.save else None,
    )
    app.run()
    return 0


# --------------------------------------------------------------------------
# Консоль аналитика
# --------------------------------------------------------------------------


def cmd_console(args: argparse.Namespace) -> int:
    """Фаза 5 (DESIGN.md, §7). Пока честно сообщает, что не готова."""
    socket = paths.socket_path()
    print("Консоль аналитика появится в фазе 5.")
    print(f"Она подключится к запущенной игре через {socket}")
    print("и даст Python REPL с объектом game: телеметрия обычными списками и словарями.")
    if not socket.exists():
        print("\nСейчас игра не запущена — сокета нет.")
    return 0


# --------------------------------------------------------------------------
# Управление сейвами
# --------------------------------------------------------------------------


def cmd_save(args: argparse.Namespace) -> int:
    action = args.save_action

    if action == "info":
        return _save_info()
    if action == "backups":
        return _save_backups()
    if action == "restore":
        target = save_mod.restore_backup(args.index)
        print(f"Бэкап .{args.index} восстановлен в {target}")
        return 0
    if action == "export":
        return _save_export(Path(args.path))
    raise AssertionError(f"неизвестное действие: {action}")


def _save_info() -> int:
    if not save_mod.has_save():
        print(f"Сейва нет: {paths.save_path()}")
        return 1
    result = save_mod.load_game()
    state = result.state
    print(f"Файл:        {result.source}")
    print(f"Схема:       v{CURRENT_SCHEMA_VERSION}")
    if result.migrated:
        steps = " → ".join(f"v{s.to_version}" for s in result.migrated)
        print(f"Миграции:    v{result.migrated[0].from_version} → {steps}")
    if result.recovered:
        print("Внимание:    основной файл не читался, показан бэкап.")
    print(f"Компания:    {state.company.name}")
    print(f"Seed:        {state.seed}")
    print(f"Тик:         {state.tick}")
    print(f"Дата:        {Simulation(state).clock.format_datetime()}")
    print(f"Касса:       {state.company.cash_cents / 100:,.2f} ₽")
    print(f"Скорость:    {state.speed}")
    print(f"Потоки RNG:  {len(state.rng_counters)}")
    return 0


def _save_backups() -> int:
    print(f"Каталог: {paths.data_home()}")
    found = False
    for index in range(1, paths.BACKUP_RING + 1):
        path = paths.backup_path(index)
        if path.exists():
            found = True
            size = path.stat().st_size
            stamp = time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(path.stat().st_mtime))
            print(f"  .{index}  {size:>8} Б  {stamp}")
    if not found:
        print("  бэкапов нет")
    return 0


def _save_export(destination: Path) -> int:
    """Скопировать текущий сейв как фикстуру (SAVEFORMAT.md, §5).

    Копируется байт в байт, без пересохранения: фикстура обязана быть тем
    файлом, который действительно писала игра, а не результатом round-trip.
    """
    source = paths.save_path()
    if not source.exists():
        print(f"Сейва нет: {source}", file=sys.stderr)
        return 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    print(f"Фикстура записана: {destination}")
    return 0


# --------------------------------------------------------------------------
# Диагностика
# --------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True

    print(f"Module {__version__}, схема сейва v{CURRENT_SCHEMA_VERSION}")
    print(f"Python {sys.version.split()[0]}")

    print("\nЗависимости:")
    for module, package in (
        ("textual", "python-textual"),
        ("rich", "python-rich"),
        ("msgpack", "python-msgpack"),
        ("platformdirs", "python-platformdirs"),
    ):
        try:
            __import__(module)
        except ImportError:
            ok = False
            print(f"  ✗ {module} — поставьте {package}")
        else:
            print(f"  ✓ {module}")

    print("\nПути:")
    for label, path in (
        ("данные", paths.data_home()),
        ("состояние", paths.state_home()),
        ("скрипты", paths.scripts_dir()),
        ("сокет", paths.socket_path().parent),
    ):
        mark = "✓" if path.exists() else "·"
        print(f"  {mark} {label:<10} {path}")

    print("\nСейв:")
    if SaveLock.is_locked(paths.save_path()):
        print("  ! партия открыта в другом окне")
    if not save_mod.has_save():
        print("  · партии ещё нет")
    else:
        try:
            result = save_mod.load_game()
        except save_mod.SaveError as exc:
            ok = False
            print(f"  ✗ {exc}")
        else:
            print(f"  ✓ читается: {result.source.name}, тик {result.state.tick}")
            if result.recovered:
                print("  ! основной файл повреждён, используется бэкап")

    print("\nВсё в порядке." if ok else "\nЕсть проблемы (см. ✗).")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Разбор аргументов
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="module",
        description="Module — симулятор энергокомпании с атомной станцией.",
    )
    parser.add_argument("--version", action="version", version=f"Module {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="запустить игру (по умолчанию)")
    _add_run_arguments(run)
    run.set_defaults(func=cmd_run)

    console = subparsers.add_parser("console", help="консоль аналитика (фаза 5)")
    console.set_defaults(func=cmd_console)

    save = subparsers.add_parser("save", help="управление сохранениями")
    save_sub = save.add_subparsers(dest="save_action", required=True)
    save_sub.add_parser("info", help="показать текущую партию")
    save_sub.add_parser("backups", help="список бэкапов")
    restore = save_sub.add_parser("restore", help="восстановить бэкап")
    restore.add_argument("index", nargs="?", type=int, default=1, help="номер бэкапа, 1..5")
    export = save_sub.add_parser("export", help="скопировать сейв как фикстуру")
    export.add_argument("path", help="куда записать")
    save.set_defaults(func=cmd_save)

    doctor = subparsers.add_parser("doctor", help="проверить окружение и сейв")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--new", action="store_true", help="начать новую партию")
    parser.add_argument("--seed", type=int, help="seed новой партии")
    parser.add_argument("--save", help="путь к файлу сейва (по умолчанию — XDG)")
    parser.add_argument("--speed", choices=Speed.ALL, help="скорость при запуске")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="досчитать и выйти, не поднимая интерфейс",
    )


#: Подкоманды. Всё остальное в первом аргументе означает неявный ``run``.
COMMANDS = ("run", "console", "save", "doctor")

#: Флаги, которые обслуживает корневой парсер, а не ``run``.
ROOT_FLAGS = ("--version", "-h", "--help")


def _needs_implicit_run(argv: list[str]) -> bool:
    """``module`` без подкоманды — это ``module run``.

    Запуск игры — основной сценарий, и набирать ``module run`` каждый раз
    незачем. Проверяется первый аргумент, чтобы работали и ``module``, и
    ``module --seed 1``, и при этом ``module --help`` доставался корневому
    парсеру, а не ``run``.
    """
    if not argv:
        return True
    if argv[0] in ROOT_FLAGS:
        return False
    return argv[0] not in COMMANDS


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)

    if _needs_implicit_run(argv):
        argv = ["run", *argv]

    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
