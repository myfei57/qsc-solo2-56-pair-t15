"""门区映射读写与修订流程。

门控只读"生效指针"文档 ``platform/<station>``（``current`` / ``resolve``），
改图不再直接覆盖指针，而是先落一份不可变的修订文档
``platform/<station>/rev/<nnnnn>``，走「提议 → 第二人确认 → 等安全作业间隙
生效」的流程；生效前任意时刻可撤回，指针不动，旧版本继续可用。

生效写入按「先意图、后提交、再收尾」的顺序落盘：先把修订置为 ``activating``
并带上本次生效标识，再原子替换指针文档（提交点），最后收尾修订状态。中途掉电
时由 :meth:`MappingRegistry.recover` 按生效标识核对指针，要么补齐收尾、要么
把修订退回 ``approved`` 等待重试；运行期写失败则当场补偿回滚，保证指针、修订
与审计三者不会出现"说不清"的中间态。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from ..clock import Clock, SystemClock
from ..errors import MappingError
from ..ids import IdFactory
from ..store import JsonStore
from .revision import (
    ACTIVATING,
    APPROVED,
    IN_FLIGHT,
    KIND_INSTALL,
    KIND_RENUMBER,
    KIND_ROLLBACK,
    ACTIVE,
    PROPOSED,
    SUPERSEDED,
    WITHDRAWN,
    MappingRevision,
)

#: 布局探针：给定站台返回 ``(布局修订号, 物理门区标识列表)``，没有布局时返回 ``None``。
LayoutProbe = Callable[[str], "tuple[int, list[str]] | None"]

#: 指针文档之外的保留文档名（不计入站台列表）。
_RESERVED_POINTER_DOCS = {"seed", "state"}


@dataclass(frozen=True)
class Mapping:
    """一个站台的现行门区映射（生效指针的内容）。"""

    station_id: str
    groups: dict[str, str]
    revision: int
    activated_from: int = 0
    activation_id: str = ""
    activated_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "groups": dict(sorted(self.groups.items())),
            "revision": self.revision,
            "activated_from": self.activated_from,
            "activation_id": self.activation_id,
            "activated_at": self.activated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Mapping":
        groups = payload.get("groups", {})
        clean = (
            {str(key): str(value) for key, value in groups.items()}
            if isinstance(groups, dict)
            else {}
        )
        return cls(
            station_id=str(payload.get("station_id", "")),
            groups=clean,
            revision=int(payload.get("revision", 1)),
            activated_from=int(payload.get("activated_from", 0)),
            activation_id=str(payload.get("activation_id", "")),
            activated_at=float(payload.get("activated_at", 0.0)),
        )


@dataclass
class RevisionLog:
    """映射修订历史（由落盘的修订文档推导，可重启重建）。"""

    station_id: str
    revisions: list[int] = field(default_factory=list)

    def push(self, revision: int) -> None:
        self.revisions.append(revision)

    def latest(self) -> int:
        return self.revisions[-1] if self.revisions else 0

    def as_dict(self) -> dict:
        return {"station_id": self.station_id, "revisions": list(self.revisions)}


class MappingRegistry:
    """门区映射登记表：生效指针 + 不可变修订文档。"""

    def __init__(
        self,
        store: JsonStore,
        clock: Clock | None = None,
        layout_probe: LayoutProbe | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self._layout_probe = layout_probe
        self._lock = threading.RLock()
        self._ids = IdFactory("maprev")
        self._rewrites = 0

    @property
    def rewrites(self) -> int:
        """返回累计生效（含初始安装）次数。"""

        return self._rewrites

    # ---- 文档定位 -----------------------------------------------------

    @staticmethod
    def _doc_id(station_id: str) -> str:
        return f"platform/{station_id}"

    @staticmethod
    def _rev_doc_id(station_id: str, revision: int) -> str:
        return f"platform/{station_id}/rev/{revision:05d}"

    # ---- 生效指针（门控唯一读取入口） ---------------------------------

    def current(self, station_id: str) -> Mapping | None:
        """读取当前生效映射，不存在时返回 ``None``。"""

        raw = self._store.read(self._doc_id(station_id))
        if not isinstance(raw, dict):
            return None
        return Mapping.from_dict(raw)

    def revision(self, station_id: str) -> int:
        """返回当前生效修订号；没有映射时返回 0。"""

        mapping = self.current(station_id)
        return mapping.revision if mapping else 0

    def resolve(self, station_id: str, group_key: str) -> str:
        """把逻辑门区解析成物理门区（始终按生效指针）。"""

        mapping = self.current(station_id)
        if mapping is None:
            raise MappingError(f"站台 {station_id} 没有门区映射", component="platform")
        group = mapping.groups.get(group_key)
        if group is None:
            raise MappingError(
                f"站台 {station_id} 的门区映射没有逻辑键 {group_key}", component="platform"
            )
        return group

    def stations(self) -> list[str]:
        """返回已有映射的站台标识（排序，不含保留文档）。"""

        found: list[str] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("platform/") or doc_id.count("/") != 1:
                continue
            name = doc_id.split("/", 1)[1]
            if name in _RESERVED_POINTER_DOCS:
                continue
            found.append(name)
        return sorted(found)

    # ---- 修订文档 -----------------------------------------------------

    def _write_rev(self, revision: MappingRevision) -> MappingRevision:
        self._store.write(self._rev_doc_id(revision.station_id, revision.revision), revision.as_dict())
        return revision

    def revision_doc(self, station_id: str, revision: int) -> MappingRevision | None:
        """读取一份修订文档，不存在时返回 ``None``。"""

        raw = self._store.read(self._rev_doc_id(station_id, revision))
        if not isinstance(raw, dict):
            return None
        return MappingRevision.from_dict(raw)

    def revisions(self, station_id: str) -> list[MappingRevision]:
        """按修订号升序列出某站台的全部修订文档。"""

        prefix = f"platform/{station_id}/rev/"
        found: list[MappingRevision] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith(prefix):
                continue
            raw = self._store.read(doc_id)
            if isinstance(raw, dict):
                found.append(MappingRevision.from_dict(raw))
        return sorted(found, key=lambda item: item.revision)

    def pending(self, station_id: str) -> MappingRevision | None:
        """返回尚在流程中的修订（提议／已确认／生效中），没有时返回 ``None``。"""

        for revision in self.revisions(station_id):
            if revision.state in IN_FLIGHT:
                return revision
        return None

    def activation_due(self, station_id: str, at: float) -> bool:
        """判断某站台是否有"已确认且已到生效时刻"的修订。"""

        revision = self.pending(station_id)
        return bool(
            revision is not None
            and revision.state == APPROVED
            and revision.not_before <= at
        )

    def history(self, station_id: str) -> RevisionLog:
        """返回某站台的修订历史（从落盘修订文档重建）。"""

        log = RevisionLog(station_id=station_id)
        for revision in self.revisions(station_id):
            log.push(revision.revision)
        if not log.revisions:
            mapping = self.current(station_id)
            if mapping is not None:
                log.push(mapping.revision)
        return log

    # ---- 生命周期：安装 ------------------------------------------------

    def install(self, station_id: str, groups: dict[str, str], actor: str = "seed") -> Mapping:
        """初始安装（交付布置用）：直接落第 1 号修订并置为生效。"""

        with self._lock:
            if not station_id:
                raise MappingError("映射的站台标识为空", component="platform")
            if self.current(station_id) is not None:
                raise MappingError(f"站台 {station_id} 已有门区映射，不能重复安装", component="platform")
            clean = self._validate_groups(station_id, groups)
            layout_revision = self._layout_revision(station_id)
            now = self._clock.now()
            activation_id = self._ids.next()
            revision = MappingRevision(
                station_id=station_id,
                revision=1,
                groups=clean,
                base_revision=0,
                kind=KIND_INSTALL,
                state=ACTIVE,
                proposer=actor,
                confirmer=actor,
                proposed_at=now,
                confirmed_at=now,
                activated_at=now,
                activation_id=activation_id,
                layout_revision=layout_revision,
            )
            self._write_rev(revision)
            mapping = Mapping(
                station_id=station_id,
                groups=clean,
                revision=1,
                activated_from=0,
                activation_id=activation_id,
                activated_at=now,
            )
            self._store.write(self._doc_id(station_id), mapping.as_dict())
            self._rewrites += 1
            return mapping

    # ---- 生命周期：提议 → 确认 → 撤回／生效 -----------------------------

    def propose(
        self,
        station_id: str,
        groups: dict[str, str],
        actor: str,
        not_before: float = 0.0,
        kind: str = KIND_RENUMBER,
        target_revision: int = 0,
    ) -> MappingRevision:
        """提议一次改图：内容落盘为不可变修订，等待第二人确认。"""

        with self._lock:
            if not actor:
                raise MappingError("改图必须登记提议人", component="platform")
            clean = self._validate_groups(station_id, groups)
            current = self.current(station_id)
            if current is None:
                raise MappingError(f"站台 {station_id} 没有门区映射，无法提议改图", component="platform")
            if clean == current.groups:
                raise MappingError("提议的映射与生效版本一致，无需改图", component="platform")
            pending = self.pending(station_id)
            if pending is not None:
                raise MappingError(
                    f"站台 {station_id} 已有待生效的修订 {pending.revision}"
                    "（状态 {state}），请先撤回或等待生效".format(state=pending.state),
                    component="platform",
                )
            revision = MappingRevision(
                station_id=station_id,
                revision=self._next_revision(station_id, current.revision),
                groups=clean,
                base_revision=current.revision,
                kind=kind,
                state=PROPOSED,
                proposer=actor,
                proposed_at=self._clock.now(),
                not_before=float(not_before),
                layout_revision=self._layout_revision(station_id),
                target_revision=target_revision,
            )
            return self._write_rev(revision)

    def confirm(self, station_id: str, revision: int, actor: str) -> MappingRevision:
        """第二人确认：确认人不能是提议人，确认后等待安全间隙生效。"""

        with self._lock:
            if not actor:
                raise MappingError("确认必须登记确认人", component="platform")
            doc = self._require_rev(station_id, revision)
            if doc.state != PROPOSED:
                raise MappingError(
                    f"修订 {revision} 当前状态为 {doc.state}，不能确认", component="platform"
                )
            if actor == doc.proposer:
                raise MappingError(
                    f"确认人 {actor} 与提议人相同，改图必须两人确认", component="platform"
                )
            return self._write_rev(
                doc.transition(state=APPROVED, confirmer=actor, confirmed_at=self._clock.now())
            )

    def withdraw(self, station_id: str, revision: int, actor: str) -> MappingRevision:
        """生效前撤回修订；生效指针不动，旧版本继续可用。"""

        with self._lock:
            if not actor:
                raise MappingError("撤回必须登记操作人", component="platform")
            doc = self._require_rev(station_id, revision)
            if doc.state not in (PROPOSED, APPROVED):
                raise MappingError(
                    f"修订 {revision} 当前状态为 {doc.state}，只有生效前才能撤回",
                    component="platform",
                )
            return self._write_rev(
                doc.transition(state=WITHDRAWN, withdrawn_by=actor, withdrawn_at=self._clock.now())
            )

    def propose_rollback(self, station_id: str, target_revision: int, actor: str) -> MappingRevision:
        """以历史修订的内容发起一次回滚提议（走同一套确认与生效流程）。"""

        with self._lock:
            target = self.revision_doc(station_id, target_revision)
            if target is None:
                raise MappingError(
                    f"站台 {station_id} 没有修订 {target_revision}，无法回滚", component="platform"
                )
            if target_revision == self.revision(station_id):
                raise MappingError(
                    f"修订 {target_revision} 正在生效，无需回滚", component="platform"
                )
            return self.propose(
                station_id,
                dict(target.groups),
                actor,
                kind=KIND_ROLLBACK,
                target_revision=target_revision,
            )

    # ---- 生命周期：生效 -------------------------------------------------

    def activate(self, station_id: str) -> tuple[MappingRevision, Mapping]:
        """把已确认的修订生效：返回 ``(新修订, 原指针内容)``。

        调用方（控制循环或手动触发）必须先确认安全作业间隙；这里负责并发
        安全网（基准修订核对）与部分失败回滚。
        """

        with self._lock:
            doc = self.pending(station_id)
            if doc is None or doc.state != APPROVED:
                raise MappingError(f"站台 {station_id} 没有已确认待生效的修订", component="platform")
            pointer = self.current(station_id)
            if pointer is None:
                raise MappingError(f"站台 {station_id} 没有生效中的映射", component="platform")
            if pointer.revision != doc.base_revision:
                # 确认之后基准被别人推进了：本次生效作废，内容不变，需重新提议。
                self._write_rev(doc.transition(state=SUPERSEDED))
                raise MappingError(
                    f"修订 {doc.revision} 的基准已失效（当前 {pointer.revision}，"
                    f"基于 {doc.base_revision}），请按最新映射重新提议",
                    component="platform",
                )
            activation_id = self._ids.next()
            now = self._clock.now()
            activating = self._write_rev(
                doc.transition(state=ACTIVATING, activation_id=activation_id)
            )
            try:
                self._store.write(
                    self._doc_id(station_id),
                    Mapping(
                        station_id=station_id,
                        groups=dict(doc.groups),
                        revision=doc.revision,
                        activated_from=pointer.revision,
                        activation_id=activation_id,
                        activated_at=now,
                    ).as_dict(),
                )
                activated = self._write_rev(
                    activating.transition(state=ACTIVE, activated_at=now)
                )
                old = self.revision_doc(station_id, pointer.revision)
                if old is not None and old.state == ACTIVE:
                    self._write_rev(old.transition(state=SUPERSEDED))
            except Exception:
                self._compensate(station_id, pointer, activating)
                raise
            self._rewrites += 1
            return activated, pointer

    def _compensate(self, station_id: str, pointer: Mapping, doc: MappingRevision) -> None:
        """生效中途失败：指针恢复旧内容，修订退回已确认，等待重试或核查。"""

        try:
            self._store.write(self._doc_id(station_id), pointer.as_dict())
            self._write_rev(doc.transition(state=APPROVED, activation_id=""))
        except Exception:
            # 补偿也失败时不再掩盖原始异常；重启后 recover() 按生效标识核对。
            pass

    # ---- 掉电恢复 -------------------------------------------------------

    def recover(self) -> list[dict]:
        """重启后核对生效写入：补齐已提交的收尾，退回未提交的意图。"""

        repairs: list[dict] = []
        with self._lock:
            for station_id in self._stations_with_revisions():
                raw = self._store.read(self._doc_id(station_id))
                pointer = Mapping.from_dict(raw) if isinstance(raw, dict) else None
                for doc in self.revisions(station_id):
                    if doc.state != ACTIVATING:
                        continue
                    committed = bool(
                        pointer is not None
                        and pointer.activation_id
                        and pointer.activation_id == doc.activation_id
                        and pointer.revision == doc.revision
                    )
                    if committed:
                        self._write_rev(doc.transition(state=ACTIVE, activated_at=self._clock.now()))
                        repairs.append(
                            {"station_id": station_id, "revision": doc.revision, "action": "finalize"}
                        )
                    else:
                        self._write_rev(doc.transition(state=APPROVED, activation_id=""))
                        repairs.append(
                            {
                                "station_id": station_id,
                                "revision": doc.revision,
                                "action": "revert_to_approved",
                            }
                        )
                if pointer is not None:
                    repairs.extend(self._normalize_active(station_id, pointer))
        return repairs

    def _normalize_active(self, station_id: str, pointer: Mapping) -> list[dict]:
        """归一修订状态：只有指针对应的修订允许处于 ``active``。"""

        repairs: list[dict] = []
        for doc in self.revisions(station_id):
            if doc.revision == pointer.revision and doc.state != ACTIVE:
                self._write_rev(doc.transition(state=ACTIVE))
                repairs.append(
                    {"station_id": station_id, "revision": doc.revision, "action": "mark_active"}
                )
            elif doc.revision != pointer.revision and doc.state == ACTIVE:
                self._write_rev(doc.transition(state=SUPERSEDED))
                repairs.append(
                    {"station_id": station_id, "revision": doc.revision, "action": "supersede"}
                )
        return repairs

    def _stations_with_revisions(self) -> list[str]:
        found: set[str] = set()
        for doc_id in self._store.list_ids():
            parts = doc_id.split("/")
            if len(parts) == 4 and parts[0] == "platform" and parts[2] == "rev":
                found.add(parts[1])
        return sorted(found)

    # ---- 内部 -----------------------------------------------------------

    def _require_rev(self, station_id: str, revision: int) -> MappingRevision:
        doc = self.revision_doc(station_id, revision)
        if doc is None:
            raise MappingError(
                f"站台 {station_id} 没有修订 {revision}", component="platform"
            )
        return doc

    def _next_revision(self, station_id: str, active_revision: int) -> int:
        """分配下一个修订号：取历史与生效指针的最大值再递增，撤回的修订号不复用。"""

        highest = active_revision
        for doc in self.revisions(station_id):
            highest = max(highest, doc.revision)
        return highest + 1

    def _validate_groups(self, station_id: str, groups: dict[str, str]) -> dict[str, str]:
        """校验并规范化映射内容，返回键值都转成字符串的干净副本。"""

        if not station_id:
            raise MappingError("映射的站台标识为空", component="platform")
        if not groups:
            raise MappingError("映射不含任何门区", component="platform")
        clean = {str(key): str(value) for key, value in groups.items()}
        for key, value in clean.items():
            if not key or not value:
                raise MappingError("映射的门区键与物理门区都不能为空", component="platform")
        if self._layout_probe is None:
            return clean
        probe = self._layout_probe(station_id)
        if probe is None:
            raise MappingError(f"站台 {station_id} 没有布局，无法校验映射", component="platform")
        _layout_revision, zone_ids = probe
        unknown = sorted(set(clean.values()) - set(zone_ids))
        if unknown:
            raise MappingError(
                f"物理门区不在站台 {station_id} 的布局中: {','.join(unknown)}",
                component="platform",
            )
        return clean

    def _layout_revision(self, station_id: str) -> int:
        if self._layout_probe is None:
            return 0
        probe = self._layout_probe(station_id)
        return int(probe[0]) if probe else 0


def resolve(registry: MappingRegistry, station_id: str, group_key: str) -> str:
    """把逻辑门区解析成物理门区。"""

    return registry.resolve(station_id, group_key)
