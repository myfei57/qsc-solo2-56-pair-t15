"""门区联动。

门区联动按站台门区映射把逻辑门区解析成物理门区，再整组开门。解析始终走生效
指针，并把所用修订号钉进联动记录：同一逻辑键在同一修订下重复联动直接返回已
有记录（幂等，重试不会重复开门）；修订切换后的第一次联动按新映射解析并落新
记录，审计据此能把每次开门对应到具体的映射修订。
"""

from __future__ import annotations

from ..errors import MappingError
from ..platform import MappingRegistry
from .door import DoorController


def active_key(station_id: str) -> str:
    return f"door/{station_id}/active_group"


def open_group(
    controller: DoorController,
    mapping: MappingRegistry,
    station_id: str,
    group_key: str,
) -> dict:
    """按映射打开一个门区组；同键同修订的重复调用幂等返回。"""

    current = mapping.current(station_id)
    if current is None:
        raise MappingError(f"站台 {station_id} 没有门区映射", component="platform")
    resolved = mapping.resolve(station_id, group_key)
    existing = controller.store.read(active_key(station_id))
    if (
        isinstance(existing, dict)
        and existing.get("group_key") == group_key
        and existing.get("group") == resolved
        and int(existing.get("revision", 0)) == current.revision
    ):
        return {
            "station_id": station_id,
            "group_key": group_key,
            "group": resolved,
            "revision": current.revision,
            "opened": list(existing.get("opened", [])),
            "replayed": True,
        }
    gate_ids = [gate.id for gate in controller.gates.by_station(station_id) if gate.group == resolved]
    if not gate_ids:
        raise MappingError(
            f"映射 {group_key} → {resolved} 在站台 {station_id} 找不到对应门区",
            component="door",
        )
    locked = [gate_id for gate_id in gate_ids if controller.lockouts.is_locked(gate_id)]
    if locked:
        raise MappingError(f"门区 {resolved} 被联锁锁定: {','.join(locked)}", component="door")
    for gate_id in gate_ids:
        controller.gates.set_open(gate_id, True)
    controller.store.write(
        active_key(station_id),
        {
            "station_id": station_id,
            "group_key": group_key,
            "group": resolved,
            "revision": current.revision,
            "opened": gate_ids,
        },
    )
    return {
        "station_id": station_id,
        "group_key": group_key,
        "group": resolved,
        "revision": current.revision,
        "opened": gate_ids,
        "replayed": False,
    }


def active_group(controller: DoorController, station_id: str) -> str:
    """返回站台当前联动的门区；没有联动时返回空串。"""

    raw = controller.store.read(active_key(station_id))
    if isinstance(raw, dict):
        return str(raw.get("group", ""))
    return ""


def active_group_revision(controller: DoorController, station_id: str) -> int:
    """返回当前联动记录所用的映射修订号；没有联动时返回 0。"""

    raw = controller.store.read(active_key(station_id))
    if isinstance(raw, dict):
        return int(raw.get("revision", 0))
    return 0
