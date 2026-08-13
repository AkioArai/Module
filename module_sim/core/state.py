"""Состояние симуляции.

Фаза 0: минимальный набор полей. Задача этого модуля сейчас — не описать игру,
а зафиксировать **правила** описания состояния, вокруг которых строятся сейвы и
миграции.

Правила (CLAUDE.md, И5; SAVEFORMAT.md, §2):

* Всё состояние — ``@dataclass(slots=True)``. ``slots`` не косметика: это
  память и скорость атрибутного доступа в горячем цикле догона.
* Деньги — целые копейки (``*_cents``). Float для денег запрещён: он ломает
  балансовое тождество на длинных партиях и делает диффы фикстур
  нестабильными.
* Имена полей — английский ``snake_case``: их читает скриптовый уровень
  (DESIGN.md, §7).
* ``to_dict``/``from_dict`` — граница между кодом и файлом. ``from_dict``
  получает данные, **уже приведённые миграциями** к текущей версии, поэтому не
  занимается совместимостью: отсутствие поля здесь — это баг миграции, а не
  ситуация, которую надо молча пережить.

Любое изменение полей ниже обязано сопровождаться новой миграцией и фикстурой
в том же коммите.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "SPEED_MULTIPLIER",
    "CompanyState",
    "FinanceState",
    "FuelState",
    "GameState",
    "MarketState",
    "PpaContract",
    "ReactorState",
    "Speed",
]


class Speed:
    """Скорости игрового времени (DESIGN.md, §2.1). Строки, а не Enum: они
    уходят в сейв как есть и должны читаться глазами в JSON."""

    PAUSED = "paused"
    X1 = "x1"
    X5 = "x5"
    X50 = "x50"

    ALL = (PAUSED, X1, X5, X50)


#: Сколько тиков в реальную секунду даёт каждая скорость.
SPEED_MULTIPLIER: dict[str, float] = {
    Speed.PAUSED: 0.0,
    Speed.X1: 1.0,
    Speed.X5: 5.0,
    Speed.X50: 50.0,
}


@dataclass(slots=True)
class CompanyState:
    """Компания игрока. В фазе 0 — только название и касса."""

    name: str = "Модуль"
    cash_cents: int = 5_000_000_000_0  # 500 млн ₽, BALANCE.md §5 CASH_START

    def to_dict(self) -> dict:
        return {"name": self.name, "cash_cents": self.cash_cents}

    @classmethod
    def from_dict(cls, data: dict) -> CompanyState:
        return cls(name=data["name"], cash_cents=data["cash_cents"])


@dataclass(slots=True)
class ReactorState:
    """Блок. Фаза 1: мощность и выгорание, без кинетики и ксенона.

    Первые непрерывные величины партии — с них контракт И4а перестаёт быть
    теоретическим (CLAUDE.md, §И4а).
    """

    #: Уставка и текущая мощность в долях номинала, 0…1.
    power_setpoint: float = 0.0
    power_level: float = 0.0
    #: Доля выработанной топливной кампании. Единица — топливо выработано;
    #: обрыва на ней в фазе 1 нет, см. physics/reactor.py.
    burnup: float = 0.0

    def to_dict(self) -> dict:
        return {
            "power_setpoint": self.power_setpoint,
            "power_level": self.power_level,
            "burnup": self.burnup,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ReactorState:
        return cls(
            power_setpoint=data["power_setpoint"],
            power_level=data["power_level"],
            burnup=data["burnup"],
        )


@dataclass(slots=True)
class PpaContract:
    """Договор на поставку по фиксированной цене (DESIGN.md, §3.3).

    ``volume_mwh`` — обязательство в каждый час, а не суммарное за срок: PPA
    отрезает от ценовых пиков и превращает любой простой в прямой убыток,
    потому что штраф платится, даже когда блок стоит.
    """

    volume_mwh: float = 0.0
    price_cents: int = 0
    signed_tick: int = 0
    ends_tick: int = 0

    def to_dict(self) -> dict:
        return {
            "volume_mwh": self.volume_mwh,
            "price_cents": self.price_cents,
            "signed_tick": self.signed_tick,
            "ends_tick": self.ends_tick,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PpaContract:
        return cls(**{key: data[key] for key in cls.__slots__})


@dataclass(slots=True)
class MarketState:
    """Расчёты за энергию (economy/market.py).

    Хранится накопленная выручка и уже выплаченное по ней. Разница между ними
    и есть очередной платёж — так ошибка округления не накапливается по
    периодам, сколько бы их ни прошло.

    Начисленное держится в трёх копилках, а не в одной: игрок обязан видеть,
    сколько принёс спот, сколько договор и сколько отняли штрафы. Свёрнутая в
    одно число выручка не отвечает на вопрос «а если бы я не хеджировался»,
    а он и есть главный экономический вопрос игры.

    ``noise`` и ``demand_deviation`` — случайная часть цены. Они меняются
    **ровно раз в игровые сутки**, дискретным событием: цена, разыгрываемая
    каждый час, превратила бы догон в потиковый цикл (economy/prices.py).
    """

    energy_sold_mwh: float = 0.0
    revenue_paid_cents: int = 0
    #: Отклонение цены от уровня, безразмерное. Процесс возврата к среднему.
    noise: float = 0.0
    #: Относительное отклонение спроса от нормы, ``(D − D_norm) / D_norm``.
    demand_deviation: float = 0.0
    #: Начисленное, копейки дробные: в целые превращается только в расчёте.
    spot_accrued_cents: float = 0.0
    ppa_accrued_cents: float = 0.0
    penalty_accrued_cents: float = 0.0
    #: Накопленная недопоставка по договору и её значение на прошлом расчёте:
    #: из разницы видно, был ли срыв поставки в этих сутках.
    ppa_shortfall_mwh: float = 0.0
    ppa_shortfall_settled_mwh: float = 0.0
    ppa: PpaContract | None = None

    def to_dict(self) -> dict:
        return {
            "energy_sold_mwh": self.energy_sold_mwh,
            "revenue_paid_cents": self.revenue_paid_cents,
            "noise": self.noise,
            "demand_deviation": self.demand_deviation,
            "spot_accrued_cents": self.spot_accrued_cents,
            "ppa_accrued_cents": self.ppa_accrued_cents,
            "penalty_accrued_cents": self.penalty_accrued_cents,
            "ppa_shortfall_mwh": self.ppa_shortfall_mwh,
            "ppa_shortfall_settled_mwh": self.ppa_shortfall_settled_mwh,
            "ppa": self.ppa.to_dict() if self.ppa is not None else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MarketState:
        contract = data["ppa"]
        return cls(
            energy_sold_mwh=data["energy_sold_mwh"],
            revenue_paid_cents=data["revenue_paid_cents"],
            noise=data["noise"],
            demand_deviation=data["demand_deviation"],
            spot_accrued_cents=data["spot_accrued_cents"],
            ppa_accrued_cents=data["ppa_accrued_cents"],
            penalty_accrued_cents=data["penalty_accrued_cents"],
            ppa_shortfall_mwh=data["ppa_shortfall_mwh"],
            ppa_shortfall_settled_mwh=data["ppa_shortfall_settled_mwh"],
            ppa=PpaContract.from_dict(contract) if contract is not None else None,
        )


@dataclass(slots=True)
class FuelState:
    """Топливо: запас на складе и цена рынка (economy/fuel.py).

    Поставки здесь не хранятся: заказанное едет событием ``fuel.delivery`` в
    очереди планировщика, а очередь и так уходит в сейв. Дублировать её списком
    в состоянии — значит завести две правды об одном факте и однажды получить
    поставку, которая доехала дважды.

    ``price_noise`` — состояние процесса цены, а не сама цена. Цена вычислима
    из него (``price_cents_per_ton``), а вычислимое в сейве не хранится: два
    поля рано или поздно разойдутся.
    """

    #: Стартовый запас — годовая потребность (BALANCE.md, §4: FUEL_START_TONS).
    #: Станция достаётся игроку с уже загруженным складом: заказывать топливо в
    #: первый же час, когда лаг поставки четыре месяца, было бы не решением, а
    #: обязательной формальностью.
    stock_tons: float = 24.0
    price_noise: float = 0.0

    def to_dict(self) -> dict:
        return {"stock_tons": self.stock_tons, "price_noise": self.price_noise}

    @classmethod
    def from_dict(cls, data: dict) -> FuelState:
        return cls(stock_tons=data["stock_tons"], price_noise=data["price_noise"])


@dataclass(slots=True)
class FinanceState:
    """Долг, ставка и состояние компании (economy/finance.py).

    Всё в целых копейках и меняется только в месячном событии — иначе округление
    происходило бы в разных точках у батчевого и потикового пути (И4а).
    """

    debt_cents: int = 0
    assets_cents: int = 0
    #: Месяцев подряд, когда выручка не покрыла проценты и расходы.
    months_below_covenant: int = 0
    #: Сколько раз ковенант был нарушен совсем: каждое нарушение дорожает долг.
    covenant_breaches: int = 0
    status: str = "норма"
    #: Тик, когда истекает срок на исправление. Ноль — срока нет.
    grace_ends_tick: int = 0
    #: До какого тика действует наказание за списание долга.
    discharge_penalty_until_tick: int = 0
    discharges: int = 0
    #: Снимок выплаченной выручки на прошлом месячном событии — из разницы
    #: считается, покрыл ли месяц свои расходы.
    revenue_at_last_month_cents: int = 0

    def to_dict(self) -> dict:
        return {
            "debt_cents": self.debt_cents,
            "assets_cents": self.assets_cents,
            "months_below_covenant": self.months_below_covenant,
            "covenant_breaches": self.covenant_breaches,
            "status": self.status,
            "grace_ends_tick": self.grace_ends_tick,
            "discharge_penalty_until_tick": self.discharge_penalty_until_tick,
            "discharges": self.discharges,
            "revenue_at_last_month_cents": self.revenue_at_last_month_cents,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FinanceState:
        return cls(**{key: data[key] for key in cls.__slots__})


@dataclass(slots=True)
class GameState:
    """Корень состояния партии.

    ``epoch`` — unix-время, которому соответствует ``tick == 0``. Игровая дата
    выводится из ``epoch + tick`` часов, поэтому календарь не хранится: он
    вычисляем, а дублирование вычислимого в сейве — источник рассогласований.
    """

    seed: int = 0
    tick: int = 0
    epoch: float = 0.0
    speed: str = Speed.X1
    company: CompanyState = field(default_factory=CompanyState)
    #: Счётчики потоков RNG, а не состояние генератора (core/rng.py).
    rng_counters: dict[str, int] = field(default_factory=dict)
    #: Очередь дискретных событий (core/events/scheduler.py). Хранится сырым
    #: словарём: ``Scheduler`` живёт в ``Simulation`` и синхронизируется сюда
    #: тем же способом, что и счётчики RNG, — единственной точкой перед записью.
    scheduler: dict = field(default_factory=lambda: {"events": [], "next_order": 0})
    reactor: ReactorState = field(default_factory=ReactorState)
    market: MarketState = field(default_factory=MarketState)
    fuel: FuelState = field(default_factory=FuelState)
    finance: FinanceState = field(default_factory=FinanceState)
    #: Приказы сырыми словарями — как и очередь событий. Объекты ``Order``
    #: создаются поверх по надобности (core/orders.py).
    orders: list[dict] = field(default_factory=list)
    next_order_id: int = 0

    def to_dict(self) -> dict:
        return {
            "seed": self.seed,
            "tick": self.tick,
            "epoch": self.epoch,
            "speed": self.speed,
            "company": self.company.to_dict(),
            "rng": {"counters": dict(self.rng_counters)},
            "scheduler": {
                "events": [dict(event) for event in self.scheduler.get("events", [])],
                "next_order": self.scheduler.get("next_order", 0),
            },
            "reactor": self.reactor.to_dict(),
            "market": self.market.to_dict(),
            "fuel": self.fuel.to_dict(),
            "finance": self.finance.to_dict(),
            "orders": [dict(order) for order in self.orders],
            "next_order_id": self.next_order_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> GameState:
        scheduler = data["scheduler"]
        return cls(
            seed=data["seed"],
            tick=data["tick"],
            epoch=data["epoch"],
            speed=data["speed"],
            company=CompanyState.from_dict(data["company"]),
            rng_counters=dict(data["rng"]["counters"]),
            scheduler={
                "events": [dict(event) for event in scheduler["events"]],
                "next_order": scheduler["next_order"],
            },
            reactor=ReactorState.from_dict(data["reactor"]),
            market=MarketState.from_dict(data["market"]),
            fuel=FuelState.from_dict(data["fuel"]),
            finance=FinanceState.from_dict(data["finance"]),
            orders=[dict(order) for order in data["orders"]],
            next_order_id=data["next_order_id"],
        )
