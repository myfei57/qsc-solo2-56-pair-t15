"""门区映射版本与修订流程。

当前映射是门控解释逻辑门区的唯一依据；每次改图都先进入只追加的映射事件流，
不会直接改写当前版本。候选版本必须由两名不同确认人批准，并且在批准之后出现的
下一个站台作业间隙才能激活。旧版本内容按修订号单独保存，任何状态迁移都不会
重写它；撤回只是追加撤回事件，激活新版本也是追加激活事件。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field

from ..audit import AuditLog
from ..clock import Clock, SystemClock
from ..errors import MappingError
from ..store import FileLock, JsonStore, JsonlJournal

SCHEMA_VERSION = 1
PARTIAL = "partially_approved"
APPROVED = "approved"
WITHDRAWN = "withdrawn"
ACTIVE = "active"
OPEN_STATES = {PARTIAL, APPROVED}


def content_hash(groups: dict[str, str]) -> str:
    """计算映射内容的规范哈希，供审计和不可变版本互相核对。"""

    payload = json.dumps(groups, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Mapping:
    """一个站台的当前门区映射。"""

    station_id: str
    groups: dict[str, str]
    revision: int
    schema_version: int = SCHEMA_VERSION
    content_hash: str = ""

    def as_dict(self) -> dict:
        groups = dict(sorted(self.groups.items()))
        return {
            "station_id": self.station_id,
            "groups": groups,
            "revision": self.revision,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash or content_hash(groups),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Mapping":
        groups = payload.get("groups", {})
        clean = (
            {str(key): str(value) for key, value in groups.items()}
            if isinstance(groups, dict)
            else {}
        )
        schema = int(payload.get("schema_version", 1))
        if schema > SCHEMA_VERSION:
            raise MappingError(
                f"映射结构版本 {schema} 高于当前支持版本 {SCHEMA_VERSION}", component="platform"
            )
        digest = str(payload.get("content_hash", "")) or content_hash(clean)
        return cls(
            station_id=str(payload.get("station_id", "")),
            groups=clean,
            revision=int(payload.get("revision", 1)),
            schema_version=schema,
            content_hash=digest,
        )


@dataclass(frozen=True)
class RevisionRecord:
    """不可变的候选或历史映射版本。"""

    station_id: str
    revision: int
    base_revision: int
    groups: dict[str, str]
    content_hash: str
    submitted_by: str
    created_at: float
    schema_version: int = SCHEMA_VERSION
    target_revision: int = 0

    def as_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "revision": self.revision,
            "base_revision": self.base_revision,
            "groups": dict(sorted(self.groups.items())),
            "content_hash": self.content_hash,
            "submitted_by": self.submitted_by,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "target_revision": self.target_revision,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "RevisionRecord":
        groups = payload.get("groups", {})
        return cls(
            station_id=str(payload.get("station_id", "")),
            revision=int(payload.get("revision", 0)),
            base_revision=int(payload.get("base_revision", 0)),
            groups={str(k): str(v) for k, v in groups.items()} if isinstance(groups, dict) else {},
            content_hash=str(payload.get("content_hash", "")),
            submitted_by=str(payload.get("submitted_by", "")),
            created_at=float(payload.get("created_at", 0.0)),
            schema_version=int(payload.get("schema_version", SCHEMA_VERSION)),
            target_revision=int(payload.get("target_revision", 0)),
        )


@dataclass
class Proposal:
    """一张改图单的投影状态。"""

    station_id: str
    revision: int
    base_revision: int
    groups: dict[str, str]
    content_hash: str
    submitted_by: str
    created_at: float
    updated_at: float
    status: str = PARTIAL
    approvers: list[str] = field(default_factory=list)
    ready_after_epoch: int | None = None
    target_revision: int = 0

    def as_dict(self) -> dict:
        return {
            "station_id": self.station_id,
            "revision": self.revision,
            "base_revision": self.base_revision,
            "groups": dict(sorted(self.groups.items())),
            "content_hash": self.content_hash,
            "submitted_by": self.submitted_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "approvers": list(self.approvers),
            "required_approvals": 2,
            "ready": self.status == APPROVED,
            "ready_after_epoch": self.ready_after_epoch,
            "target_revision": self.target_revision,
        }


@dataclass
class RevisionLog:
    """映射修订历史的只读投影。"""

    station_id: str
    revisions: list[int] = field(default_factory=list)

    def latest(self) -> int:
        return self.revisions[-1] if self.revisions else 0

    def as_dict(self) -> dict:
        return {"station_id": self.station_id, "revisions": list(self.revisions)}


@dataclass
class _State:
    current: Mapping | None = None
    active_revision: int = 0
    revisions: dict[int, RevisionRecord] = field(default_factory=dict)
    proposals: dict[int, Proposal] = field(default_factory=dict)

    def revision_numbers(self) -> list[int]:
        return sorted(self.revisions)


class MappingRegistry:
    """门区映射登记表，事件流为事实来源，JSON 文档为快速投影。"""

    def __init__(
        self,
        store: JsonStore,
        audit: AuditLog | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock or SystemClock()
        self._global = threading.RLock()
        self._locks: dict[str, FileLock] = {}
        self._journals: dict[str, JsonlJournal] = {}
        self._states: dict[str, _State] = {}
        self._counts: dict[str, int] = {}
        self._activations = 0

    @property
    def rewrites(self) -> int:
        """累计激活次数；撤回或未生效候选不计入。"""

        return self._activations

    @staticmethod
    def _doc_id(station_id: str) -> str:
        return f"platform/{station_id}"

    @staticmethod
    def _active_doc_id(station_id: str) -> str:
        return f"mapping/{station_id}/active"

    @staticmethod
    def _revision_doc_id(station_id: str, revision: int) -> str:
        return f"mapping/{station_id}/revision/{revision}"

    def _lock(self, station_id: str) -> FileLock:
        with self._global:
            lock = self._locks.get(station_id)
            if lock is None:
                lock = FileLock(self._store.root, f"mapping-{station_id}")
                self._locks[station_id] = lock
            return lock

    def _journal(self, station_id: str) -> JsonlJournal:
        with self._global:
            journal = self._journals.get(station_id)
            if journal is None:
                journal = JsonlJournal(self._store.root, f"mapping-{station_id}", segment_limit=1024)
                self._journals[station_id] = journal
            return journal

    def _now(self, at: float | None) -> float:
        return self._clock.now() if at is None else at

    def _clean_groups(self, groups: dict[str, str]) -> dict[str, str]:
        if not isinstance(groups, dict) or not groups:
            raise MappingError("映射不含任何门区", component="platform")
        clean: dict[str, str] = {}
        for key, value in groups.items():
            key_text = str(key)
            value_text = str(value)
            if not key_text or not value_text:
                raise MappingError("映射的逻辑门区或物理门区为空", component="platform")
            clean[key_text] = value_text
        return dict(sorted(clean.items()))

    def _event_id(self, station_id: str, seq: int, event_type: str) -> str:
        return f"mapping:{station_id}:{seq}:{event_type}"

    def audit_entries(self):
        """返回已落盘审计；方法边界便于测试替身提供最小接口。"""

        return self._audit.list() if self._audit is not None else []

    def _audit_for_event(self, event: dict) -> None:
        """按只追加事件补齐审计，避免事件与审计跨文件部分失败时不一致。"""

        if self._audit is None:
            return
        station_id = str(event.get("station_id", ""))
        at = float(event.get("at", 0.0))
        event_id = str(event.get("id", ""))
        event_type = str(event.get("type", ""))
        kinds = {
            "revision_bootstrapped": "mapping_bootstrapped",
            "revision_proposed": "mapping_proposed",
            "revision_approved": "mapping_approved",
            "revision_withdrawn": "mapping_withdrawn",
            "revision_activated": "mapping_activated",
        }
        kind = kinds.get(event_type)
        if kind is None:
            return
        ignored = {"schema_version", "id", "seq", "station_id", "type", "at", "groups"}
        detail = {key: value for key, value in event.items() if key not in ignored and value is not None}
        self._audit.record(kind, "", station_id, at=at, event_id=event_id, publish=False, **detail)

    def _append_event(self, station_id: str, event_type: str, at: float, **payload: object) -> dict:
        journal = self._journal(station_id)
        seq = journal.count() + 1
        event = {
            "schema_version": SCHEMA_VERSION,
            "id": self._event_id(station_id, seq, event_type),
            "seq": seq,
            "station_id": station_id,
            "type": event_type,
            "at": at,
            **payload,
        }
        previous = self._journal(station_id).read_all()[-1] if seq > 1 else {}
        prev_hash = str(previous.get("event_hash", ""))
        event["prev_hash"] = prev_hash
        event["event_hash"] = self._event_hash(event, prev_hash)
        journal.append(event)
        return event

    @staticmethod
    def _event_hash(event: dict, prev_hash: str) -> str:
        payload = dict(event)
        payload.pop("event_hash", None)
        payload["prev_hash"] = prev_hash
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _set_state(self, station_id: str, state: _State, count: int) -> None:
        self._states[station_id] = state
        self._counts[station_id] = count

    def _load(self, station_id: str) -> _State:
        """从事件流重建某站台状态，并修复缺失的投影文档。"""

        journal = self._journal(station_id)
        count = journal.count()
        cached = self._states.get(station_id)
        if cached is not None and self._counts.get(station_id) == count:
            return cached

        state = _State()
        audited = {
            entry.detail.get("event_id")
            for entry in self.audit_entries()
            if isinstance(entry.detail.get("event_id"), str)
        } if self._audit is not None else set()
        previous_hash = ""
        for event in journal.read_all():
            expected_hash = str(event.get("event_hash", ""))
            actual_hash = self._event_hash(event, str(event.get("prev_hash", "")))
            if expected_hash != actual_hash or str(event.get("prev_hash", "")) != previous_hash:
                raise MappingError("映射事件链校验失败，历史不可变被破坏", component="platform")
            previous_hash = expected_hash
            if self._audit is not None:
                event_id = str(event.get("id", ""))
                if event_id and event_id not in audited:
                    self._audit_for_event(event)
                    audited.add(event_id)
            self._apply(station_id, state, event)
        if state.current is not None:
            self._write_active(station_id, state.current)
        for record in state.revisions.values():
            self._write_revision(record)
        self._set_state(station_id, state, count)
        return state

    def _apply(self, station_id: str, state: _State, event: dict) -> None:
        if int(event.get("schema_version", 1)) > SCHEMA_VERSION:
            raise MappingError("映射事件结构版本不受支持", component="platform")
        event_type = str(event.get("type", ""))
        at = float(event.get("at", 0.0))
        revision = int(event.get("revision", 0))

        if event_type in {"revision_bootstrapped", "revision_proposed"}:
            groups = self._clean_groups(
                event.get("groups", {}) if isinstance(event.get("groups"), dict) else {}
            )
            digest = str(event.get("content_hash", "")) or content_hash(groups)
            if digest != content_hash(groups):
                raise MappingError(f"修订 {revision} 内容哈希不一致", component="platform")
            record = RevisionRecord(
                station_id=station_id,
                revision=revision,
                base_revision=int(event.get("base_revision", 0)),
                groups=groups,
                content_hash=digest,
                submitted_by=str(event.get("submitted_by", "system")),
                created_at=at,
                target_revision=int(event.get("target_revision", 0)),
            )
            state.revisions[revision] = record
            proposal = Proposal(
                station_id=station_id,
                revision=revision,
                base_revision=record.base_revision,
                groups=dict(groups),
                content_hash=digest,
                submitted_by=record.submitted_by,
                created_at=at,
                updated_at=at,
                status=ACTIVE if event_type == "revision_bootstrapped" else PARTIAL,
                target_revision=record.target_revision,
            )
            state.proposals[revision] = proposal
            if event_type == "revision_bootstrapped":
                mapping = self._mapping_from_record(record)
                state.current = mapping
                state.active_revision = revision
                self._activations += 1
            return

        proposal = state.proposals.get(revision)
        if proposal is None:
            raise MappingError(f"映射事件引用了不存在的修订 {revision}", component="platform")
        if event_type == "revision_approved":
            if proposal.status in OPEN_STATES:
                approver = str(event.get("approver", ""))
                if approver and approver not in proposal.approvers:
                    proposal.approvers.append(approver)
                proposal.updated_at = at
                if len(proposal.approvers) >= 2:
                    proposal.status = APPROVED
                    epoch = event.get("ready_after_epoch")
                    proposal.ready_after_epoch = None if epoch is None else int(epoch)
        elif event_type == "revision_withdrawn":
            if proposal.status in OPEN_STATES:
                proposal.status = WITHDRAWN
                proposal.updated_at = at
        elif event_type == "revision_activated":
            record = state.revisions[revision]
            mapping = self._mapping_from_record(record)
            state.current = mapping
            state.active_revision = revision
            proposal.status = ACTIVE
            proposal.updated_at = at
            self._activations += 1
        else:
            raise MappingError(f"未知映射事件类型: {event_type}", component="platform")

    @staticmethod
    def _mapping_from_record(record: RevisionRecord) -> Mapping:
        return Mapping(
            station_id=record.station_id,
            groups=dict(record.groups),
            revision=record.revision,
            schema_version=record.schema_version,
            content_hash=record.content_hash,
        )

    def _write_revision(self, record: RevisionRecord) -> None:
        doc_id = self._revision_doc_id(record.station_id, record.revision)
        raw = self._store.read(doc_id)
        if isinstance(raw, dict):
            stored = RevisionRecord.from_dict(raw)
            if stored.content_hash != record.content_hash:
                raise MappingError(
                    f"修订 {record.revision} 已有不同内容，历史不可变被破坏", component="platform"
                )
            return
        self._store.write(doc_id, record.as_dict())

    def _write_active(self, station_id: str, mapping: Mapping) -> None:
        # 先写版本化指针；旧路径作为兼容投影，由事件流负责补齐。
        self._store.write(self._active_doc_id(station_id), mapping.as_dict())
        self._store.write(self._doc_id(station_id), mapping.as_dict())

    def bootstrap_initial(
        self,
        station_id: str,
        groups: dict[str, str],
        at: float = 0.0,
        submitted_by: str = "system",
    ) -> Mapping:
        """建立安装基线；仅在该站台没有任何映射历史时允许。"""

        if not station_id:
            raise MappingError("映射的站台标识为空", component="platform")
        clean = self._clean_groups(groups)
        with self._lock(station_id).locked():
            state = self._load(station_id)
            if state.current is not None or state.revisions:
                if state.current is not None:
                    return state.current
                raise MappingError(f"站台 {station_id} 已存在候选修订", component="platform")
            legacy = self.current(station_id)
            revision = legacy.revision if legacy is not None else 1
            clean = dict(legacy.groups) if legacy is not None else clean
            digest = legacy.content_hash if legacy is not None else content_hash(clean)
            self._append_event(
                station_id,
                "revision_bootstrapped",
                at,
                revision=revision,
                base_revision=0,
                groups=clean,
                content_hash=digest,
                submitted_by=submitted_by,
            )
            state = self._load(station_id)
            return state.current

    def current(self, station_id: str) -> Mapping | None:
        """读取当前生效映射，不存在时返回 ``None``。"""

        with self._lock(station_id).locked():
            raw = self._store.read(self._active_doc_id(station_id))
            if not isinstance(raw, dict):
                raw = self._store.read(self._doc_id(station_id))
            if not isinstance(raw, dict):
                return None
            mapping = Mapping.from_dict(raw)
            if mapping.content_hash != content_hash(mapping.groups):
                raise MappingError(f"站台 {station_id} 当前映射哈希不一致", component="platform")
            return mapping

    def propose(
        self,
        station_id: str,
        groups: dict[str, str],
        submitted_by: str,
        at: float | None = None,
        base_revision: int | None = None,
        target_revision: int = 0,
    ) -> Proposal:
        """提交一张改图单，不改变当前映射。"""

        if not station_id:
            raise MappingError("映射的站台标识为空", component="platform")
        if not submitted_by:
            raise MappingError("改图提交人为空", component="platform")
        clean = self._clean_groups(groups)
        moment = self._now(at)
        with self._lock(station_id).locked():
            state = self._load(station_id)
            current = state.current
            if current is None:
                raise MappingError(f"站台 {station_id} 没有映射基线", component="platform")
            pending = [item for item in state.proposals.values() if item.status in OPEN_STATES]
            if pending:
                raise MappingError(
                    f"站台 {station_id} 已有待处理修订 {pending[0].revision}", component="platform"
                )
            active_revision = current.revision
            if base_revision is not None and int(base_revision) != active_revision:
                raise MappingError(
                    f"改图基准版本已变化：要求 {base_revision}，当前 {active_revision}",
                    component="platform",
                )
            revision = max(state.revision_numbers(), default=0) + 1
            digest = content_hash(clean)
            self._append_event(
                station_id,
                "revision_proposed",
                moment,
                revision=revision,
                base_revision=active_revision,
                groups=clean,
                content_hash=digest,
                submitted_by=submitted_by,
                target_revision=target_revision,
            )
            state = self._load(station_id)
            return state.proposals[revision]

    def approve(
        self,
        station_id: str,
        revision: int,
        approver: str,
        at: float | None = None,
        ready_after_epoch: int | None = None,
    ) -> Proposal:
        """追加一次确认；第二名不同确认人使改图单具备待生效资格。"""

        if not approver:
            raise MappingError("确认人为空", component="platform")
        moment = self._now(at)
        with self._lock(station_id).locked():
            state = self._load(station_id)
            proposal = self._open_proposal(state, revision)
            if approver == proposal.submitted_by:
                raise MappingError("提交人不能作为确认人", component="platform")
            if approver in proposal.approvers:
                raise MappingError("两名确认人必须是不同人员", component="platform")
            if len(proposal.approvers) >= 2:
                return proposal
            next_count = len(proposal.approvers) + 1
            becomes_ready = next_count >= 2
            if becomes_ready and ready_after_epoch is None:
                raise MappingError("第二次确认时必须记录可生效的作业间隙", component="platform")
            self._append_event(
                station_id,
                "revision_approved",
                moment,
                revision=revision,
                approver=approver,
                approval_seq=next_count,
                required_approvals=2,
                ready_after_epoch=int(ready_after_epoch) if ready_after_epoch is not None else None,
            )
            state = self._load(station_id)
            return state.proposals[revision]

    def withdraw(
        self,
        station_id: str,
        revision: int,
        actor: str,
        at: float | None = None,
        reason: str = "",
    ) -> Proposal:
        """在生效前撤回；旧当前版本继续作为门控依据。"""

        if not actor:
            raise MappingError("撤回操作人为空", component="platform")
        moment = self._now(at)
        with self._lock(station_id).locked():
            state = self._load(station_id)
            proposal = self._open_proposal(state, revision)
            self._append_event(
                station_id,
                "revision_withdrawn",
                moment,
                revision=revision,
                actor=actor,
                reason=reason,
            )
            state = self._load(station_id)
            return state.proposals[revision]

    def activate(
        self,
        station_id: str,
        revision: int,
        current_gap_epoch: int,
        at: float | None = None,
    ) -> Mapping:
        """在批准后的下一个安全作业间隙激活候选修订。"""

        moment = self._now(at)
        with self._lock(station_id).locked():
            state = self._load(station_id)
            proposal = self._open_proposal(state, revision)
            if proposal.status != APPROVED:
                raise MappingError(
                    f"修订 {revision} 尚未取得两名确认人确认", component="platform"
                )
            if proposal.ready_after_epoch is None:
                raise MappingError(f"修订 {revision} 缺少批准时间隙", component="platform")
            if current_gap_epoch <= proposal.ready_after_epoch:
                raise MappingError(
                    "仍处于批准时的作业间隙，需等待下一个安全作业间隙", component="platform"
                )
            current = state.current
            if current is None:
                raise MappingError(f"站台 {station_id} 缺少旧映射", component="platform")
            self._append_event(
                station_id,
                "revision_activated",
                moment,
                revision=revision,
                from_revision=current.revision,
                gap_epoch=current_gap_epoch,
                content_hash=proposal.content_hash,
            )
            state = self._load(station_id)
            activated = state.current
            if activated is None:
                raise MappingError("激活后状态缺失", component="platform")
            return activated

    def _open_proposal(self, state: _State, revision: int) -> Proposal:
        proposal = state.proposals.get(revision)
        if proposal is None:
            raise MappingError(f"修订 {revision} 不存在", component="platform")
        if proposal.status not in OPEN_STATES:
            raise MappingError(f"修订 {revision} 当前状态为 {proposal.status}", component="platform")
        return proposal

    def proposal(self, station_id: str, revision: int) -> Proposal:
        with self._lock(station_id).locked():
            state = self._load(station_id)
            return self._open_proposal(state, revision)

    def pending(self, station_id: str) -> Proposal | None:
        """返回尚未完成或撤回的改图单。"""

        with self._lock(station_id).locked():
            state = self._load(station_id)
            pending = [item for item in state.proposals.values() if item.status in OPEN_STATES]
            return sorted(pending, key=lambda item: item.revision)[0] if pending else None

    def revision_record(self, station_id: str, revision: int) -> RevisionRecord:
        with self._lock(station_id).locked():
            state = self._load(station_id)
            record = state.revisions.get(revision)
            if record is None:
                raise MappingError(f"站台 {station_id} 没有修订 {revision}", component="platform")
            return record

    def list_revisions(self, station_id: str) -> list[RevisionRecord]:
        with self._lock(station_id).locked():
            state = self._load(station_id)
            return [state.revisions[number] for number in state.revision_numbers()]

    def history(self, station_id: str) -> RevisionLog:
        """返回某站台的不可变修订历史。"""

        with self._lock(station_id).locked():
            state = self._load(station_id)
            return RevisionLog(station_id=station_id, revisions=state.revision_numbers())

    def revision(self, station_id: str) -> int:
        """返回当前修订号；没有映射时返回 0。"""

        mapping = self.current(station_id)
        return mapping.revision if mapping else 0

    def resolve(self, station_id: str, group_key: str) -> str:
        """把逻辑门区解析成物理门区。"""

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
        """返回已有映射的站台标识（排序）。"""

        found: list[str] = []
        for doc_id in self._store.list_ids():
            if doc_id.startswith("mapping/") and doc_id.endswith("/active"):
                parts = doc_id.split("/")
                if len(parts) == 3:
                    found.append(parts[1])
            elif doc_id.startswith("platform/") and doc_id.count("/") == 1:
                station_id = doc_id.split("/", 1)[1]
                if station_id not in {"seed", "state"}:
                    found.append(station_id)
        return sorted(set(found))


def resolve(registry: MappingRegistry, station_id: str, group_key: str) -> str:
    """把逻辑门区解析成物理门区。"""

    return registry.resolve(station_id, group_key)
