"""Тесты компаратора состояний.

Компаратор стал несущей конструкцией: через него проходят все проверки
эквивалентности догона. Инструмент, которым меряют, обязан быть проверен сам —
иначе «все тесты зелёные» перестанет что-либо означать.
"""

from __future__ import annotations

import math

from support.state_compare import (
    ABS_TOL,
    REL_TOL,
    assert_states_equivalent,
    assert_states_exact,
    compare_states,
)


def test_identical_states_have_no_problems():
    state = {"tick": 5, "xenon": 1.5, "company": {"name": "Модуль"}}
    assert compare_states(state, dict(state)) == []


def test_float_within_tolerance_passes():
    left = {"xenon": 1.0}
    right = {"xenon": 1.0 + REL_TOL / 10}
    assert compare_states(left, right) == []


def test_float_beyond_tolerance_fails():
    left = {"xenon": 1.0}
    right = {"xenon": 1.0 + REL_TOL * 10}
    problems = compare_states(left, right)
    assert len(problems) == 1
    assert "xenon" in problems[0]


def test_integers_are_never_approximate():
    """Деньги и счётчики — дискретное состояние, допуск к ним не применяется."""
    problems = compare_states({"cash_cents": 1_000_000}, {"cash_cents": 1_000_001})
    assert len(problems) == 1
    assert "cash_cents" in problems[0]


def test_epoch_is_compared_exactly_despite_being_float():
    """``epoch`` не эволюционирует: расхождение в нём — сдвиг календаря."""
    problems = compare_states({"epoch": 1.0}, {"epoch": 1.0 + REL_TOL / 10})
    assert len(problems) == 1
    assert "epoch" in problems[0]


def test_event_subtree_is_compared_exactly():
    """Журнал событий — уровень 1 контракта, допуск туда не проникает."""
    left = {"events": [{"tick": 3, "severity": 1.0}]}
    right = {"events": [{"tick": 3, "severity": 1.0 + REL_TOL / 10}]}
    problems = compare_states(left, right)
    assert len(problems) == 1
    assert "events[0].severity" in problems[0]


def test_bool_is_not_an_integer_here():
    """``True == 1`` в Python, но в сейве это разные вещи."""
    problems = compare_states({"scrammed": True}, {"scrammed": 1})
    assert len(problems) == 1
    assert "разные типы" in problems[0]


def test_int_and_float_are_different_types():
    """Молчаливое превращение копеек в float обязано быть заметным."""
    problems = compare_states({"cash_cents": 100}, {"cash_cents": 100.0})
    assert len(problems) == 1
    assert "разные типы" in problems[0]


def test_missing_keys_are_reported_on_both_sides():
    problems = compare_states({"a": 1}, {"b": 2})
    assert len(problems) == 2
    assert any("нет справа" in item for item in problems)
    assert any("нет слева" in item for item in problems)


def test_near_zero_values_do_not_blow_up_relative_comparison():
    """Концентрация после долгой остановки уходит в ноль — там относительная
    мера теряет смысл, и сравнение обязано опираться на абсолютный порог."""
    assert compare_states({"xenon": 0.0}, {"xenon": ABS_TOL / 10}) == []
    assert compare_states({"xenon": 1e-20}, {"xenon": -1e-20}) == []


def test_nan_matches_nan():
    """NaN не равен себе по IEEE, но два одинаково сломанных состояния — это
    не расхождение догона, а одна и та же поломка."""
    assert compare_states({"x": math.nan}, {"x": math.nan}) == []


def test_length_mismatch_reported_without_element_noise():
    problems = compare_states({"events": [1, 2, 3]}, {"events": [1, 2]})
    assert len(problems) == 1
    assert "разная длина" in problems[0]


def test_all_problems_are_collected_not_just_the_first():
    """При отладке интегрирования важно видеть, одна величина разъехалась
    или все сразу."""
    left = {"a": 1, "b": 2, "c": 3}
    right = {"a": 9, "b": 9, "c": 9}
    assert len(compare_states(left, right)) == 3


def test_path_shows_nesting():
    problems = compare_states(
        {"core": {"rods": [{"depth": 1}]}},
        {"core": {"rods": [{"depth": 2}]}},
    )
    assert "core.rods[0].depth" in problems[0]


def test_assert_helpers_raise_with_readable_message():
    try:
        assert_states_equivalent({"x": 1}, {"x": 2})
    except AssertionError as exc:
        assert "состояния разошлись" in str(exc)
        assert "x: 1 против 2" in str(exc)
    else:
        raise AssertionError("ожидалось расхождение")

    try:
        assert_states_exact({"x": 1.0}, {"x": 1.0 + REL_TOL / 10})
    except AssertionError as exc:
        assert "не совпадают точно" in str(exc)
    else:
        raise AssertionError("ожидалось расхождение")
