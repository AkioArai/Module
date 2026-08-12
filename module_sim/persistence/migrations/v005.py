"""v005: финансы — долг, ковенанты, банкротство (фаза 2).

Добавляется ``game.finance``. Старая партия получает компанию без долга и без
балансовой стоимости станции: до фазы 2 ни того, ни другого в игре не было, и
задним числом выдумывать компании кредитную историю неоткуда.

Практическое следствие для игрока: партия, начатая в фазе 1, продолжится
беспроцентной. Это щедро, но честнее, чем внезапно повесить на неё долг, о
котором она не знала.

ВЫПУЩЕНА. Не редактировать — только добавлять новую миграцию.
"""

from __future__ import annotations

FROM_VERSION = 4
TO_VERSION = 5
DESCRIPTION = "добавлены финансы: долг, ковенанты, статус банкротства"

DEFAULT = {
    "debt_cents": 0,
    "assets_cents": 0,
    "months_below_covenant": 0,
    "covenant_breaches": 0,
    "status": "норма",
    "grace_ends_tick": 0,
    "discharge_penalty_until_tick": 0,
    "discharges": 0,
    "revenue_at_last_month_cents": 0,
}


def migrate(data: dict) -> dict:
    data["game"].setdefault("finance", dict(DEFAULT))
    return data
