"""Приказы — единственный способ воздействия игрока на мир.

Инвариант И7 (CLAUDE.md): блокирующих ожиданий в игре нет. Ремонт, найм,
поставка топлива, раунд переговоров, движение стержней — всё это приказы с
длительностью в игровых часах, которые исполняются, пока игрок занят другим.

Фаза 0: зафиксированы тип и жизненный цикл. Исполнение, права доступа и
проверка предусловий — фазы 3+.

Важно для будущего батчинга (И4): ``finishes_at_tick`` — это и есть дискретное
событие, до которого догон прыгает одним блоком. Приказ без известного заранее
времени завершения ломает батчинг, поэтому длительность обязательна и
вычисляется при постановке, а не «по ходу».

В ``GameState`` приказы пока не входят. Когда войдут — это изменение структуры
сейва со всеми последствиями: новая миграция и новая фикстура
(SAVEFORMAT.md, §5).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Order", "OrderStatus"]


class OrderStatus:
    """Состояния приказа (DESIGN.md, §6). Строки — уйдут в сейв как есть."""

    CREATED = "created"
    ASSIGNED = "assigned"
    RUNNING = "running"
    DONE = "done"
    REJECTED = "rejected"  # нет прав или не выполнены предусловия
    CANCELLED = "cancelled"  # отменён игроком, частичный возврат
    FAILED = "failed"  # ошибка исполнителя (DESIGN.md, §5.4)

    TERMINAL = (DONE, REJECTED, CANCELLED, FAILED)


@dataclass(slots=True)
class Order:
    """Приказ. Часть детерминированного состояния, поэтому сериализуем."""

    order_id: int
    kind: str
    status: str = OrderStatus.CREATED
    #: Тик постановки и тик завершения. Второй известен заранее — см. модульный
    #: докстринг про батчинг.
    issued_at_tick: int = 0
    finishes_at_tick: int = 0
    #: Кто исполняет. None — ещё не назначен (некому по правам, §5.5).
    assignee_id: int | None = None
    #: Требуемая роль и уровень допуска — проверяются матрицей прав.
    required_role: str = ""
    required_clearance: int = 0
    cost_cents: int = 0
    params: dict | None = None

    @property
    def duration_ticks(self) -> int:
        return max(0, self.finishes_at_tick - self.issued_at_tick)

    @property
    def is_terminal(self) -> bool:
        return self.status in OrderStatus.TERMINAL

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "kind": self.kind,
            "status": self.status,
            "issued_at_tick": self.issued_at_tick,
            "finishes_at_tick": self.finishes_at_tick,
            "assignee_id": self.assignee_id,
            "required_role": self.required_role,
            "required_clearance": self.required_clearance,
            "cost_cents": self.cost_cents,
            "params": dict(self.params) if self.params else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Order:
        return cls(**data)
