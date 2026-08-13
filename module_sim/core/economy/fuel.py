"""Топливо: цена, лаг поставки, склад (DESIGN.md, §3.4).

Обогащённый уран не покупают на споте. Его заказывают контрактом, цена
фиксируется в момент заказа, а привозят через месяцы — и в этом лаге вся
механика. Заказ сегодня по сегодняшней цене — это ставка на то, каким будет
рынок через четыре месяца, и единственный способ выиграть у процесса цены,
который сам по себе честно возвращается к среднему.

Три решения, которые стоит объяснить.

**Поставки не хранятся в состоянии.** Заказанное едет событием ``fuel.delivery``
в очереди планировщика, а очередь и так уходит в сейв (core/events/scheduler.py).
Список в ``FuelState`` был бы второй правдой о том же факте, и однажды поставка
доехала бы дважды.

**Платят при поставке, по цене заказа.** Деньги двигаются только в дискретном
событии (И4а, уровень 2), а цена берётся из полезной нагрузки события — та, что
была на момент подписания. Иначе контракт не был бы контрактом.

**Закупка автоматическая, пока игрок не вмешался.** Как и кредитная линия в
``finance``: компания не самоубийца и сама поддерживает запас. Игрок управляет
не фактом закупки, а её моментом — заказать больше и заранее, когда цена внизу.
Автозакупка при этом никогда не опережает расход: она смотрит на склад, а
остановленный блок топлива не ест.

Чего здесь нет: перегрузки зоны и того, что происходит с мощностью, когда
топливо кончилось. Это физика (DESIGN.md, §4.5), её время — фаза 5. Сейчас
пустой склад означает ровно одно: автозакупка не успела, и это видно.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from module_sim.core.clock import MONTH_HOURS
from module_sim.core.economy import apply_cash, mean_reverting_step
from module_sim.core.events import registry
from module_sim.core.events.scheduler import ScheduledEvent
from module_sim.core.physics.reactor import CAMPAIGN_HOURS, NOMINAL_ELECTRIC_MW
from module_sim.core.state import FuelState

if TYPE_CHECKING:  # pragma: no cover - подсистемы не тянут sim во время работы
    from module_sim.core.sim import Simulation

__all__ = [
    "COVERAGE_TARGET_MONTHS",
    "DELIVERY_KIND",
    "DELIVERY_LAG_HOURS",
    "MONTH_KIND",
    "PRICE_BASE_CENTS_PER_TON",
    "TONS_PER_CAMPAIGN",
    "consume",
    "in_transit_tons",
    "inventory_value_cents",
    "order",
    "price_cents_per_ton",
]

MONTH_KIND = "fuel.month"
DELIVERY_KIND = "fuel.delivery"
PRICE_STREAM = "fuel.price"

#: BALANCE.md, §4. Цена тонны готовой топливной сборки — 200 млн ₽.
PRICE_BASE_CENTS_PER_TON = 20_000_000_000

#: Возврат к среднему и разброс логарифма цены, за шаг = за игровой месяц.
PRICE_THETA_PER_MONTH = 0.05
PRICE_SIGMA = 0.12

#: Лаг поставки — четыре игровых месяца (BALANCE.md, §4).
DELIVERY_LAG_HOURS = 4 * MONTH_HOURS

#: Сколько топлива съедает кампания (BALANCE.md, §2.5, §4). Ориентир —
#: одноблочная PWR: около 36 т за 18 месяцев на номинале.
TONS_PER_CAMPAIGN = 36.0

#: Расход на МВт·ч отпущенной энергии. Через выработку, а не через время: стоящий
#: блок топлива не ест, а работающий вполсилы ест вполовину.
TONS_PER_MWH = TONS_PER_CAMPAIGN / (NOMINAL_ELECTRIC_MW * CAMPAIGN_HOURS)

#: Расход за месяц на номинале — мера, в которой удобно мерить запас.
TONS_PER_MONTH_NOMINAL = TONS_PER_MWH * NOMINAL_ELECTRIC_MW * MONTH_HOURS

#: Целевое покрытие склада: лаг поставки плюс столько же про запас. Меньше —
#: любая задержка оставляет станцию без топлива, больше — деньги лежат на складе.
COVERAGE_TARGET_MONTHS = 8

#: Меньше этого не заказывают. Топливо приходит сборками, а не граммами, и
#: контракт на полкило урана не подписывают.
#:
#: Порог нужен ещё и арифметически. Покрытие склада — непрерывная величина, и
#: в батчевом пути она отличается от потикового в последних разрядах (И4а,
#: уровень 3). Без порога автозакупка на этой разнице оформляла бы контракт на
#: 10⁻¹³ тонны — в одном пути на такую, в другом на другую, — и партии
#: разъезжались бы содержимым очереди событий, то есть уровнем 1 контракта.
MIN_ORDER_TONS = 0.5


def price_cents_per_ton(state: FuelState) -> int:
    """Цена тонны сегодня, копеек.

    Логнормальная: случайность ходит по логарифму цены, поэтому цена не может
    стать отрицательной ни на каком выбросе, а движения вверх и вниз
    симметричны в процентах — так ведут себя настоящие сырьевые рынки.
    """
    return round(PRICE_BASE_CENTS_PER_TON * math.exp(state.price_noise))


def inventory_value_cents(state: FuelState) -> int:
    """Стоимость склада по текущей цене. Входит в активы (economy/finance.py)."""
    return round(state.stock_tons * price_cents_per_ton(state))


def consume(simulation: Simulation, produced_mwh: float) -> None:
    """Списать топливо за выработанную энергию.

    Непрерывная величина, но замкнутая на интервал даром: расход пропорционален
    выработке, а выработку реактор уже посчитал замкнуто (physics/reactor.py).

    Ниже нуля склад не уходит. Обрезание на нуле аддитивно — величина
    монотонно убывает, и оба пути упираются в один и тот же ноль, — поэтому
    контракт И4а от него не страдает.
    """
    if produced_mwh <= 0.0:
        return
    fuel = simulation.state.fuel
    fuel.stock_tons = max(0.0, fuel.stock_tons - produced_mwh * TONS_PER_MWH)


def in_transit_tons(simulation: Simulation) -> float:
    """Сколько тонн уже заказано и едет.

    Читается из очереди событий — единственного места, где эта правда живёт.
    Обход очереди стоит дорого, поэтому вызывается раз в игровой месяц.
    """
    return sum(
        float(event.payload.get("tons", 0.0))
        for event in simulation.scheduler.pending()
        if event.kind == DELIVERY_KIND
    )


def order(simulation: Simulation, tons: float) -> int:
    """Заказать ``tons`` тонн по сегодняшней цене. Вернуть тик поставки.

    Деньги не списываются: контракт подписан, платить будем по факту приёмки.
    """
    if tons <= 0.0:
        raise ValueError("объём заказа должен быть положительным")
    arrival = simulation.state.tick + DELIVERY_LAG_HOURS
    simulation.schedule(
        arrival,
        DELIVERY_KIND,
        {"tons": tons, "price_cents": price_cents_per_ton(simulation.state.fuel)},
    )
    return arrival


def _procure(simulation: Simulation) -> None:
    """Дозаказать до целевого покрытия, если склад с поставками ниже него."""
    fuel = simulation.state.fuel
    target = TONS_PER_MONTH_NOMINAL * COVERAGE_TARGET_MONTHS
    deficit = target - fuel.stock_tons - in_transit_tons(simulation)
    if deficit >= MIN_ORDER_TONS:
        order(simulation, deficit)


@registry.handler(MONTH_KIND)
def _on_month(simulation: Simulation, event: ScheduledEvent) -> None:
    fuel = simulation.state.fuel
    fuel.price_noise = mean_reverting_step(
        fuel.price_noise,
        PRICE_THETA_PER_MONTH,
        PRICE_SIGMA,
        simulation.rng.stream(PRICE_STREAM).normal(),
    )
    _procure(simulation)
    simulation.schedule_in(MONTH_HOURS, MONTH_KIND)


@registry.handler(DELIVERY_KIND)
def _on_delivery(simulation: Simulation, event: ScheduledEvent) -> None:
    """Приёмка: топливо на склад, деньги по цене договора."""
    tons = float(event.payload["tons"])
    price = int(event.payload["price_cents"])
    simulation.state.fuel.stock_tons += tons
    apply_cash(simulation, -round(tons * price))
