"""Симуляция: состояние + часы + RNG и два способа двигать время.

Фаза 0. Геймплея здесь нет — есть каркас и, главное, **оба пути продвижения
времени сразу**:

* ``run(n)`` — потиковый, эталонный;
* ``catch_up(n)`` — батчевый, которым считается пропущенное время.

Инвариант И4 (CLAUDE.md, DESIGN.md §Р2) требует, чтобы они давали бит-в-бит
одинаковый результат. Тест ``test_catchup_equivalence`` проверяет это уже
сейчас, на пустой симуляции. Так дешевле: правило заводится до того, как
появятся формулы, а не после того, как половина из них окажется
непроинтегрируемой замкнуто.

Каждая подсистема, добавляемая в фазах 1+, обязана расширить оба метода
согласованно — либо замкнутой формулой на интервал, либо дискретным событием в
планировщике.
"""

from __future__ import annotations

from module_sim.core.clock import Clock
from module_sim.core.rng import Rng
from module_sim.core.state import GameState

__all__ = ["Simulation"]

#: Поток-заглушка фазы 0. Существует, чтобы догон и потиковый прогон было чем
#: различать в тестах: без единого обращения к RNG эквивалентность проверялась
#: бы вхолостую. Когда появятся настоящие подсистемы, поток исчезнет — и это
#: ничего не сломает: счётчик удалённого потока просто останется лежать в
#: старых сейвах, никого не сдвигая (core/rng.py, свойство 2).
HEARTBEAT_STREAM = "sim.heartbeat"


class Simulation:
    """Партия целиком. Ничего не знает об интерфейсе (инвариант И1)."""

    __slots__ = ("clock", "rng", "state")

    def __init__(self, state: GameState) -> None:
        self.state = state
        self.clock = Clock(state)
        self.rng = Rng(state.seed, state.rng_counters)

    @classmethod
    def new_game(cls, seed: int, epoch: float, company_name: str | None = None) -> Simulation:
        state = GameState(seed=seed, tick=0, epoch=epoch)
        if company_name:
            state.company.name = company_name
        return cls(state)

    # -- продвижение времени ---------------------------------------------

    def tick_once(self) -> None:
        """Один игровой час. Эталон, относительно которого проверяется батч."""
        self.rng.stream(HEARTBEAT_STREAM).random()
        self.clock.advance(1)

    def run(self, ticks: int) -> None:
        """Потиковый прогон. Используется, когда игрок смотрит на игру."""
        if ticks < 0:
            raise ValueError("время назад не идёт")
        for _ in range(ticks):
            self.tick_once()

    def catch_up(self, ticks: int) -> None:
        """Батчевый прогон пропущенного времени.

        Здесь и должна жить вся математика «замкнутой формулой на интервал».
        Пока подсистем нет, батч сводится к промотке счётчика за O(1) и сдвигу
        часов — но именно эта форма и требуется от будущих подсистем: не цикл
        по часам, а прыжок до ближайшего дискретного события.
        """
        if ticks < 0:
            raise ValueError("время назад не идёт")
        if ticks == 0:
            return
        self.rng.stream(HEARTBEAT_STREAM).skip(ticks)
        self.clock.advance(ticks)

    # -- сериализация ----------------------------------------------------

    def sync_state(self) -> GameState:
        """Собрать состояние к сохранению.

        Счётчики RNG живут в объектах потоков, а в ``GameState`` попадают
        только здесь — единственной точкой, чтобы нельзя было сохранить
        рассинхронизированную пару «состояние + счётчики»."""
        self.state.rng_counters = self.rng.counters()
        return self.state

    def __repr__(self) -> str:
        return f"Simulation(tick={self.state.tick}, seed={self.state.seed})"
