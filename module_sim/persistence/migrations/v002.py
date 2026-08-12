"""v002: деньги переводятся из дробных рублей в целые копейки.

``company.balance`` (float, рубли) → ``company.cash_cents`` (int, копейки).

Причина смены типа, а не только имени: на длинной партии float накапливает
ошибку и балансовое тождество ``активы − обязательства = капитал``
(DESIGN.md, §3.1) перестаёт сходиться до копейки. Диффы фикстур в git при этом
шумят на последних разрядах. Деньги в игре целые — см. SAVEFORMAT.md, §2.

Округление банковское (``round``), выполняется один раз и навсегда: после этой
ступени дробных денег в сейвах не бывает.

ВЫПУЩЕНА. Не редактировать — только добавлять новую миграцию.
"""

from __future__ import annotations

FROM_VERSION = 1
TO_VERSION = 2
DESCRIPTION = "company.balance (рубли, float) → company.cash_cents (копейки, int)"


def migrate(data: dict) -> dict:
    company = data["game"]["company"]
    # pop с умолчанием, а не [] — миграция не имеет права упасть на данных
    # своей версии (SAVEFORMAT.md, §5, правило 5).
    rubles = company.pop("balance", 0.0)
    company["cash_cents"] = round(float(rubles) * 100)
    return data
