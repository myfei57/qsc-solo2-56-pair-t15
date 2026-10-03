"""站台作业间隙登记。

映射生效不是看墙上时钟，而是看一个完整作业单元的边界。每个站台记录
``clear / occupied`` 和单调递增的 ``epoch``：列车进入作业时进入占用，确认
发车并清扫后才回到间隙，同时推进一个新的间隙代号。待生效修订记住批准时的
间隙代号，只允许在之后的新间隙切换，避免“批准时恰好无车”却立即切入正在
开始的下一轮作业。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import InterlockError
from ..store import JsonStore

CLEAR = "clear"
OCCUPIED = "occupied"


@dataclass(frozen=True)
class WorkGap:
    """一个站台当前的作业间隙状态。"""

    station_id: str
    state: str
    epoch: int
    service_seq: int
    train_id: str
    at: float

    def as_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "state": self.state,
            "epoch": self.epoch,
            "service_seq": self.service_seq,
            "train_id": self.train_id,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "WorkGap":
        return cls(
            station_id=str(payload.get("station_id", "")),
            state=str(payload.get("state", CLEAR)),
            epoch=int(payload.get("epoch", 0)),
            service_seq=int(payload.get("service_seq", 0)),
            train_id=str(payload.get("train_id", "")),
            at=float(payload.get("at", 0.0)),
        )


class WorkGapRegistry:
    """按站台保存作业占用状态。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._entries = 0

    @property
    def entries(self) -> int:
        return self._entries

    @staticmethod
    def _doc_id(station_id: str) -> str:
        return f"gap/{station_id}/work"

    def get(self, station_id: str) -> WorkGap:
        """读取状态；没有记录时初始化为第 0 个间隙。"""

        raw = self._store.read(self._doc_id(station_id))
        if isinstance(raw, dict):
            return WorkGap.from_dict(raw)
        return WorkGap(
            station_id=station_id,
            state=CLEAR,
            epoch=0,
            service_seq=0,
            train_id="",
            at=0.0,
        )

    def clear_epoch(self, station_id: str) -> int | None:
        """间隙安全时返回当前间隙代号；占用中返回 ``None``。"""

        gap = self.get(station_id)
        return gap.epoch if gap.state == CLEAR else None

    def mark_clear(self, station_id: str, at: float) -> WorkGap:
        """登记一次现场确认的安全间隙边界；占用中拒绝。"""

        current = self.get(station_id)
        if current.state != CLEAR:
            raise InterlockError(f"站台 {station_id} 正在作业", component="gap")
        gap = WorkGap(
            station_id=station_id,
            state=CLEAR,
            epoch=current.epoch + 1,
            service_seq=current.service_seq,
            train_id="",
            at=at,
        )
        self._store.write(self._doc_id(station_id), gap.as_dict())
        return gap

    def force_state(self, gap: WorkGap) -> None:
        """恢复到指定作业状态；仅用于同一关键区段内失败补偿。"""

        self._store.write(self._doc_id(gap.station_id), gap.as_dict())

    def active_service(self, station_id: str, train_id: str) -> int | None:
        """返回某列车在该站台的作业序号；不匹配时返回 ``None``。"""

        gap = self.get(station_id)
        if gap.state == OCCUPIED and gap.train_id == train_id:
            return gap.service_seq
        return None

    def enter(self, station_id: str, train_id: str, at: float) -> WorkGap:
        """进入一轮站台作业；同一列车重复进入是幂等的。"""

        current = self.get(station_id)
        if current.state == OCCUPIED:
            if current.train_id == train_id:
                return current
            raise InterlockError(
                f"站台 {station_id} 正在服务 {current.train_id}", component="gap"
            )
        gap = WorkGap(
            station_id=station_id,
            state=OCCUPIED,
            epoch=current.epoch + 1,
            service_seq=current.service_seq + 1,
            train_id=train_id,
            at=at,
        )
        self._store.write(self._doc_id(station_id), gap.as_dict())
        self._entries += 1
        return gap

    def exit(self, station_id: str, train_id: str, at: float) -> WorkGap:
        """结束一轮站台作业并进入下一个安全间隙。"""

        current = self.get(station_id)
        if current.state == CLEAR:
            return current
        if current.train_id != train_id:
            raise InterlockError(
                f"列车 {train_id} 不能结束站台 {station_id} 的另一轮作业", component="gap"
            )
        gap = WorkGap(
            station_id=station_id,
            state=CLEAR,
            epoch=current.epoch + 1,
            service_seq=current.service_seq,
            train_id="",
            at=at,
        )
        self._store.write(self._doc_id(station_id), gap.as_dict())
        return gap

    def status(self, station_id: str) -> dict:
        gap = self.get(station_id)
        return {
            "station_id": station_id,
            "state": gap.state,
            "clear": gap.state == CLEAR,
            "epoch": gap.epoch,
            "service_seq": gap.service_seq,
            "train_id": gap.train_id,
        }
