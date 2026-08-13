"""Экономика: рынок, контракты, финансы, банкротство.

* ``prices`` — форма цены и её замкнутое интегрирование;
* ``market`` — продажа энергии, расчёты, штрафы;
* ``contracts`` — договоры PPA;
* ``fuel`` — топливо: цена, лаг поставки, запас;
* ``finance`` — долг, проценты, ковенанты, банкротство.

Числа живут в BALANCE.md и в константах модулей; формулы — в DESIGN.md, §3.

Здесь же — то немногое, что нужно **всем** подсистемам экономики сразу.
Держать это в ``finance`` было бы удобнее на один импорт и хуже на всю
оставшуюся игру: топливо и рынок тянули бы за собой ковенанты и банкротство
ради одной функции, а зависимости в ``core/`` обязаны идти в одну сторону.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - подсистемы не тянут sim во время работы
    from module_sim.core.sim import Simulation

__all__ = [
    "BANKRUPTCY_ASSET_DISCOUNT",
    "apply_cash",
    "bankruptcy_discount",
    "mean_reverting_step",
]

#: Дисконт при вынужденной распродаже активов (BALANCE.md, §5).
#: Функцией, а не константой: значение читается из одного места и в фазе 3
#: станет зависеть от репутации, не ломая вызывающий код.
BANKRUPTCY_ASSET_DISCOUNT = 0.55


def bankruptcy_discount() -> float:
    return BANKRUPTCY_ASSET_DISCOUNT


def apply_cash(simulation: Simulation, delta_cents: int) -> None:
    """Изменить кассу на ``delta_cents``. Ушли в минус — ушли в долг.

    Кредитная линия автоматическая, и касса не уходит в минус никогда:
    отрицательные деньги на счету — это уже заём, и честнее называть его
    заёмом (economy/finance.py).

    Единственная точка, через которую подсистемы двигают деньги. Вызывать её
    можно **только из обработчиков событий**: непрерывное начисление округлялось
    бы в разных точках у батчевого и потикового пути, и касса разошлась бы, а
    она обязана совпадать точно (И4а, уровень 2).
    """
    company = simulation.state.company
    company.cash_cents += delta_cents
    if company.cash_cents < 0:
        simulation.state.finance.debt_cents += -company.cash_cents
        company.cash_cents = 0


def mean_reverting_step(value: float, theta: float, sigma: float, draw: float) -> float:
    """Один шаг процесса Орнштейна–Уленбека, точная дискретизация.

    ``theta`` — скорость возврата к среднему за **один шаг**, ``sigma`` —
    стационарное среднеквадратичное отклонение, ``draw`` — стандартная
    нормальная величина из именованного потока.

    Возврат к среднему обязателен для всего случайного в экономике: без него
    цена уходит в бесконечность на длинном догоне (DESIGN.md, §3.2). Формула
    точна для любого шага, а не является приближением: за сто лет отсутствия
    процесс останется в своей полосе, а не расползётся как случайное блуждание.
    """
    persistence = math.exp(-theta)
    return value * persistence + sigma * math.sqrt(1.0 - persistence * persistence) * draw
