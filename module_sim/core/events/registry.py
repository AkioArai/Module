"""Реестр обработчиков событий.

Планировщик знает **когда** сработает событие, реестр — **что** тогда делать.
Разделение нужно затем, что очередь уходит в сейв, а обработчики — нет: в файле
лежит только вид события строкой.

Отсюда неприятный, но обязательный случай: сейв может содержать вид события,
которого в текущей игре больше нет. Так бывает при откате игры назад и при
удалении механики между версиями. Ронять партию из-за этого нельзя — требование
номер один говорит, что обновление не теряет прогресс, и «протухшее событие в
очереди» не то основание, чтобы его нарушить.

Поэтому неизвестный вид **пропускается и запоминается**, а не бросает
исключение. Игрок узнаёт об этом из уведомления при загрузке: молча терять
запланированное событие тоже нельзя, это скрытое изменение партии.

Инвариант И8 (событие обязано иметь выводимую из состояния причину) действует
на уровне того, кто ставит событие в очередь, а не здесь: реестр — механика
доставки, а не источник случайности.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from module_sim.core.events.scheduler import ScheduledEvent

if TYPE_CHECKING:  # pragma: no cover - только для типов
    from module_sim.core.sim import Simulation

__all__ = ["Handler", "clear_handlers", "dispatch", "handler", "is_known", "known_kinds"]

Handler = Callable[["Simulation", ScheduledEvent], None]

_HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    """Зарегистрировать обработчик вида события.

    Повторная регистрация одного вида запрещена: два обработчика на одно
    событие означают, что порядок их вызова определяет результат, а порядок
    зависел бы от порядка импортов — то есть от случайности (И2).
    """

    def register(function: Handler) -> Handler:
        if kind in _HANDLERS:
            raise ValueError(f"обработчик вида {kind!r} уже зарегистрирован")
        _HANDLERS[kind] = function
        return function

    return register


def is_known(kind: str) -> bool:
    return kind in _HANDLERS


def known_kinds() -> list[str]:
    """Виды событий по алфавиту. Отсортировано: список попадает в диагностику,
    а порядок словаря не должен просачиваться наружу."""
    return sorted(_HANDLERS)


def dispatch(simulation: Simulation, event: ScheduledEvent) -> bool:
    """Выполнить событие. ``False`` — вид неизвестен, событие пропущено."""
    function = _HANDLERS.get(event.kind)
    if function is None:
        return False
    function(simulation, event)
    return True


def clear_handlers() -> None:
    """Только для тестов: реестр глобален, и тест, зарегистрировавший вид,
    обязан за собой убрать, иначе следующий тест получит чужой обработчик."""
    _HANDLERS.clear()


def snapshot_handlers() -> dict[str, Handler]:
    """Только для тестов: копия реестра, чтобы потом вернуть его как было."""
    return dict(_HANDLERS)


def restore_handlers(snapshot: dict[str, Handler]) -> None:
    """Только для тестов: вернуть реестр к сохранённому состоянию.

    Пара к ``snapshot_handlers``. Нужна отдельно от ``clear_handlers``, потому
    что очистка в фазах 2+ снесла бы настоящие обработчики игры, а тест обязан
    убирать за собой ровно то, что добавил."""
    _HANDLERS.clear()
    _HANDLERS.update(snapshot)
