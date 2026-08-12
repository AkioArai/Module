"""Игровое время: тик, скорость, догон пропущенного.

Инвариант И3 (CLAUDE.md): 1 тик = 1 игровой час, фиксированно. Инвариант И6:
пока процесс закрыт, время не идёт — пропущенное досчитывается **при запуске**,
и только здесь.

Инвариант И2 требует, чтобы внутри симуляции не читалось системное время.
Поэтому ``Clock`` никогда не вызывает ``time.time()``: момент «сейчас» приходит
параметром снаружи (из ``cli``/``ui``), а тест может подставить любой. Ровно два
места в игре имеют право знать wall-clock: запись ``saved_at`` и расчёт догона —
оба вне ``core``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from module_sim.core.state import SPEED_MULTIPLIER, GameState, Speed

__all__ = ["CATCHUP_TICKS_PER_SECOND", "MAX_CATCHUP_TICKS", "Clock", "Missed"]

#: Сколько игровых часов начисляется за реальную секунду отсутствия.
#: Совпадает со скоростью 1x: мир не движется быстрее оттого, что игрока не
#: было. Сутки отсутствия — почти десять игровых лет, отсюда и требование к
#: скорости батчинга (И4). Курс агрессивный и подлежит калибровке в фазе 2 —
#: см. DESIGN.md, §2.1 и BALANCE.md, §1.
CATCHUP_TICKS_PER_SECOND = 1.0

#: Потолок догона: 100 игровых лет за один запуск. Не для производительности —
#: батчинг справится, — а чтобы сломанные системные часы (скачок в 2038-й)
#: не превращали партию в мусор молча. Упор в потолок логируется.
MAX_CATCHUP_TICKS = 100 * 365 * 24


@dataclass(frozen=True, slots=True)
class Missed:
    """Сколько времени прошло без игрока и сколько из него потерялось.

    ``dropped`` существует затем, что упор в ``MAX_CATCHUP_TICKS`` обязан быть
    виден. Молча начислить сто лет вместо тысячи — значит подменить партию и не
    сказать: игрок решит, что игра сломалась, хотя сломались системные часы.
    """

    ticks: int
    dropped: int = 0

    @property
    def capped(self) -> bool:
        return self.dropped > 0


class Clock:
    """Обёртка над временными полями ``GameState``.

    Собственного состояния не имеет: всё живёт в ``GameState`` и попадает в
    сейв. Это осознанно — часы, у которых есть незасериализованное поле,
    рано или поздно дадут расхождение после перезапуска.
    """

    __slots__ = ("state",)

    def __init__(self, state: GameState) -> None:
        self.state = state

    # -- чтение ----------------------------------------------------------

    @property
    def tick(self) -> int:
        return self.state.tick

    @property
    def speed(self) -> str:
        return self.state.speed

    @property
    def multiplier(self) -> float:
        return SPEED_MULTIPLIER[self.state.speed]

    @property
    def paused(self) -> bool:
        return self.state.speed == Speed.PAUSED

    def game_datetime(self) -> datetime:
        """Игровая дата. Всегда UTC: локальная зона зависит от машины и
        переводов часов, а игровая дата обязана быть функцией только сейва."""
        return datetime.fromtimestamp(self.state.epoch, tz=UTC) + timedelta(hours=self.state.tick)

    def format_datetime(self) -> str:
        """Строка для панели времени (DESIGN.md, §11)."""
        return self.game_datetime().strftime("%d.%m.%Y %H:00")

    # -- изменение -------------------------------------------------------

    def set_speed(self, speed: str) -> None:
        if speed not in SPEED_MULTIPLIER:
            raise ValueError(f"неизвестная скорость: {speed!r}")
        self.state.speed = speed

    def advance(self, ticks: int) -> None:
        if ticks < 0:
            raise ValueError("время назад не идёт")
        self.state.tick += ticks

    # -- догон -----------------------------------------------------------

    def missed(self, saved_at: float, now: float) -> Missed:
        """Сколько игровых часов начислить за отсутствие игрока.

        Пауза замораживает мир: если партию сохранили на паузе, при следующем
        запуске не начисляется ничего. Это единственный способ отойти надолго
        и вернуться в тот же мир, и он должен быть явным — поэтому завязан на
        скорость в сейве, а не на отдельный флаг.

        Часы машины, ушедшие назад (правка времени, переезд из другой зоны),
        дают отрицательную разницу — она трактуется как ноль, а не как откат
        симуляции.
        """
        if self.paused:
            return Missed(0)
        elapsed = now - saved_at
        if elapsed <= 0.0:
            return Missed(0)
        ticks = int(elapsed * CATCHUP_TICKS_PER_SECOND)
        if ticks > MAX_CATCHUP_TICKS:
            return Missed(MAX_CATCHUP_TICKS, dropped=ticks - MAX_CATCHUP_TICKS)
        return Missed(ticks)

    def __repr__(self) -> str:
        return f"Clock(tick={self.state.tick}, speed={self.state.speed!r})"
