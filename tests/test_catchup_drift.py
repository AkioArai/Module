"""Контракт И4а на непрерывных величинах, которых в фазе 0 ещё нет.

Проблема этого теста в том, что охраняемое им правило адресовано коду, который
напишут в фазах 2–5. Ждать до тех пор нельзя: к моменту появления ксенона и
износа требование «бит-в-бит» уже стояло бы в CLAUDE.md, тест бы упал посреди
работы над физикой, и самым дешёвым выходом оказалось бы ослабить проверку.

Поэтому здесь заведена **синтетическая модель** с ровно теми двумя видами
непрерывной динамики, которые есть в DESIGN.md:

* релаксация к равновесию — ксенон и иод (§4.3), усталость и мораль (§5.3);
* линейное накопление — выгорание (§4.5) и износ (§4.6).

Модель нарочно не трогает ``GameState``: миграция и фикстура ради теста — это
хвост, виляющий собакой. Когда настоящие величины появятся, они обязаны прийти
со своими тестами эквивалентности, а этот останется проверять сам компаратор.
"""

from __future__ import annotations

import math

import pytest
from support.state_compare import (
    DRIFT_HORIZON_TICKS,
    REL_TOL,
    assert_states_equivalent,
    assert_states_exact,
    compare_states,
    relative_difference,
)

#: Ксенон-135, T½ = 9.2 ч (BALANCE.md, §2.3).
LAMBDA_XE = math.log(2) / 9.2
#: Равновесная концентрация, к которой идёт релаксация.
XENON_EQUILIBRIUM = 3.0e17
#: Доля кампании, выгорающая за час: 18 месяцев ≈ 13 140 ч (BALANCE.md, §2.5).
BURN_RATE = 1.0 / 13_140


def initial() -> dict:
    return {"xenon": 1.0e15, "burnup": 0.0, "cash_cents": 5_000_000_000_0}


def advance(state: dict, hours: int) -> dict:
    """Замкнутая формула на интервал — то, чего И4 требует от подсистем.

    Релаксация к равновесию и линейное накопление считаются сразу на ``hours``
    часов, без цикла. Именно так обязан выглядеть шаг батчевого догона.
    """
    decay = math.exp(-LAMBDA_XE * hours)
    return {
        "xenon": XENON_EQUILIBRIUM + (state["xenon"] - XENON_EQUILIBRIUM) * decay,
        "burnup": state["burnup"] + BURN_RATE * hours,
        # Деньги целые: дискретное состояние обязано совпадать точно.
        "cash_cents": state["cash_cents"] - 1_000_00 * hours,
    }


def advance_euler(state: dict, hours: int) -> dict:
    """Неверное интегрирование: линеаризация экспоненты на весь интервал.

    Ошибка растёт с длиной блока, а не с числом операций. Такой код проходит
    потиково и разваливается при батчинге — ровно тот класс ошибок, ради
    которого допуск выставлен с запасом в четыре порядка.
    """
    return {
        "xenon": XENON_EQUILIBRIUM
        + (state["xenon"] - XENON_EQUILIBRIUM) * max(0.0, 1.0 - LAMBDA_XE * hours),
        "burnup": state["burnup"] + BURN_RATE * hours,
        "cash_cents": state["cash_cents"] - 1_000_00 * hours,
    }


def run_stepwise(hours: int, step=advance) -> dict:
    state = initial()
    for _ in range(hours):
        state = step(state, 1)
    return state


def run_blocks(blocks, step=advance) -> dict:
    state = initial()
    for block in blocks:
        state = step(state, block)
    return state


#: Неравные блоки: настоящий догон прыгает от события к событию, а не ровными
#: кусками. Сумма — 1000 часов.
BLOCKS = (13, 200, 1, 486, 300)

#: Короткие блоки — 20 часов, около двух периодов полураспада ксенона.
#:
#: Отдельная константа нужна из-за неочевидного свойства релаксации: на длинном
#: горизонте величина приходит к равновесию, и **любая** ошибка интегрирования
#: смывается вместе с памятью о начальном условии. Проверять точность формулы
#: надо там, где величина ещё движется, иначе тест подтвердит что угодно. То же
#: правило относится к будущим тестам ксенона, усталости и морали.
SHORT_BLOCKS = (3, 5, 2, 10)


def test_exact_equality_is_unachievable():
    """Прежняя формулировка И4 («бит-в-бит») невыполнима — вот доказательство.

    Тест закрепляет причину, по которой инвариант расщеплён. Если он однажды
    начнёт проходить, значит кто-то убрал из состояния непрерывные величины,
    и расщепление можно пересматривать.
    """
    with pytest.raises(AssertionError):
        assert_states_exact(run_blocks(BLOCKS), run_stepwise(sum(BLOCKS)))


def test_equivalent_within_tolerance():
    """Тот же расчёт по контракту И4а расхождением не считается."""
    assert_states_equivalent(run_blocks(BLOCKS), run_stepwise(sum(BLOCKS)))


def test_discrete_part_stays_exact_anyway():
    """Допуск не должен незаметно распространяться на деньги и счётчики."""
    blocks = run_blocks(BLOCKS)
    stepwise = run_stepwise(sum(BLOCKS))
    assert blocks["cash_cents"] == stepwise["cash_cents"]

    blocks["cash_cents"] += 1
    problems = compare_states(blocks, stepwise)
    assert any("cash_cents" in item for item in problems), problems


@pytest.mark.parametrize("horizon", [1_000, 10_000, DRIFT_HORIZON_TICKS])
def test_drift_does_not_grow_with_horizon(horizon):
    """Погрешность обязана быть ограниченной, а не накапливаться.

    Формула, набирающая ошибку линейно по числу блоков, на тысяче часов пройдёт
    по допуску и уведёт партию в сторону за десять игровых лет. Поэтому горизонт
    проверки — ``CATCHUP_DRIFT_HORIZON_TICKS``, а не удобная тысяча.
    """
    # Блоки по 24 часа: на горизонте в 10 лет это 3650 стыков, где ошибка и
    # накапливалась бы, будь она накапливающейся.
    blocks = [24] * (horizon // 24)
    blocks.append(horizon - sum(blocks))

    assert_states_equivalent(run_blocks(blocks), run_stepwise(horizon))


def test_drift_at_ten_years_is_orders_below_tolerance():
    """Запас допуска измеряется, а не предполагается.

    Если однажды окажется, что реальная погрешность подобралась к ``REL_TOL``,
    это повод разбираться с формулой, а не расширять допуск.
    """
    horizon = DRIFT_HORIZON_TICKS
    blocks = [24] * (horizon // 24)
    blocks.append(horizon - sum(blocks))

    fast = run_blocks(blocks)
    slow = run_stepwise(horizon)
    worst = max(
        relative_difference(fast["xenon"], slow["xenon"]),
        relative_difference(fast["burnup"], slow["burnup"]),
    )
    assert worst < REL_TOL / 100, f"погрешность {worst:.3e} подобралась к допуску {REL_TOL:.0e}"


def test_wrong_integrator_is_caught():
    """У проверки есть зубы: неверная замкнутая формула не проходит.

    Без этого теста нельзя утверждать, что допуск отличает шум округления от
    настоящей ошибки — он мог бы просто пропускать всё подряд.

    Горизонт короткий (``SHORT_BLOCKS``) намеренно: см. комментарий к этой
    константе о том, как релаксация к равновесию прячет ошибки.
    """
    problems = compare_states(
        run_blocks(SHORT_BLOCKS, step=advance_euler),
        run_stepwise(sum(SHORT_BLOCKS)),
    )
    assert any("xenon" in item for item in problems), problems


def test_correct_integrator_passes_on_the_same_horizon():
    """Контроль к предыдущему тесту: на том же горизонте верная формула чиста.

    Иначе нельзя отличить «компаратор поймал ошибку» от «компаратор ругается
    на любой короткий горизонт».
    """
    assert_states_equivalent(run_blocks(SHORT_BLOCKS), run_stepwise(sum(SHORT_BLOCKS)))
