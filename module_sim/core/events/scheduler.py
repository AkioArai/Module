"""Планировщик дискретных событий — опора батчевого догона (DESIGN.md, §Р2).

Догон устроен как прыжок от события к событию: на интервале между ними
состояние эволюционирует замкнутыми формулами, а само событие применяется
точечно. Всё, что знает, куда прыгать, живёт здесь.

Три свойства, ради которых планировщик написан именно так.

**Детерминизм порядка.** События сравниваются по паре ``(тик, порядковый
номер)``. Номер выдаётся возрастающим счётчиком, поэтому ничьих не бывает
вовсе: два события, назначенные на один тик, всегда срабатывают в том порядке,
в каком их поставили в очередь. Сортировка по одному тику дала бы зависимость
от внутреннего порядка кучи — то есть от версии CPython.

**Очередь — часть партии.** Она целиком уходит в сейв и восстанавливается из
него (И5). Планировщик, у которого есть незасериализованное поле, после
перезапуска даст другую партию.

**Отмена ленивая.** Отменённое событие остаётся в куче и пропускается при
извлечении. Так дешевле, чем перестраивать кучу, и главное — не зависит от
порядка обхода: удаление из середины сдвинуло бы всё остальное.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ScheduledEvent", "Scheduler", "SchedulerError"]


class SchedulerError(RuntimeError):
    """Неправильное обращение с очередью событий."""


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    """Одно запланированное событие.

    ``payload`` — простые данные, уходящие в сейв как есть: словарь из чисел и
    строк. Ссылки на объекты сюда класть нельзя, их нечем сериализовать.
    """

    tick: int
    order: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tick": self.tick,
            "order": self.order,
            "kind": self.kind,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict) -> ScheduledEvent:
        return cls(
            tick=data["tick"],
            order=data["order"],
            kind=data["kind"],
            payload=dict(data.get("payload", {})),
        )


class Scheduler:
    """Очередь дискретных событий партии."""

    __slots__ = ("_cancelled", "_heap", "_next_order")

    def __init__(
        self,
        events: list[ScheduledEvent] | None = None,
        next_order: int = 0,
        cancelled: set[int] | None = None,
    ) -> None:
        self._heap: list[tuple[int, int, ScheduledEvent]] = [
            (event.tick, event.order, event) for event in (events or [])
        ]
        heapq.heapify(self._heap)
        self._next_order = next_order
        # Множество, а не список: сюда только заглядывают по членству, никогда
        # не обходят. Порядок обхода множества недетерминирован, и обход стал бы
        # нарушением И2 — поэтому наружу оно уходит только отсортированным.
        self._cancelled: set[int] = set(cancelled or ())

    # -- планирование ----------------------------------------------------

    def schedule(
        self,
        tick: int,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        not_before: int | None = None,
    ) -> int:
        """Поставить событие на тик ``tick``. Возвращает его номер для отмены.

        ``not_before`` — текущий тик симуляции, если вызывающий его знает.
        Событие в прошлом никогда не сработает и почти всегда означает ошибку в
        формуле интервала, поэтому падаем сразу, а не молча копим мусор.
        """
        if not_before is not None and tick < not_before:
            raise SchedulerError(
                f"событие {kind!r} назначено на тик {tick}, а сейчас уже {not_before}"
            )
        order = self._next_order
        self._next_order += 1
        event = ScheduledEvent(tick=tick, order=order, kind=kind, payload=dict(payload or {}))
        heapq.heappush(self._heap, (tick, order, event))
        return order

    def cancel(self, order: int) -> None:
        """Отменить событие по номеру. Повторная отмена не ошибка."""
        self._cancelled.add(order)

    def is_cancelled(self, order: int) -> bool:
        return order in self._cancelled

    # -- извлечение ------------------------------------------------------

    def _drop_cancelled_head(self) -> None:
        """Снять с вершины отменённые события.

        Здесь же чистится множество отменённых: номер, чьё событие уже покинуло
        кучу, хранить незачем, а расти без границы ему нельзя — сейв длинной
        партии распух бы на пустом месте.
        """
        while self._heap and self._heap[0][1] in self._cancelled:
            _, order, _ = heapq.heappop(self._heap)
            self._cancelled.discard(order)

    def peek(self) -> int | None:
        """Тик ближайшего события или ``None``, если очередь пуста.

        Это и есть ответ на вопрос «до какого момента можно прыгнуть одним
        куском» — то, вокруг чего построен весь батчевый догон.
        """
        self._drop_cancelled_head()
        return self._heap[0][0] if self._heap else None

    def pop_due(self, tick: int) -> ScheduledEvent | None:
        """Снять одно событие, срок которого настал. ``None`` — таких нет.

        По одному, а не пачкой: обработчик события имеет право поставить новое
        на этот же тик, и оно обязано попасть в ту же выборку.
        """
        self._drop_cancelled_head()
        if not self._heap or self._heap[0][0] > tick:
            return None
        _, _, event = heapq.heappop(self._heap)
        return event

    # -- состояние -------------------------------------------------------

    def pending(self) -> list[ScheduledEvent]:
        """Все неотменённые события по возрастанию ``(тик, номер)``."""
        return [event for _, order, event in sorted(self._heap) if order not in self._cancelled]

    def __len__(self) -> int:
        return len(self.pending())

    def to_dict(self) -> dict:
        """Снимок для сейва.

        Отменённые события не сохраняются вовсе: восстанавливать их, чтобы тут
        же пропустить, незачем. Поэтому и список отменённых уходит пустым —
        после загрузки отменять уже нечего.
        """
        return {
            "events": [event.to_dict() for event in self.pending()],
            "next_order": self._next_order,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Scheduler:
        events = [ScheduledEvent.from_dict(item) for item in data.get("events", [])]
        # next_order обязан быть строго больше любого сохранённого номера,
        # иначе новые события начнут получать чужие номера и отмена промахнётся.
        highest = max((event.order for event in events), default=-1)
        next_order = max(int(data.get("next_order", 0)), highest + 1)
        return cls(events=events, next_order=next_order)

    def __repr__(self) -> str:
        return f"Scheduler(pending={len(self.pending())}, next_order={self._next_order})"
