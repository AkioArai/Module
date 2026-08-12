"""Приказы: воздействия игрока, имеющие длительность (DESIGN.md, §6).

Инвариант И7: игрок никогда не ждёт внутри игры. Ремонт, перегрузка топлива,
найм — всё это не «нажал и смотри на полоску», а приказ, который исполняется в
игровом времени, пока игрок занимается другим.

Устройство простое и целиком опирается на планировщик: приказ — запись в
состоянии, его завершение — событие в очереди. Отсюда два бесплатных свойства:
приказ переживает выход из игры, и он корректно доигрывается в догоне.

Отмена приказа снимает событие завершения. Последствия отмены — дело конкретного
вида приказа: прерванная перегрузка топлива не возвращает потраченное.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from module_sim.core.events import registry
from module_sim.core.events.scheduler import ScheduledEvent

if TYPE_CHECKING:  # pragma: no cover - подсистемы не тянут sim во время работы
    from module_sim.core.sim import Simulation

__all__ = [
    "COMPLETE_KIND",
    "MAINTENANCE_HOURS",
    "REFUEL_HOURS",
    "Order",
    "cancel_order",
    "issue_order",
]

COMPLETE_KIND = "order.complete"

#: BALANCE.md, §2.5: перегрузка — 30 суток останова.
REFUEL_HOURS = 30 * 24
#: Плановое ТО. Число условное до фазы 5, где появится износ.
MAINTENANCE_HOURS = 5 * 24

STATUS_RUNNING = "выполняется"
STATUS_DONE = "выполнен"
STATUS_CANCELLED = "отменён"


@dataclass(slots=True)
class Order:
    """Приказ игрока. Живёт в состоянии партии, поэтому сериализуем."""

    id: int
    kind: str
    issued_tick: int
    ends_tick: int
    status: str = STATUS_RUNNING
    #: Номер события завершения в планировщике — чтобы было что отменять.
    event_order: int = -1
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "issued_tick": self.issued_tick,
            "ends_tick": self.ends_tick,
            "status": self.status,
            "event_order": self.event_order,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Order:
        return cls(
            id=data["id"],
            kind=data["kind"],
            issued_tick=data["issued_tick"],
            ends_tick=data["ends_tick"],
            status=data["status"],
            event_order=data.get("event_order", -1),
            payload=dict(data.get("payload", {})),
        )


#: Сколько часов занимает каждый вид приказа и требует ли он остановки блока.
DURATIONS: dict[str, int] = {
    "refuel": REFUEL_HOURS,
    "maintenance": MAINTENANCE_HOURS,
}
NEEDS_SHUTDOWN: frozenset[str] = frozenset({"refuel", "maintenance"})


def issue_order(simulation: Simulation, kind: str, payload: dict | None = None) -> Order:
    """Отдать приказ. Возвращает его немедленно — ждать игроку нечего."""
    if kind not in DURATIONS:
        raise ValueError(f"неизвестный вид приказа: {kind!r}")

    state = simulation.state
    order = Order(
        id=state.next_order_id,
        kind=kind,
        issued_tick=state.tick,
        ends_tick=state.tick + DURATIONS[kind],
        payload=dict(payload or {}),
    )
    state.next_order_id += 1

    if kind in NEEDS_SHUTDOWN:
        # Уставка сбрасывается сразу, но мощность падает не мгновенно: блок
        # разгружается с той же скоростью, с какой набирает (physics/reactor).
        state.reactor.power_setpoint = 0.0

    order.event_order = simulation.schedule(order.ends_tick, COMPLETE_KIND, {"order_id": order.id})
    state.orders.append(order.to_dict())
    return order


def find_order(simulation: Simulation, order_id: int) -> dict | None:
    for raw in simulation.state.orders:
        if raw["id"] == order_id:
            return raw
    return None


def cancel_order(simulation: Simulation, order_id: int) -> bool:
    """Отменить приказ. ``False`` — приказа нет или он уже не выполняется."""
    raw = find_order(simulation, order_id)
    if raw is None or raw["status"] != STATUS_RUNNING:
        return False
    simulation.scheduler.cancel(raw["event_order"])
    raw["status"] = STATUS_CANCELLED
    return True


def active_orders(simulation: Simulation) -> list[dict]:
    return [raw for raw in simulation.state.orders if raw["status"] == STATUS_RUNNING]


@registry.handler(COMPLETE_KIND)
def _on_complete(simulation: Simulation, event: ScheduledEvent) -> None:
    raw = find_order(simulation, event.payload["order_id"])
    if raw is None or raw["status"] != STATUS_RUNNING:
        # Приказ отменили, а событие всё же дошло — такое возможно только при
        # порче сейва, но падать из-за этого партии незачем.
        return

    if raw["kind"] == "refuel":
        simulation.state.reactor.burnup = 0.0

    raw["status"] = STATUS_DONE
