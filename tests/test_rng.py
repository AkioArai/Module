"""Детерминизм случайности (инвариант И2).

Свойства, которые здесь проверяются, — это не «генератор работает», а прямые
гарантии для сейвов: тот же seed даёт тот же мир, новый поток не сдвигает
старые, счётчик полностью описывает состояние.
"""

from __future__ import annotations

import pytest

from module_sim.core.rng import Rng, RngStream


def test_same_seed_same_sequence():
    a = Rng(12345)
    b = Rng(12345)
    left = [a.stream("market.noise").random() for _ in range(50)]
    right = [b.stream("market.noise").random() for _ in range(50)]
    assert left == right


def test_different_seeds_diverge():
    a = [Rng(1).stream("market.noise").random() for _ in range(10)]
    b = [Rng(2).stream("market.noise").random() for _ in range(10)]
    assert a != b


def test_streams_are_independent():
    """Разные имена — разные последовательности при одном seed."""
    rng = Rng(777)
    noise = [rng.stream("market.noise").random() for _ in range(20)]
    failure = [rng.stream("equipment.failure").random() for _ in range(20)]
    assert noise != failure


def test_new_stream_does_not_shift_existing():
    """Главное свойство для совместимости сейвов.

    Добавление подсистемы в новой версии игры заводит новый поток. Старые
    потоки обязаны выдать ровно то же самое — иначе обновление меняло бы ход
    уже идущей партии.
    """
    before = Rng(42)
    baseline = [before.stream("market.noise").random() for _ in range(30)]

    after = Rng(42)
    # «Новая подсистема» вклинивается между обращениями к старому потоку.
    observed = []
    for i in range(30):
        if i == 7:
            after.stream("brand.new.subsystem").random()
            after.stream("another.new.one").randint(0, 100)
        observed.append(after.stream("market.noise").random())

    assert observed == baseline


def test_counters_round_trip():
    """Счётчиков достаточно, чтобы продолжить с того же места."""
    rng = Rng(999)
    for _ in range(13):
        rng.stream("people.error").random()
    rng.stream("fuel.price").normal()

    restored = Rng(999, rng.counters())
    assert restored.counters() == rng.counters()

    expected = [rng.stream("people.error").random() for _ in range(5)]
    actual = [restored.stream("people.error").random() for _ in range(5)]
    assert actual == expected


def test_counters_are_sorted():
    """Порядок ключей в сейве не должен зависеть от порядка обращения."""
    rng = Rng(5)
    for name in ("zulu", "alpha", "mike"):
        rng.stream(name).random()
    assert list(rng.counters()) == ["alpha", "mike", "zulu"]


def test_at_is_pure():
    """``at`` не двигает счётчик — на этом держится батчинг."""
    stream = RngStream(1, "test")
    assert stream.at(100) == stream.at(100)
    assert stream.counter == 0


def test_skip_matches_sequential_draws():
    """Промотка за O(1) обязана давать то же, что и честные розыгрыши."""
    sequential = RngStream(3, "wear")
    for _ in range(1000):
        sequential.random()

    jumped = RngStream(3, "wear")
    jumped.skip(1000)

    assert jumped.counter == sequential.counter
    assert jumped.random() == sequential.random()


def test_normal_consumes_exactly_two_draws():
    """Расход счётчика обязан быть предсказуем, иначе промотка разъедется."""
    stream = RngStream(8, "people.error")
    stream.normal()
    assert stream.counter == 2


def test_uniform_within_range():
    stream = RngStream(11, "market.noise")
    for _ in range(200):
        assert 0.0 <= stream.random() < 1.0


def test_randint_covers_bounds_without_bias():
    stream = RngStream(21, "people.names")
    seen = {stream.randint(1, 6) for _ in range(400)}
    assert seen == {1, 2, 3, 4, 5, 6}


def test_weibull_and_exponential_are_positive():
    stream = RngStream(31, "equipment.failure")
    for _ in range(100):
        assert stream.exponential(0.001) > 0.0
        assert stream.weibull(2.2, 45_000.0) > 0.0


def test_choice_rejects_empty():
    with pytest.raises(ValueError):
        RngStream(1, "x").choice([])


def test_skip_rejects_negative():
    with pytest.raises(ValueError):
        RngStream(1, "x").skip(-1)


def test_stream_values_are_stable_across_versions():
    """Закреплённые значения.

    Если эти числа изменятся, изменится ход **любой** существующей партии.
    Правка допустима только вместе с осознанным решением сломать
    совместимость и миграцией, которая это учитывает.
    """
    stream = RngStream(0, "canary")
    assert stream.raw_at(0) == 16373079682034215818
    assert stream.raw_at(1) == 16497167622200361917
    assert stream.raw_at(1_000_000) == 526425884656822583
