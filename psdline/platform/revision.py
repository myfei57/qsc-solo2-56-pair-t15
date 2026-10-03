"""门区映射修订的生命周期。

一次改图不再"改完就生效"，而是走一条可撤回的状态机：

``proposed``  提议已落盘（内容从此不可变，任何修改都要另起新修订号）；
``approved``  第二人确认完成（确认人不能是提议人），开始等待安全作业间隙；
``activating`` 生效写入进行中（掉电恢复时据此判断生效是否越过了提交点）；
``active``    已生效，门控按此修订解析门区；
``withdrawn`` 生效前被撤回，旧版本继续可用，门控完全无感；
``superseded`` 被更新的修订顶替（含基准失效被安全网拦下的情况）。

修订内容（``groups``）落盘后不再改动，状态迁移逐条写进审计流水，两者合起来
构成不可变历史：任何时刻都能回答"当时按哪份图开的门、谁提的、谁确认的"。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

PROPOSED = "proposed"
APPROVED = "approved"
ACTIVATING = "activating"
ACTIVE = "active"
WITHDRAWN = "withdrawn"
SUPERSEDED = "superseded"

IN_FLIGHT = (PROPOSED, APPROVED, ACTIVATING)
TERMINAL = (ACTIVE, WITHDRAWN, SUPERSEDED)

KIND_RENUMBER = "renumber"
KIND_ROLLBACK = "rollback"
KIND_INSTALL = "install"

SCHEMA = 1


@dataclass(frozen=True)
class MappingRevision:
    """一份门区映射修订；内容不可变，只有状态字段随生命周期推进。"""

    station_id: str
    revision: int
    groups: dict[str, str]
    base_revision: int
    kind: str = KIND_RENUMBER
    state: str = PROPOSED
    proposer: str = ""
    confirmer: str = ""
    withdrawn_by: str = ""
    proposed_at: float = 0.0
    confirmed_at: float = 0.0
    activated_at: float = 0.0
    withdrawn_at: float = 0.0
    not_before: float = 0.0
    activation_id: str = ""
    layout_revision: int = 0
    target_revision: int = 0
    schema: int = SCHEMA

    def transition(self, **changes: object) -> "MappingRevision":
        """返回状态推进后的新实例（原实例不变，保持不可变语义）。"""

        return replace(self, **changes)

    def as_dict(self) -> dict:
        return {
            "schema": self.schema,
            "station_id": self.station_id,
            "revision": self.revision,
            "groups": dict(sorted(self.groups.items())),
            "base_revision": self.base_revision,
            "kind": self.kind,
            "state": self.state,
            "proposer": self.proposer,
            "confirmer": self.confirmer,
            "withdrawn_by": self.withdrawn_by,
            "proposed_at": self.proposed_at,
            "confirmed_at": self.confirmed_at,
            "activated_at": self.activated_at,
            "withdrawn_at": self.withdrawn_at,
            "not_before": self.not_before,
            "activation_id": self.activation_id,
            "layout_revision": self.layout_revision,
            "target_revision": self.target_revision,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "MappingRevision":
        groups = payload.get("groups", {})
        clean = (
            {str(key): str(value) for key, value in groups.items()}
            if isinstance(groups, dict)
            else {}
        )
        return cls(
            station_id=str(payload.get("station_id", "")),
            revision=int(payload.get("revision", 0)),
            groups=clean,
            base_revision=int(payload.get("base_revision", 0)),
            kind=str(payload.get("kind", KIND_RENUMBER)),
            state=str(payload.get("state", PROPOSED)),
            proposer=str(payload.get("proposer", "")),
            confirmer=str(payload.get("confirmer", "")),
            withdrawn_by=str(payload.get("withdrawn_by", "")),
            proposed_at=float(payload.get("proposed_at", 0.0)),
            confirmed_at=float(payload.get("confirmed_at", 0.0)),
            activated_at=float(payload.get("activated_at", 0.0)),
            withdrawn_at=float(payload.get("withdrawn_at", 0.0)),
            not_before=float(payload.get("not_before", 0.0)),
            activation_id=str(payload.get("activation_id", "")),
            layout_revision=int(payload.get("layout_revision", 0)),
            target_revision=int(payload.get("target_revision", 0)),
            schema=int(payload.get("schema", SCHEMA)),
        )
