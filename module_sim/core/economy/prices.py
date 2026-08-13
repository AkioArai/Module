"""Форма цены электроэнергии и её замкнутое интегрирование (DESIGN.md, §3.2).

Это модуль, ради которого фаза 2б устроена именно так. Цена меняется каждый
час — и наивная реализация этого требования ломает батчевый догон: чтобы
посчитать выручку за год, пришлось бы пройти 8760 часов по одному, и инвариант
И4 (не меньше 200 000 тиков/с) превратился бы в фикцию.

Выход — расщепить цену на две части с разной природой:

``P_spot(t) = уровень_суток(день, σ, спрос) · f_daily(час_суток)``

* **уровень суток** — всё случайное и медленное: сезон, отклонение спроса,
  шум σ. Он **постоянен внутри игровых суток** и меняется ровно один раз в
  сутки, дискретным событием планировщика (economy/market.py). Случайность в
  батчевом пути обязана разыгрываться там же, где в потиковом, иначе И4а не
  выполняется ничем;
* **``f_daily``** — детерминированная функция часа суток, таблица из 24
  коэффициентов (BALANCE.md, §3). Она и несёт весь почасовой рельеф.

Из такого расщепления следует главное свойство: на любом интервале внутри
суток цена — **ступенчатая функция с известными скачками**, а мощность блока —
линейная функция времени (physics/reactor.py). Интеграл произведения ступенчатой
и линейной функций берётся в замкнутом виде через две префикс-суммы, за O(1),
независимо от длины интервала.

Почему префикс-суммы, а не сумма по часам. Сумма по часам дала бы верный ответ,
но её стоимость линейна по игровому времени, и «догон за год» снова стал бы
циклом на 8760 итераций. Префиксы делают стоимость блока не зависящей от того,
сутки в нём или час.

Отдельно про точность. Все интегралы считаются в **локальных координатах
суток** (u ∈ [0, 24]), а не в абсолютных тиках. Это не косметика: величина
``∫ s·f(s) ds`` растёт как t², и на тике 800 000 разность двух таких величин
потеряла бы в вычитании столько разрядов, что касса батчевого и потикового пути
разошлась бы на копейки — а она обязана совпадать точно (И4а, уровень 2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "DAILY_PROFILE",
    "DAYS_PER_YEAR",
    "EPSILON_DEMAND",
    "HOURS_PER_DAY",
    "PROFILE_MEAN",
    "P_BASE_CENTS",
    "SEASON_AMPLITUDE",
    "SEASON_PEAK_DAY",
    "HourTable",
    "day_index",
    "day_level_cents",
    "day_table",
    "fair_price_cents",
    "hour_of_day",
    "season_factor",
    "shortfall_table",
    "spot_price_cents",
]

HOURS_PER_DAY = 24
DAYS_PER_YEAR = 365

#: BALANCE.md, §3: 3000 ₽ за МВт·ч в копейках.
P_BASE_CENTS = 300_000.0

#: Суточный профиль, 24 коэффициента (BALANCE.md, §3). Два пика: утренний
#: (8–10) и вечерний (17–19). Вечерний выше — именно его отнимает ксеноновая
#: яма, когда в фазе 5 появится физика.
DAILY_PROFILE: tuple[float, ...] = (
    0.72, 0.68, 0.65, 0.64, 0.66, 0.74, 0.88, 1.05,
    1.18, 1.20, 1.15, 1.10, 1.08, 1.06, 1.05, 1.08,
    1.16, 1.30, 1.34, 1.28, 1.14, 1.00, 0.88, 0.78,
)  # fmt: skip

#: Сезонная синусоида: зимний максимум (BALANCE.md, §3).
SEASON_AMPLITUDE = 0.18
SEASON_PEAK_DAY = 15

#: Эластичность цены по спросу (BALANCE.md, §3).
EPSILON_DEMAND = 0.8

#: Предел, в котором держится случайный множитель. Нужен не для реализма, а
#: чтобы множитель ``(1 + σ)`` не мог обратиться в ноль или уйти в минус на
#: выбросе: отрицательная цена — это другая игра, и вводить её надо осознанно,
#: а не через хвост распределения.
NOISE_CLAMP = 0.6


def day_index(tick: int) -> int:
    """Номер игровых суток. Тик 0 — первый час суток номер 0."""
    return tick // HOURS_PER_DAY


def hour_of_day(tick: int) -> int:
    return tick % HOURS_PER_DAY


def season_factor(day: int) -> float:
    """Сезонный множитель. Максимум в середине января (DESIGN.md, §3.2).

    Функция **дня**, а не часа: сезон меняется на порядки медленнее суток, и
    квантование по суткам даёт постоянный множитель внутри суток — то, на чём
    держится замкнутое интегрирование.
    """
    phase = 2.0 * math.pi * (day - SEASON_PEAK_DAY) / DAYS_PER_YEAR
    return 1.0 + SEASON_AMPLITUDE * math.cos(phase)


def clamp_noise(value: float) -> float:
    return max(-NOISE_CLAMP, min(NOISE_CLAMP, value))


def day_level_cents(day: int, noise: float, demand_deviation: float) -> float:
    """Уровень цены на сутки: всё, что не зависит от часа суток.

    ``demand_deviation`` — относительное отклонение спроса от нормы,
    ``(D − D_norm) / D_norm``. Детерминированная часть спроса уже сидит в
    суточном и сезонном профилях, поэтому отдельно разыгрывается только
    отклонение: погода, аварии конкурентов, вывод чужих мощностей в ремонт.
    """
    return (
        P_BASE_CENTS
        * season_factor(day)
        * (1.0 + EPSILON_DEMAND * demand_deviation)
        * (1.0 + clamp_noise(noise))
    )


def spot_price_cents(tick: int, noise: float, demand_deviation: float) -> float:
    """Цена спота в час ``tick``, копеек за МВт·ч.

    Отдельная функция, а не обращение к таблице: интерфейсу и консоли нужна
    одна цена, и собирать ради неё таблицу на сутки незачем.
    """
    return (
        day_level_cents(day_index(tick), noise, demand_deviation) * DAILY_PROFILE[hour_of_day(tick)]
    )


#: Средний коэффициент суточного профиля. Почти единица — профиль перераспределяет
#: цену внутри суток, а не поднимает её.
PROFILE_MEAN = sum(DAILY_PROFILE) / HOURS_PER_DAY


def fair_price_cents(noise: float, demand_deviation: float) -> float:
    """Средняя цена суток **без сезонной составляющей**, копеек за МВт·ч.

    От неё считается цена долгосрочного договора (economy/contracts.py). Сезон
    выброшен намеренно: контрагент видит календарь ровно так же, как игрок, и
    подписывать в январе годовой договор по зимней цене ему незачем. Торговать
    можно только тем, чего другая сторона не знает, — сегодняшним состоянием
    рынка, то есть шумом и спросом.
    """
    return (
        P_BASE_CENTS
        * PROFILE_MEAN
        * (1.0 + EPSILON_DEMAND * demand_deviation)
        * (1.0 + clamp_noise(noise))
    )


@dataclass(frozen=True, slots=True)
class HourTable:
    """Ступенчатая функция часа суток с двумя префикс-суммами.

    ``values[i]`` — значение на часе ``i`` суток, постоянное внутри часа.
    Префиксы дают два интеграла в замкнутом виде:

    * ``a(u) = ∫₀ᵘ f(s) ds`` — для постоянной мощности;
    * ``b(u) = ∫₀ᵘ s·f(s) ds`` — для мощности, идущей к уставке линейно.

    Вместе они интегрируют произведение ступенчатой цены на линейную мощность
    за O(1) — метод ``integrate``.
    """

    values: tuple[float, ...]
    a_prefix: tuple[float, ...]
    b_prefix: tuple[float, ...]

    @classmethod
    def build(cls, values: tuple[float, ...]) -> HourTable:
        a: list[float] = [0.0]
        b: list[float] = [0.0]
        for hour, value in enumerate(values):
            a.append(a[-1] + value)
            # ∫ от h до h+1 от s·f(s) ds = f·((h+1)² − h²)/2 = f·(h + 0.5).
            b.append(b[-1] + value * (hour + 0.5))
        return cls(values=values, a_prefix=tuple(a), b_prefix=tuple(b))

    def a(self, u: float) -> float:
        """``∫₀ᵘ f(s) ds``. ``u`` — часы от начала суток, 0…24."""
        whole = int(u)
        if whole >= HOURS_PER_DAY:
            return self.a_prefix[HOURS_PER_DAY]
        return self.a_prefix[whole] + (u - whole) * self.values[whole]

    def b(self, u: float) -> float:
        """``∫₀ᵘ s·f(s) ds``."""
        whole = int(u)
        if whole >= HOURS_PER_DAY:
            return self.b_prefix[HOURS_PER_DAY]
        return self.b_prefix[whole] + self.values[whole] * (u * u - whole * whole) * 0.5

    def integrate(self, u0: float, u1: float, value: float, slope: float) -> float:
        """``∫ f(u)·(value + slope·(u − u0)) du`` по ``[u0, u1]``.

        Формула точна для любого разбиения интервала: сумма по частям равна
        целому с точностью до последнего разряда. Это и есть требование И4а —
        батчевый путь режет время моментами событий, потиковый по часу, и
        совпадать они обязаны не «примерно по смыслу», а арифметически.
        """
        if u1 <= u0:
            return 0.0
        area = self.a(u1) - self.a(u0)
        if slope == 0.0:
            return value * area
        moment = self.b(u1) - self.b(u0)
        return (value - slope * u0) * area + slope * moment


def day_table(day: int, noise: float, demand_deviation: float) -> HourTable:
    """Таблица цен на игровые сутки, копеек за МВт·ч по часам.

    Собирается один раз в сутки и переиспользуется всеми блоками внутри них
    (economy/market.py). Двадцать четыре умножения на сутки — это на два
    порядка дешевле, чем считать цену на каждом часе догона, и главное: цена
    блока перестаёт зависеть от его длины.
    """
    level = day_level_cents(day, noise, demand_deviation)
    return HourTable.build(tuple(level * shape for shape in DAILY_PROFILE))


def shortfall_table(prices: HourTable, contract_price_cents: float) -> HourTable:
    """Таблица ``max(P_spot − P_ppa, 0)`` — цена замещения недопоставки.

    Клип на нуле означает: сорвав поставку в дешёвый час, компания не
    зарабатывает на этом (DESIGN.md, §3.3). Своя таблица нужна потому, что
    ``max`` нельзя вынести за интеграл — а с готовой таблицей штраф считается
    тем же замкнутым способом, что и выручка.
    """
    return HourTable.build(tuple(max(value - contract_price_cents, 0.0) for value in prices.values))
