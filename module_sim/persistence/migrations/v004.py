"""v004: реактор, расчёты за энергию и приказы (фаза 1).

Добавляются четыре поля:

* ``game.reactor`` — мощность и выгорание;
* ``game.market`` — накопленная выработка и уже выплаченное за неё;
* ``game.orders`` и ``game.next_order_id`` — приказы игрока.

Старая партия получает остановленный блок с нулевым выгоранием. Это честно:
до фазы 1 реактора в игре не было вовсе, и выдумывать ему прошлое неоткуда.

ВЫПУЩЕНА. Не редактировать — только добавлять новую миграцию.
"""

from __future__ import annotations

FROM_VERSION = 3
TO_VERSION = 4
DESCRIPTION = "добавлены реактор, расчёты за энергию и приказы"


def migrate(data: dict) -> dict:
    game = data["game"]
    game.setdefault(
        "reactor",
        {"power_setpoint": 0.0, "power_level": 0.0, "burnup": 0.0},
    )
    game.setdefault("market", {"energy_sold_mwh": 0.0, "revenue_paid_cents": 0})
    game.setdefault("orders", [])
    game.setdefault("next_order_id", 0)
    return data
