"""Форма цены и её замкнутое интегрирование (economy/prices.py).

Здесь проверяется не игра, а арифметика, на которой она стоит. Если интеграл по
префикс-суммам считает не то же, что честная сумма по мелким шагам, — рынок
будет ошибаться тихо, одинаково в обоих путях, и ни один тест на
эквивалентность догона этого не увидит.

Поэтому основной приём файла — сверка с **независимым** способом посчитать то
же самое: численным интегрированием мелким шагом.
"""

from __future__ import annotations

import itertools
import math

import pytest
from support.state_compare import relative_difference

from module_sim.core.economy import prices

TABLE = prices.day_table(day=0, noise=0.0, demand_deviation=0.0)


def numeric_integral(table: prices.HourTable, u0: float, u1: float, value: float, slope: float):
    """Тот же интеграл, посчитанный в лоб мелким шагом. Эталон для сверки."""
    steps = 20_000
    step = (u1 - u0) / steps
    total = 0.0
    for index in range(steps):
        u = u0 + (index + 0.5) * step
        hour = min(int(u), prices.HOURS_PER_DAY - 1)
        total += table.values[hour] * (value + slope * (u - u0)) * step
    return total


# -- профили ---------------------------------------------------------------


def test_daily_profile_covers_the_day():
    assert len(prices.DAILY_PROFILE) == prices.HOURS_PER_DAY


def test_daily_profile_redistributes_rather_than_lifts():
    """Средний коэффициент около единицы: профиль двигает цену внутри суток,
    а не поднимает её уровень. Иначе базовая цена перестала бы быть базовой."""
    assert 0.95 < prices.PROFILE_MEAN < 1.05


def test_evening_peak_is_the_highest():
    peak_hour = max(range(24), key=lambda hour: prices.DAILY_PROFILE[hour])
    assert peak_hour == 18


def test_season_peaks_in_january_and_bottoms_half_a_year_later():
    peak = prices.season_factor(prices.SEASON_PEAK_DAY)
    trough = prices.season_factor(prices.SEASON_PEAK_DAY + prices.DAYS_PER_YEAR // 2)
    assert peak > 1.0 > trough
    assert relative_difference(peak, 1.0 + prices.SEASON_AMPLITUDE) < 1e-12


def test_season_is_periodic_over_a_year():
    for day in (0, 40, 200, 364):
        assert (
            relative_difference(
                prices.season_factor(day), prices.season_factor(day + prices.DAYS_PER_YEAR)
            )
            < 1e-12
        )


def test_noise_cannot_make_the_price_negative():
    """Клип шума — не украшение: множитель ``(1 + σ)`` обязан остаться
    положительным на любом выбросе, иначе цена уходит в минус, а это другая
    игра."""
    for noise in (-50.0, -1.0, 50.0):
        assert prices.day_level_cents(0, noise, 0.0) > 0.0


def test_price_is_a_function_of_the_hour_of_day():
    """Одинаковый час разных суток при одинаковом уровне даёт одинаковую цену —
    то самое свойство, ради которого рельеф отделён от случайности."""
    assert prices.spot_price_cents(5, 0.0, 0.0) == prices.spot_price_cents(
        5 + 24 * prices.DAYS_PER_YEAR, 0.0, 0.0
    )


def test_spot_price_matches_the_day_table():
    table = prices.day_table(day=3, noise=0.1, demand_deviation=-0.05)
    for hour in range(24):
        tick = 3 * 24 + hour
        assert prices.spot_price_cents(tick, 0.1, -0.05) == pytest.approx(
            table.values[hour], rel=1e-15
        )


# -- интегралы -------------------------------------------------------------


@pytest.mark.parametrize(
    ("u0", "u1", "value", "slope"),
    [
        (0.0, 24.0, 1000.0, 0.0),  # сутки на постоянной мощности
        (5.0, 6.0, 500.0, 0.0),  # один час
        (0.0, 10.0, 0.0, 100.0),  # разгон с нуля
        (5.5, 12.25, 550.0, -50.0),  # дробные края и сброс мощности
        (17.0, 19.0, 300.0, 25.0),  # вечерний пик
        (23.0, 24.0, 1000.0, 0.0),  # последний час суток
    ],
)
def test_integral_matches_numeric(u0, u1, value, slope):
    exact = TABLE.integrate(u0, u1, value, slope)
    assert relative_difference(exact, numeric_integral(TABLE, u0, u1, value, slope)) < 1e-4


def test_integral_is_additive_at_arbitrary_cuts():
    """Главное свойство: сумма по кускам равна целому.

    Именно на нём держится И4а. Батчевый путь режет время моментами событий,
    потиковый — по часу; если разбиение меняет ответ, совпасть они не могут
    никакими допусками.
    """
    whole = TABLE.integrate(0.0, 24.0, 200.0, 30.0)
    cuts = [0.0, 0.3, 1.0, 5.5, 5.5001, 12.0, 17.75, 23.0, 24.0]
    pieces = sum(
        TABLE.integrate(left, right, 200.0 + 30.0 * left, 30.0)
        for left, right in itertools.pairwise(cuts)
    )
    assert relative_difference(whole, pieces) < 1e-12


def test_empty_and_reversed_intervals_give_nothing():
    assert TABLE.integrate(5.0, 5.0, 100.0, 10.0) == 0.0
    assert TABLE.integrate(6.0, 5.0, 100.0, 10.0) == 0.0


def test_constant_power_integral_is_just_the_area():
    assert TABLE.integrate(0.0, 24.0, 1.0, 0.0) == pytest.approx(sum(TABLE.values), rel=1e-15)


def test_shortfall_table_clips_at_zero():
    floor_price = sorted(TABLE.values)[12]  # медиана: часть часов ниже, часть выше
    table = prices.shortfall_table(TABLE, floor_price)
    assert min(table.values) == 0.0
    assert max(table.values) == pytest.approx(max(TABLE.values) - floor_price)
    for cheap, replacement in zip(TABLE.values, table.values, strict=True):
        assert replacement == pytest.approx(max(cheap - floor_price, 0.0))


def test_fair_price_ignores_the_season():
    """Цена договора не зависит от календаря: контрагент видит сезон так же,
    как игрок, и продавать зиму по цене зимы ему незачем."""
    winter = prices.fair_price_cents(0.05, 0.0)
    assert winter == prices.fair_price_cents(0.05, 0.0)
    assert winter != prices.fair_price_cents(0.06, 0.0)


def test_moment_integral_keeps_precision_far_from_zero():
    """Интегралы считаются в координатах суток, а не абсолютных тиках.

    Проверяется тем, что таблица вообще не умеет принимать абсолютный тик:
    ``b(u)`` растёт как u², и на тике под миллион разность двух таких величин
    потеряла бы в вычитании разряды, из-за которых касса двух путей разошлась
    бы на копейки. Здесь фиксируется граница применимости: 0…24.
    """
    assert TABLE.b(24.0) == TABLE.b(100.0)
    assert TABLE.a(24.0) == TABLE.a(1_000_000.0)
    assert TABLE.b(24.0) < 24.0 * 24.0 * max(TABLE.values)
    assert math.isfinite(TABLE.b(24.0))
