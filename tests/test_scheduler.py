"""Планировщик дискретных событий (DESIGN.md, §Р2).

Планировщик — то, что делает батчевый догон возможным: он отвечает на вопрос
«до какого момента можно прыгнуть одним куском». Ошибка здесь не проявится
падением, она проявится тем, что закрытая игра доигрывает не так, как открытая.
Поэтому проверяется не только «работает», но и детерминизм порядка, переживание
сейва и поведение на границах.
"""

from __future__ import annotations

import pytest

from module_sim.core.events.scheduler import ScheduledEvent, Scheduler, SchedulerError


def make() -> Scheduler:
    return Scheduler()


# -- порядок ---------------------------------------------------------------


def test_events_come_out_in_tick_order():
    scheduler = make()
    scheduler.schedule(50, "поздно")
    scheduler.schedule(10, "рано")
    scheduler.schedule(30, "посередине")

    assert scheduler.peek() == 10
    assert scheduler.pop_due(100).kind == "рано"
    assert scheduler.pop_due(100).kind == "посередине"
    assert scheduler.pop_due(100).kind == "поздно"


def test_same_tick_keeps_insertion_order():
    """Ничьих не бывает: порядковый номер разводит события одного тика.

    Без этого порядок зависел бы от внутреннего устройства кучи, то есть от
    версии CPython, — и партия перестала бы воспроизводиться (И2).
    """
    scheduler = make()
    for index in range(20):
        scheduler.schedule(7, f"событие-{index}")

    order = [scheduler.pop_due(7).kind for _ in range(20)]
    assert order == [f"событие-{index}" for index in range(20)]


def test_pop_due_respects_the_boundary():
    scheduler = make()
    scheduler.schedule(10, "сейчас")
    scheduler.schedule(11, "потом")

    assert scheduler.pop_due(10).kind == "сейчас"
    assert scheduler.pop_due(10) is None, "событие следующего тика сработало раньше срока"
    assert scheduler.pop_due(11).kind == "потом"


def test_empty_queue_peeks_none():
    assert make().peek() is None


# -- отмена ----------------------------------------------------------------


def test_cancelled_event_never_fires():
    scheduler = make()
    order = scheduler.schedule(10, "отменяемое")
    scheduler.schedule(10, "остаётся")
    scheduler.cancel(order)

    assert [event.kind for event in scheduler.pending()] == ["остаётся"]
    assert scheduler.pop_due(10).kind == "остаётся"
    assert scheduler.pop_due(10) is None


def test_cancelling_twice_is_not_an_error():
    scheduler = make()
    order = scheduler.schedule(10, "событие")
    scheduler.cancel(order)
    scheduler.cancel(order)
    assert scheduler.pop_due(10) is None


def test_cancelled_head_does_not_hide_the_next_event():
    """Отменённое на вершине не должно закрывать обзор: ``peek`` обязан
    показать следующее живое, иначе догон прыгнет мимо."""
    scheduler = make()
    order = scheduler.schedule(5, "отменённое")
    scheduler.schedule(9, "живое")
    scheduler.cancel(order)

    assert scheduler.peek() == 9


def test_cancelled_ids_do_not_pile_up_in_the_save():
    """Множество отменённых чистится: иначе сейв долгой партии распухал бы
    номерами событий, которых давно нет."""
    scheduler = make()
    for _ in range(100):
        scheduler.cancel(scheduler.schedule(3, "мусор"))
    scheduler.pop_due(3)

    assert scheduler.to_dict()["events"] == []
    assert len(scheduler.pending()) == 0


# -- планирование в прошлое ------------------------------------------------


def test_scheduling_into_the_past_is_refused():
    """Событие в прошлом никогда не сработает — это ошибка в формуле интервала,
    и обнаружить её надо в момент планирования, а не месяцем позже."""
    scheduler = make()
    with pytest.raises(SchedulerError, match=r"а сейчас уже"):
        scheduler.schedule(5, "поздно", not_before=10)


def test_scheduling_on_the_current_tick_is_allowed():
    scheduler = make()
    scheduler.schedule(10, "сейчас же", not_before=10)
    assert scheduler.peek() == 10


# -- сериализация ----------------------------------------------------------


def test_queue_survives_a_round_trip():
    scheduler = make()
    scheduler.schedule(10, "первое", {"unit": 1})
    scheduler.schedule(10, "второе", {"unit": 2})
    scheduler.schedule(99, "третье")

    restored = Scheduler.from_dict(scheduler.to_dict())

    assert [(event.tick, event.kind, event.payload) for event in restored.pending()] == [
        (10, "первое", {"unit": 1}),
        (10, "второе", {"unit": 2}),
        (99, "третье", {}),
    ]


def test_order_counter_survives_and_does_not_collide():
    """После загрузки новые события обязаны получать свежие номера.

    Если счётчик сбросится, новое событие получит номер уже существующего, и
    отмена по номеру попадёт не в то событие.
    """
    scheduler = make()
    scheduler.schedule(10, "старое")
    scheduler.schedule(20, "тоже старое")

    restored = Scheduler.from_dict(scheduler.to_dict())
    fresh = restored.schedule(30, "новое")

    existing = {event.order for event in restored.pending() if event.kind != "новое"}
    assert fresh not in existing


def test_corrupted_next_order_is_repaired_on_load():
    """Сейв с заниженным next_order не должен приводить к пересечению номеров.

    Файл мог быть отредактирован руками или пережить неудачную миграцию;
    молча выдавать чужие номера после этого нельзя.
    """
    restored = Scheduler.from_dict(
        {
            "events": [ScheduledEvent(tick=1, order=41, kind="старое").to_dict()],
            "next_order": 0,
        }
    )
    assert restored.schedule(2, "новое") > 41


def test_empty_queue_round_trips():
    assert Scheduler.from_dict(make().to_dict()).peek() is None
