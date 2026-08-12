"""Симуляция: состояние, часы, RNG, очередь событий и продвижение времени.

Продвижение времени устроено одним способом (``advance``) и предъявляется двумя:

* ``run(n)`` — потиковый, эталонный. Им идёт время, пока игрок смотрит на игру;
* ``catch_up(n)`` — батчевый, которым считается пропущенное время.

Оба ходят через один и тот же ``advance``, и разница между ними ровно одна:
величина шага. Это не мелочь, а способ выполнить контракт И4а: два разных
интегратора неизбежно разъехались бы, один интегратор с разной нарезкой — нет.

Устройство ``advance`` (DESIGN.md, §Р2):

1. Спросить у планировщика тик ближайшего события.
2. Проинтегрировать состояние одним куском до этого момента (или до цели, если
   она ближе) — здесь будут замкнутые формулы подсистем фаз 2+.
3. Применить событие. Обработчик имеет право поставить новые события, в том
   числе на этот же тик.
4. Повторять, пока не дошли до цели.

Идле-простой в год считается за единицы миллисекунд не потому, что цикл
быстрый, а потому что итераций в нём столько, сколько событий, — десятки вместо
8760.
"""

from __future__ import annotations

from module_sim.core.clock import Clock
from module_sim.core.events import registry
from module_sim.core.events.scheduler import ScheduledEvent, Scheduler
from module_sim.core.physics import reactor
from module_sim.core.rng import Rng
from module_sim.core.state import GameState

#: Периодические события, без которых партия не живёт: вид и период в часах.
#: Продублировано из подсистем строками, чтобы ``sim`` от них не зависел —
#: подсистемы знают о ``sim``, обратная зависимость сделала бы цикл.
PERIODIC_EVENTS: tuple[tuple[str, int], ...] = (
    ("market.settlement", 24),  # расчёт за энергию, economy/market.py
    ("finance.month", 30 * 24),  # проценты, расходы, ковенанты, economy/finance.py
)

__all__ = ["MAX_EVENT_CASCADE", "Simulation"]

#: Поток-заглушка фазы 0. Существует, чтобы догон и потиковый прогон было чем
#: различать в тестах: без единого обращения к RNG эквивалентность проверялась
#: бы вхолостую. Когда появятся настоящие подсистемы, поток исчезнет — и это
#: ничего не сломает: счётчик удалённого потока просто останется лежать в
#: старых сейвах, никого не сдвигая (core/rng.py, свойство 2).
HEARTBEAT_STREAM = "sim.heartbeat"

#: Предохранитель от каскада: обработчик события ставит новое событие на тот же
#: тик, тот — следующее, и симуляция зависает, не сдвинув часы ни на час.
#: Ронять партию неприятно, но зависший наглухо процесс хуже: игрок не поймёт,
#: что случилось, и потеряет несохранённое (BALANCE.md, §1).
MAX_EVENT_CASCADE = 10_000


class Simulation:
    """Партия целиком. Ничего не знает об интерфейсе (инвариант И1)."""

    __slots__ = ("clock", "rng", "scheduler", "state", "unknown_events")

    def __init__(self, state: GameState) -> None:
        self.state = state
        self.clock = Clock(state)
        self.rng = Rng(state.seed, state.rng_counters)
        self.scheduler = Scheduler.from_dict(state.scheduler)
        #: Виды событий из сейва, которых эта версия игры не знает. Не часть
        #: состояния: это факт про текущую загрузку, о котором надо сказать
        #: игроку и забыть (core/events/registry.py).
        self.unknown_events: list[str] = []
        self._ensure_bootstrap_events()

    @classmethod
    def new_game(cls, seed: int, epoch: float, company_name: str | None = None) -> Simulation:
        state = GameState(seed=seed, tick=0, epoch=epoch)
        if company_name:
            state.company.name = company_name
        return cls(state)

    def _ensure_bootstrap_events(self) -> None:
        """Завести события, без которых партия не живёт.

        Нужно не только новой партии: сейв, поднятый миграцией с версии, где
        подсистемы ещё не было, тоже приходит без её событий. Проверка идёт по
        очереди, а не по флагу в состоянии, — флаг пришлось бы мигрировать и
        держать в согласии с реальностью.
        """
        pending = {event.kind for event in self.scheduler.pending()}
        for kind, interval in PERIODIC_EVENTS:
            if kind in pending:
                continue
            next_tick = (self.state.tick // interval + 1) * interval
            self.scheduler.schedule(next_tick, kind, not_before=self.state.tick)

    # -- планирование ----------------------------------------------------

    def schedule(self, tick: int, kind: str, payload: dict | None = None) -> int:
        """Поставить событие на абсолютный тик.

        Текущий тик передаётся планировщику, чтобы событие в прошлом падало
        сразу: оно никогда не сработает, и почти всегда это ошибка в формуле
        интервала, а не намерение.
        """
        return self.scheduler.schedule(tick, kind, payload, not_before=self.state.tick)

    def schedule_in(self, hours: int, kind: str, payload: dict | None = None) -> int:
        """То же, но с отсчётом от текущего момента."""
        return self.scheduler.schedule(
            self.state.tick + hours, kind, payload, not_before=self.state.tick
        )

    # -- продвижение времени ---------------------------------------------

    def _integrate(self, hours: int) -> None:
        """Проэволюционировать состояние на ``hours`` часов одним куском.

        Единственное место, где непрерывные величины меняются. Подсистемы фаз
        2+ добавляют сюда свои замкнутые формулы; всё, что нельзя
        проинтегрировать замкнуто, обязано стать событием в планировщике (И4).
        """
        if hours <= 0:
            return
        self.rng.stream(HEARTBEAT_STREAM).skip(hours)
        # Реактор — первая непрерывная подсистема. Выработка копится дробной:
        # в деньги она превращается только в событии расчёта, иначе округление
        # происходило бы в разных точках у батчевого и потикового пути и касса
        # разошлась бы (economy/market.py).
        self.state.market.energy_sold_mwh += reactor.advance(self.state.reactor, hours)
        self.clock.advance(hours)

    def _fire(self, event: ScheduledEvent) -> None:
        if not registry.dispatch(self, event):
            self.unknown_events.append(event.kind)

    def advance(self, ticks: int) -> None:
        """Продвинуть время на ``ticks`` часов, применив все события по пути."""
        if ticks < 0:
            raise ValueError("время назад не идёт")
        target = self.state.tick + ticks

        cascade = 0
        while True:
            event = self.scheduler.pop_due(self.state.tick)
            if event is not None:
                self._fire(event)
                cascade += 1
                if cascade > MAX_EVENT_CASCADE:
                    raise RuntimeError(
                        f"каскад событий на тике {self.state.tick}: "
                        f"больше {MAX_EVENT_CASCADE} подряд без сдвига часов"
                    )
                continue

            cascade = 0
            if self.state.tick >= target:
                return

            next_tick = self.scheduler.peek()
            stop = target if next_tick is None else min(target, next_tick)
            self._integrate(stop - self.state.tick)

    def run(self, ticks: int) -> None:
        """Потиковый прогон. Эталон, относительно которого проверяется батч."""
        if ticks < 0:
            raise ValueError("время назад не идёт")
        for _ in range(ticks):
            self.advance(1)

    def catch_up(self, ticks: int) -> None:
        """Батчевый прогон пропущенного времени (DESIGN.md, §Р2)."""
        self.advance(ticks)

    # -- сериализация ----------------------------------------------------

    def sync_state(self) -> GameState:
        """Собрать состояние к сохранению.

        Счётчики RNG и очередь событий живут в своих объектах, а в ``GameState``
        попадают только здесь — единственной точкой, чтобы нельзя было
        сохранить рассинхронизированную пару «состояние + счётчики»."""
        self.state.rng_counters = self.rng.counters()
        self.state.scheduler = self.scheduler.to_dict()
        return self.state

    def __repr__(self) -> str:
        return f"Simulation(tick={self.state.tick}, seed={self.state.seed})"


# Импорт подсистем в самом низу — только ради регистрации обработчиков событий
# (core/events/registry.py). Наверх его не поднять: подсистемы ссылаются на
# ``Simulation``, и наверху это был бы цикл. Отсюда же следует, что подсистема,
# не упомянутая здесь, просто не будет знать своих событий, и сейв с ними
# загрузится с предупреждением — молча ломаться нечему.
from module_sim.core import orders as _orders  # noqa: E402,F401
from module_sim.core.economy import finance as _finance  # noqa: E402,F401
from module_sim.core.economy import market as _market  # noqa: E402,F401
