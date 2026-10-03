"""门区联动。

门区联动按站台门区映射把逻辑门区解析成物理门区，再整组开门。映射必须存在且
当前内容哈希完好；联动记录同时保存映射修订和幂等命令号。映射回滚不会反算已
开过的门，也不会自动补开，只会影响之后的新开门请求。
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
    command_id: str = "",
) -> dict:
    """按映射打开一个门区组；相同命令号重复提交时不重复驱动门区。"""

    active_mapping = mapping.current(station_id)
    if active_mapping is None:
        raise MappingError(f"站台 {station_id} 没有门区映射", component="door")
    resolved = mapping.resolve(station_id, group_key)
    raw = controller.store.read(active_key(station_id))
    if command_id and isinstance(raw, dict) and raw.get("command_id") == command_id:
        gate_ids = [
            gate.id
            for gate in controller.gates.by_station(station_id)
            if gate.group == str(raw.get("group", ""))
        ]
        return {
            "station_id": station_id,
            "group_key": str(raw.get("group_key", group_key)),
            "group": str(raw.get("group", "")),
            "revision": int(raw.get("revision", 0)),
            "opened": gate_ids,
            "idempotent": True,
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
            "revision": active_mapping.revision,
            "content_hash": active_mapping.content_hash,
            "command_id": command_id,
        },
    )
    return {
        "station_id": station_id,
        "group_key": group_key,
        "group": resolved,
        "revision": active_mapping.revision,
        "content_hash": active_mapping.content_hash,
        "opened": gate_ids,
        "idempotent": False,
    }


def active_group(controller: DoorController, station_id: str) -> str:
    """返回站台当前联动的门区；没有联动时返回空串。"""

    raw = controller.store.read(active_key(station_id))
    if isinstance(raw, dict):
        return str(raw.get("group", ""))
    return ""
