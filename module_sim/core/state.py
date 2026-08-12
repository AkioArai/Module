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

__all__ = ["SPEED_MULTIPLIER", "CompanyState", "GameState", "Speed"]


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
    cash_cents: int = 5_000_000_000_0  # 5 млрд ₽, BALANCE.md §5 CASH_START

    def to_dict(self) -> dict:
        return {"name": self.name, "cash_cents": self.cash_cents}

    @classmethod
    def from_dict(cls, data: dict) -> CompanyState:
        return cls(name=data["name"], cash_cents=data["cash_cents"])


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
        )
