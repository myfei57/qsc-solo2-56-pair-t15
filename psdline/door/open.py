"""开门动作。

开门是"对位落盘之后"的第一个动作：读不到对位记录、或对位记录没落盘，都直接
拒绝。开门事实写成 ``door/<train>/open`` 文档，发车前由开门状态推导。文档带
站台作业序号，重复的到站/开门请求命中同一作业时直接返回既有事实，不会把同一
轮开门重复计入或重复驱动门区。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import InterlockError
from .door import DoorController


@dataclass(frozen=True)
class OpenRecord:
    """一次作业开门事实。"""

    train_id: str
    station_id: str
    position_m: int
    service_seq: int
    at: float

    def as_dict(self) -> dict:
        return {
            "train_id": self.train_id,
            "station_id": self.station_id,
            "position_m": self.position_m,
            "service_seq": self.service_seq,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "OpenRecord":
        return cls(
            train_id=str(payload.get("train_id", "")),
            station_id=str(payload.get("station_id", "")),
            position_m=int(payload.get("position_m", 0)),
            service_seq=int(payload.get("service_seq", 0)),
            at=float(payload.get("at", 0.0)),
        )


def open_doc_id(train_id: str) -> str:
    return f"door/{train_id}/open"


def _departed(controller: DoorController, train_id: str) -> bool:
    return controller.store.exists(f"door/{train_id}/departed")


def open_doors(
    controller: DoorController,
    train_id: str,
    at: float,
    station_id: str = "",
    service_seq: int = 0,
) -> dict:
    """按对位记录打开一列车的站台门；同一作业内重复调用幂等。"""

    alignment = controller.align.get(train_id)
    if alignment is None:
        raise InterlockError("列车没有对位记录", component="door")
    if not alignment.durable:
        raise InterlockError("列车对位尚未落盘", component="door")

    raw = controller.store.read(open_doc_id(train_id))
    if isinstance(raw, dict):
        existing = OpenRecord.from_dict(raw)
        if existing.service_seq == service_seq and service_seq > 0:
            return {
                "ok": True,
                "train_id": train_id,
                "station_id": existing.station_id,
                "position_m": existing.position_m,
                "service_seq": existing.service_seq,
                "idempotent": True,
                "violations": [],
            }
        if not _departed(controller, train_id):
            raise InterlockError("列车上一轮站台门服务尚未结束", component="door")
        controller.store.remove(f"door/{train_id}/departed")

    record = OpenRecord(
        train_id=train_id,
        station_id=station_id,
        position_m=alignment.position_m,
        service_seq=service_seq,
        at=at,
    )
    controller.store.write(open_doc_id(train_id), record.as_dict())
    controller.note_open()
    return {
        "ok": True,
        "train_id": train_id,
        "station_id": station_id,
        "position_m": alignment.position_m,
        "service_seq": service_seq,
        "idempotent": False,
        "violations": [],
    }


def opened(controller: DoorController, train_id: str) -> bool:
    """判断一列车的站台门是否已打开。"""

    return controller.store.exists(open_doc_id(train_id))
