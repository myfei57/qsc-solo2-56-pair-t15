"""平台装配与控制循环。

``Platform`` 把命名空间、站台、列车、站台门、信号、联锁、门控、门区映射、间隙
探测、配额与审计按真实调用链装到一起，并暴露四件事：到发编排（到站、发车、
门区联动、批量开门、清扫）、控制循环、状态快照与手动检修动作。

控制链固定为「信号 → 停稳对位 → 开门 → 间隙探测 → 关门 → 发车 → 审计」，
任何一步错位都会被对应组件拒绝，并把拒绝原因落进审计。
"""

from __future__ import annotations

from contextlib import contextmanager

from .audit import AuditLog, build_timeline
from .clock import Clock, SystemClock
from .config import PlatformConfig, default_config, with_storage_root
from .door import (
    DoorController,
    active_group,
    batch_open,
    close_and_audit,
    depart,
    depart_policy,
    departed,
    door_report,
    door_state,
    enqueue,
    open_pressure,
    open_doors,
    open_group,
    process_queue,
    queued_total,
)
from .errors import MappingError, RailError
from .eventbus import Event, EventBus
from .gap import GapRegistry, WorkGapRegistry, gap_summary
from .ids import IdFactory
from .interlock import (
    FaultRegistry,
    InterlockArmer,
    interlock_status,
    next_sequence,
    releasable,
    restore_lockouts,
    verdict_of,
)
from .ns import Namespace, NamespaceRegistry
from .platform import MappingRegistry, adjacent, group_keys, physical_groups, resolve
from .psd import LockoutRegistry, PSD, PSDRegistry, sweep_station
from .quota import QuotaRegistry
from .sig import SignalController, aspect_of, depart_checked, refresh_position, signal_summary
from .station import (
    DoorZone,
    Layout,
    LayoutRegistry,
    Schedule,
    ScheduleRegistry,
    Station,
    StationRegistry,
    nearest_zone,
    spread_m,
)
from .store import CommandLog, FileLock, JsonStore, JsonlJournal, StoreMetrics
from .telemetry import TrendBuffer
from .train import (
    AlignRegistry,
    DoorStateRegistry,
    Dwell,
    DwellRegistry,
    PositionTracker,
    Route,
    RouteRegistry,
    Train,
    TrainRegistry,
    train_state,
)

ARRIVAL = "arrival"
DEPART = "depart"


class Platform:
    """站台门区顺序与联锁控制平台。"""

    def __init__(
        self,
        config: PlatformConfig | None = None,
        clock: Clock | None = None,
        unit: str | None = None,
    ) -> None:
        self.config = (config or default_config()).validate()
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.unit = unit or self.config.platform.unit
        self.ids = IdFactory("op")

        store = JsonStore(self.config.storage_root)
        self.store = store
        self.metrics = StoreMetrics(store.root)
        self.audit = AuditLog(
            self.clock,
            self.bus,
            JsonlJournal(store.root, "audit", segment_limit=self.config.audit.journal_segment),
        )
        self.namespace = Namespace(
            self.config.namespace.line, list(self.config.namespace.stations)
        )
        self.namespaces = NamespaceRegistry(self.unit)
        self.trend = TrendBuffer(period_s=self.config.signal.refresh_period_s)

        self.stations = StationRegistry(store)
        self.layouts = LayoutRegistry(store)
        self.schedules = ScheduleRegistry(store)
        self.trains = TrainRegistry(store)
        self.align = AlignRegistry(store, tolerance_m=self.config.station.stop_tolerance_m)
        self.train_doors = DoorStateRegistry(store)
        self.dwells = DwellRegistry(store, max_seconds=self.config.train.dwell_max_s)
        self.positions = PositionTracker(
            store,
            max_position_m=self.config.train.max_position_m,
            stale_after_s=self.config.signal.stale_after_s,
        )
        self.routes = RouteRegistry(store)

        self.gates = PSDRegistry(store)
        self.lockouts = LockoutRegistry(self.gates)
        self.signals = SignalController(store)
        self.faults = FaultRegistry(store)
        self.armers = InterlockArmer(store)
        self.mappings = MappingRegistry(store, self.audit, self.clock)
        self.gaps = GapRegistry(store)
        self.work_gaps = WorkGapRegistry(store)
        self.quotas = QuotaRegistry(store)
        self.commands = CommandLog(store)
        self.doors = DoorController(
            store, self.align, self.commands, self.gates, self.lockouts
        )
        self.doors.set_batch_limit(self.config.door.batch_limit)

        self._cycles = 0
        self._operations = 0
        self._event_counts: dict[str, int] = {}
        self._station_locks: dict[str, FileLock] = {}
        self.bus.subscribe("*", self._on_event)
        self._register_namespaces()
        self._seed(store)
        self._restore()

    def _on_event(self, event: Event) -> None:
        """统计总线事件，供状态接口判断动作有没有真的流过组件。"""

        self._event_counts[event.topic] = self._event_counts.get(event.topic, 0) + 1

    # ---- 装配 ---------------------------------------------------------

    def _register_namespaces(self) -> None:
        """登记现场设备点位（控制台与审计共用同一套名字）。"""

        for area, device, kind in (
            ("line", "signal", "signal-controller"),
            ("line", "sequence", "sequence-source"),
            ("station", "platform", "platform-screen"),
            ("station", "gap", "gap-detector"),
            ("station", "door", "platform-door"),
            ("station", "interlock", "door-interlock"),
            ("station", "quota", "quota-counter"),
            ("station", "audit", "audit-sink"),
        ):
            self.namespaces.register(area, device, kind)

    def _seed(self, store: JsonStore) -> None:
        """按线路命名空间布置站台、布局、映射与站台门。"""

        seed_doc = "platform/seed"
        if store.exists(seed_doc):
            return
        groups_per_station = self.config.station.groups_per_station
        doors_per_group = self.config.station.doors_per_group
        span = self.config.station.group_span_m
        for station_index, station_id in enumerate(self.namespace.station_ids(), start=1):
            groups = [f"G{index:02d}" for index in range(1, groups_per_station + 1)]
            self.stations.upsert(
                Station(
                    id=station_id,
                    name=f"站台 {station_index}",
                    line=self.namespace.line_id(),
                    door_groups=groups,
                )
            )
            base = (station_index - 1) * span
            zones = [
                DoorZone(id=group, distance_m=int(base + (index - 1) * span / groups_per_station))
                for index, group in enumerate(groups, start=1)
            ]
            self.layouts.save(Layout(station_id=station_id, zones=zones))
            self.mappings.bootstrap_initial(
                station_id,
                {group: group for group in groups},
                at=0.0,
                submitted_by="installer",
            )
            self.schedules.save(
                Schedule(station_id=station_id, headways=[120, 150, 180, 240])
            )
            for group in groups:
                for door_index in range(1, doors_per_group + 1):
                    self.gates.upsert(
                        PSD(
                            id=f"{station_id}-{group}-{door_index:02d}",
                            station=station_id,
                            group=group,
                        )
                    )
        for name, limit in (
            (ARRIVAL, self.config.quota.arrival_limit),
            (DEPART, self.config.quota.depart_limit),
        ):
            self.quotas.set_limit(name, limit)
        store.write(
            seed_doc,
            {"line": self.namespace.line_id(), "stations": self.namespace.station_ids()},
        )

    def _restore(self) -> None:
        """从落盘状态恢复操作计数与锁定状态。"""

        state = self.store.read("platform/state")
        if isinstance(state, dict):
            self._operations = int(state.get("operations", 0))
            self._cycles = int(state.get("cycles", 0))
        restore_lockouts(self.faults, self.gates, self.lockouts, not self.gaps.blocked_ids())
        self._reconcile_mappings()

    def _persist(self) -> None:
        """落盘平台计数，供重启后对照。"""

        self.store.write(
            "platform/state",
            {
                "operations": self._operations,
                "cycles": self._cycles,
                "unit": self.unit,
                "line": self.namespace.line_id(),
            },
        )

    def _station_lock(self, station_id: str) -> FileLock:
        """返回某站台的跨进程关键区段锁。"""

        if station_id not in self._station_locks:
            self._station_locks[station_id] = FileLock(self.store.root, f"station-{station_id}")
        return self._station_locks[station_id]

    @contextmanager
    def _station_guard(self, station_id: str):
        """串行化同一站台到发、清扫与映射生效边界。"""

        if not station_id:
            raise RailError("站台标识为空", component="platform")
        with self._station_lock(station_id).locked():
            yield

    def _reconcile_mappings(self) -> None:
        """把旧版当前映射补登记为安装基线，并重放未完成投影。"""

        for station_id in self.namespace.station_ids():
            if self.mappings.history(station_id).revisions:
                continue
            mapping = self.mappings.current(station_id)
            if mapping is not None:
                self.mappings.bootstrap_initial(
                    station_id,
                    mapping.groups,
                    at=0.0,
                    submitted_by="migration",
                )

    def _record_operation(self, op_id: str, operation: str, report: dict) -> dict:
        """把编排步骤逐条写事件，并记一条汇总审计。"""

        train_id = str(report.get("train_id", ""))
        station_id = str(report.get("station_id", ""))
        reserved = {
            "train_id",
            "station_id",
            "op_id",
            "severity",
            "message",
            "component",
            "action",
            "detail",
        }
        for step in report.get("steps", []):
            detail = {
                key: value
                for key, value in dict(step.get("detail", {})).items()
                if not isinstance(value, (dict, list)) and key not in reserved
            }
            self.bus.publish(
                Event.of(
                    "door",
                    str(step.get("component", "door")),
                    str(step.get("action", "step")),
                    op_id=op_id,
                    train_id=train_id,
                    severity="notice",
                    **detail,
                )
            )
        for violation in report.get("violations", []):
            self.bus.publish(
                Event.of(
                    "audit",
                    "interlock",
                    "interlock_rejected",
                    op_id=op_id,
                    train_id=train_id,
                    station_id=station_id,
                    severity="alarm",
                    message=f"{operation} 被拒绝：{violation}",
                    violation=violation,
                )
            )
        entry = self.audit.record(
            f"{operation}_complete",
            train_id,
            station_id,
            op_id=op_id,
            ok=bool(report.get("ok")),
            violations=list(report.get("violations", [])),
        )
        return {
            "op_id": op_id,
            "operation": operation,
            "ok": bool(report.get("ok")),
            "violations": list(report.get("violations", [])),
            "audit_id": entry.id,
        }

    # ---- 生命周期 -----------------------------------------------------

    def start(self) -> dict:
        """启动平台：核对命名空间、站台、布局与映射是否一致。"""

        missing: dict[str, list[str]] = {}
        for station_id in self.namespace.station_ids():
            if self.stations.get(station_id) is None:
                missing.setdefault("station", []).append(station_id)
            if self.layouts.get(station_id) is None:
                missing.setdefault("layout", []).append(station_id)
            if self.mappings.current(station_id) is None:
                missing.setdefault("mapping", []).append(station_id)
        return {
            "unit": self.unit,
            "line": self.namespace.label(),
            "missing": missing,
            "stations": len(self.namespace.station_ids()),
            "gates": len(self.gates.list()),
            "operations": self._operations,
            "ready": not missing,
        }

    def stop(self) -> dict:
        """停运平台：整站清扫后收起全部灯位。"""

        op_id = self.ids.labeled("stop")
        steps: list[dict] = []
        closed = 0
        for station_id in self.namespace.station_ids():
            report = self.sweep(station_id)
            closed += report["closed"]
            steps.append(
                {
                    "component": "psd",
                    "action": "sweep",
                    "detail": {"station_id": station_id, "closed": report["closed"]},
                }
            )
        for train in self.trains.list():
            self.signals.clear(train.id, ARRIVAL)
            self.signals.clear(train.id, DEPART)
        report = {"ok": True, "train_id": "", "closed": closed, "steps": steps, "violations": []}
        summary = self._record_operation(op_id, "stop", report)
        self._operations += 1
        self._persist()
        return {**report, **summary}

    # ---- 到发编排 -----------------------------------------------------

    def arrive(self, train_id: str, station_id: str, position_m: int) -> dict:
        """到站到开门：进入作业 → 信号到站 → 对位落盘 → 开门。"""

        op_id = self.ids.labeled("arrive")
        steps: list[dict] = []
        violations: list[str] = []

        def reject(reason: str) -> dict:
            violations.append(reason)
            self.bus.publish(
                Event.of(
                    "audit",
                    "interlock",
                    "arrive_rejected",
                    op_id=op_id,
                    train_id=train_id,
                    station_id=station_id,
                    severity="alarm",
                    message=reason,
                )
            )
            return {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": violations,
            }

        if not self.quotas.consume(ARRIVAL):
            return reject("到站配额已用完")
        try:
            self.namespace.require(station_id)
        except RailError as error:
            return reject(error.message)
        if self.stations.get(station_id) is None:
            return reject(f"站台未登记: {station_id}")

        with self._station_guard(station_id):
            previous_gap = self.work_gaps.get(station_id)
            try:
                work_gap = self.work_gaps.enter(station_id, train_id, self.clock.now())
            except RailError as error:
                return reject(error.message)
            if previous_gap.state == "occupied" and previous_gap.train_id == train_id:
                return {
                    "ok": True,
                    "train_id": train_id,
                    "station_id": station_id,
                    "position_m": self.align.get(train_id).position_m
                    if self.align.get(train_id)
                    else 0,
                    "service_seq": previous_gap.service_seq,
                    "steps": [],
                    "violations": [],
                    "idempotent": True,
                }
            service_seq = work_gap.service_seq
            try:
                self.signals.raise_signal(train_id, ARRIVAL, self.clock.now())
                steps.append(
                    {"component": "sig", "action": "arrival_signal", "detail": {"train_id": train_id}}
                )
                if self.trains.get(train_id) is None:
                    self.trains.upsert(Train(id=train_id, line=self.namespace.line_id()))
                self.trains.assign_station(train_id, station_id)
                self._reset_train_service(train_id, station_id)

                stop_position = self._stop_position(station_id)
                try:
                    alignment = self.align.align(
                        train_id, position_m, stop_position, self.clock.now()
                    )
                except RailError as error:
                    self.work_gaps.force_state(previous_gap)
                    return reject(error.message)
                steps.append(
                    {
                        "component": "train",
                        "action": "align",
                        "detail": {"train_id": train_id, "position_m": alignment.position_m},
                    }
                )
                try:
                    opened_report = self.doors_open(train_id, station_id, service_seq)
                except RailError as error:
                    self._reset_train_service(train_id, station_id)
                    self.work_gaps.force_state(previous_gap)
                    return reject(error.message)
            except Exception:
                self.work_gaps.force_state(previous_gap)
                raise
        entry = self.audit.record(
            ARRIVAL,
            train_id,
            station_id,
            position_m=position_m,
            service_seq=service_seq,
            mapping_revision=int(opened_report.get("mapping_revision", 0)),
            mapping_hash=str(opened_report.get("mapping_hash", "")),
            gap_epoch=work_gap.epoch,
        )
        report = {
            "ok": True,
            "train_id": train_id,
            "station_id": station_id,
            "position_m": alignment.position_m,
            "service_seq": service_seq,
            "mapping_revision": int(opened_report.get("mapping_revision", 0)),
            "mapping_hash": str(opened_report.get("mapping_hash", "")),
            "steps": steps,
            "violations": [],
        }
        summary = self._record_operation(op_id, "arrive", report)
        self._operations += 1
        self._persist()
        return {**report, **summary, "arrival_audit": entry.id}

    def doors_open(self, train_id: str, station_id: str, service_seq: int = 0) -> dict:
        """开门并确认车门：对位必须先落盘。"""

        report = open_doors(
            self.doors,
            train_id,
            self.clock.now(),
            station_id=station_id,
            service_seq=service_seq,
        )
        group = self._primary_group(station_id)
        linked: dict = {}
        if group:
            command_id = f"arrive:{station_id}:{train_id}:{service_seq}"
            resolved_group = self.mappings.resolve(station_id, group)
            before = {
                gate.id: gate.open
                for gate in self.gates.by_station(station_id)
                if gate.group == resolved_group
            }
            try:
                linked = open_group(
                    self.doors, self.mappings, station_id, group, command_id=command_id
                )
            except Exception:
                # 门区联动若部分完成，必须恢复执行前的开闭状态，避免失败请求产生开门副作用。
                for gate_id, was_open in before.items():
                    gate = self.gates.get(gate_id)
                    if gate is not None and gate.open != was_open:
                        self.gates.set_open(gate_id, was_open)
                self.store.remove(f"door/{station_id}/active_group")
                raise
        self.train_doors.confirm_open(train_id, self.clock.now())
        mapping_revision = int(linked.get("revision", 0)) if linked else 0
        mapping_hash = str(linked.get("content_hash", "")) if linked else ""
        self.bus.publish(
            Event.of(
                "door",
                "door",
                "opened",
                train_id=train_id,
                station_id=station_id,
                severity="notice",
                group=group,
                service_seq=service_seq,
                mapping_revision=mapping_revision,
                mapping_hash=mapping_hash,
            )
        )
        return {
            **report,
            "group": group,
            "mapping_revision": mapping_revision,
            "mapping_hash": mapping_hash,
        }

    def depart(self, train_id: str, station_id: str) -> dict:
        """关门到发车：关门 → 间隙确认 → 发车 → 清扫并开放下一间隙。"""

        op_id = self.ids.labeled("depart")
        steps: list[dict] = []

        def fail(reason: str, locked_gate_id: str = "") -> dict:
            report = {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": [reason],
            }
            summary = self._record_operation(op_id, "depart", report)
            if locked_gate_id and self.lockouts.is_locked(locked_gate_id):
                self.lockouts.clear(locked_gate_id)
            return {**report, **summary}

        if not self.quotas.consume(DEPART):
            report = {
                "ok": False,
                "train_id": train_id,
                "station_id": station_id,
                "steps": steps,
                "violations": ["发车配额已用完"],
            }
            summary = self._record_operation(op_id, "depart", report)
            return {**report, **summary}

        with self._station_guard(station_id):
            service_seq = self.work_gaps.active_service(station_id, train_id)
            if service_seq is None:
                return fail("列车不在该站台的当前作业中")
            gate = self._primary_gate(station_id)
            locked_gate_id = ""
            if gate is not None and not self.lockouts.is_locked(gate.id):
                self.lockouts.set(gate.id, True)
                locked_gate_id = gate.id
                steps.append(
                    {"component": "interlock", "action": "lock", "detail": {"psd_id": gate.id}}
                )
            try:
                closed = close_and_audit(
                    self.doors, self.audit, train_id, station_id, DEPART, self.clock.now()
                )
            except RailError as error:
                return fail(error.message, locked_gate_id)
            steps.append(
                {"component": "door", "action": "close_durable", "detail": {"train_id": train_id}}
            )
            gap_state = self.gaps.state(train_id)
            steps.append(
                {
                    "component": "gap",
                    "action": "check",
                    "detail": {"train_id": train_id, "state": gap_state},
                }
            )
            verdict = depart_checked(self.signals, self.store, self.gaps, train_id, self.clock.now())
            if not verdict["ok"]:
                return fail("; ".join(verdict["violations"]), locked_gate_id)
            steps.append(
                {"component": "sig", "action": "depart_signal", "detail": {"train_id": train_id}}
            )
            departure = depart(self.doors, train_id, self.clock.now())
            if gate is not None:
                self.lockouts.clear(gate.id)
                locked_gate_id = ""
            swept = sweep_station(self.gates, station_id)
            work_gap = self.work_gaps.exit(station_id, train_id, self.clock.now())
            steps.append(
                {
                    "component": "psd",
                    "action": "sweep",
                    "detail": {"station_id": station_id, "closed": swept["closed"]},
                }
            )
            activated = self.apply_ready_mappings(station_id)
        report = {
            "ok": True,
            "train_id": train_id,
            "station_id": station_id,
            "close_durable": bool(closed["close_durable"]),
            "aspect": verdict["aspect"],
            "cleared_commands": departure["cleared_commands"],
            "swept": swept["closed"],
            "gap_epoch": work_gap.epoch,
            "activated_mapping": activated,
            "steps": steps,
            "violations": [],
        }
        summary = self._record_operation(op_id, "depart", report)
        self._operations += 1
        self._persist()
        return {**report, **summary}

    def check_gap(self, train_id: str, clear: bool = True) -> dict:
        """确认间隙无异常，或登记一次可疑探测结果。"""

        if clear:
            result = self.gaps.check(train_id, self.clock.now())
        else:
            result = self.gaps.probe(train_id, False, self.clock.now())
        self.audit.record("gap_probe", train_id, "", clear=clear)
        return {"ok": True, "train_id": train_id, "clear": result.clear, "violations": []}

    def mark_work_gap(self, station_id: str, actor: str) -> dict:
        """由现场登记一次安全间隙边界，用于首车前的维护切换。"""

        self._require_station(station_id)
        with self._station_guard(station_id):
            gap = self.work_gaps.mark_clear(station_id, self.clock.now())
            activated = self.apply_ready_mappings(station_id)
            self.audit.record(
                "work_gap_marked",
                "",
                station_id,
                actor=actor,
                gap_epoch=gap.epoch,
                activated=bool(activated),
            )
        return {
            "ok": True,
            "station_id": station_id,
            "work_gap": self.work_gaps.status(station_id),
            "activated_mapping": activated,
            "violations": [],
        }

    def submit_mapping(
        self,
        station_id: str,
        groups: dict[str, str],
        submitted_by: str,
        base_revision: int | None = None,
    ) -> dict:
        """提交改图单；候选映射不会影响当前开门。"""

        self._require_station(station_id)
        self._validate_mapping_groups(station_id, groups)
        proposal = self.mappings.propose(
            station_id,
            groups,
            submitted_by=submitted_by,
            at=self.clock.now(),
            base_revision=base_revision,
        )
        return {
            "ok": True,
            "station_id": station_id,
            "proposal": proposal.as_dict(),
            "active_revision": self.mappings.revision(station_id),
            "violations": [],
        }

    def approve_mapping(self, station_id: str, revision: int, approver: str) -> dict:
        """追加一次双人确认；第二次确认记录批准时的作业间隙。"""

        self._require_station(station_id)
        with self._station_guard(station_id):
            ready_epoch = self.work_gaps.clear_epoch(station_id)
            if ready_epoch is None:
                raise RailError("站台正在作业，不能在此间隙完成第二次确认", component="gap")
            proposal = self.mappings.approve(
                station_id,
                revision,
                approver=approver,
                at=self.clock.now(),
                ready_after_epoch=ready_epoch,
            )
        return {
            "ok": True,
            "station_id": station_id,
            "proposal": proposal.as_dict(),
            "work_gap": self.work_gaps.status(station_id),
            "violations": [],
        }

    def withdraw_mapping(self, station_id: str, revision: int, actor: str, reason: str = "") -> dict:
        """生效前撤回候选；当前旧版本继续可用。"""

        self._require_station(station_id)
        with self._station_guard(station_id):
            proposal = self.mappings.withdraw(
                station_id, revision, actor=actor, at=self.clock.now(), reason=reason
            )
        return {"ok": True, "station_id": station_id, "proposal": proposal.as_dict(), "violations": []}

    def apply_ready_mappings(self, station_id: str) -> dict | None:
        """若已到批准后的下一个安全间隙，则激活待生效映射。"""

        pending = self.mappings.pending(station_id)
        if pending is None or pending.status != "approved":
            return None
        gap = self.work_gaps.get(station_id)
        if gap.state != "clear" or pending.ready_after_epoch is None:
            return None
        if gap.epoch <= pending.ready_after_epoch:
            return None
        mapping = self.mappings.activate(
            station_id, pending.revision, gap.epoch, at=self.clock.now()
        )
        return mapping.as_dict()

    def activate_ready_mappings(self, station_id: str) -> dict:
        """控制台/周期显式触发一次安全间隙生效检查。"""

        self._require_station(station_id)
        with self._station_guard(station_id):
            activated = self.apply_ready_mappings(station_id)
            pending = self.mappings.pending(station_id)
        return {
            "ok": activated is not None,
            "station_id": station_id,
            "activated": activated,
            "pending": pending.as_dict() if pending else None,
            "work_gap": self.work_gaps.status(station_id),
            "violations": [],
        }

    def rollback_mapping(
        self,
        station_id: str,
        target_revision: int,
        submitted_by: str,
        approver_one: str,
        approver_two: str,
    ) -> dict:
        """把旧版本作为一张新的回滚候选提交并双人确认。"""

        record = self.mappings.revision_record(station_id, target_revision)
        actors = {submitted_by, approver_one, approver_two}
        if len(actors) != 3:
            raise MappingError("回滚提交人与两名确认人必须互不相同", component="platform")
        if record.revision == self.mappings.revision(station_id):
            raise MappingError("目标版本已经是当前生效版本", component="platform")
        submit = self.submit_mapping(
            station_id,
            dict(record.groups),
            submitted_by=submitted_by,
            base_revision=self.mappings.revision(station_id),
        )
        proposal_id = int(submit["proposal"]["revision"])
        self.mappings.approve(
            station_id, proposal_id, approver=approver_one, at=self.clock.now()
        )
        second = self.approve_mapping(station_id, proposal_id, approver_two)
        return {
            "ok": True,
            "station_id": station_id,
            "rollback_from": target_revision,
            "proposal": second["proposal"],
            "violations": [],
        }

    def mapping_workflow(self, station_id: str) -> dict:
        """返回当前版本、候选单、历史版本和作业间隙。"""

        self._require_station(station_id)
        mapping = self.mappings.current(station_id)
        pending = self.mappings.pending(station_id)
        return {
            "station_id": station_id,
            "active": mapping.as_dict() if mapping else None,
            "pending": pending.as_dict() if pending else None,
            "revisions": [item.as_dict() for item in self.mappings.list_revisions(station_id)],
            "work_gap": self.work_gaps.status(station_id),
        }

    def renumber(self, station_id: str, groups: dict[str, str]) -> dict:
        """兼容旧演示接口：提交并由两个检修账号确认，仍等待下一作业间隙。"""

        proposal = self.submit_mapping(station_id, groups, submitted_by="legacy-operator")
        revision = int(proposal["proposal"]["revision"])
        self.mappings.approve(station_id, revision, approver="legacy-reviewer-a")
        approved = self.approve_mapping(station_id, revision, "legacy-reviewer-b")
        return {
            "ok": True,
            "station_id": station_id,
            "revision": revision,
            "mapping": approved["proposal"]["groups"],
            "active_group": "",
            "proposal": approved["proposal"],
            "violations": [],
        }

    def enqueue(self, train_id: str, command_id: str, kind: str, station_id: str = "") -> dict:
        """把一条门控指令排进队列。"""

        command = enqueue(self.doors, train_id, command_id, kind, station_id)
        self.audit.record(
            "door_enqueue", train_id, station_id, command_id=command.id, command_kind=kind
        )
        return {"ok": True, "train_id": train_id, "command": command.as_dict(), "violations": []}

    def process_queue(self, train_id: str) -> dict:
        """返回列车队列里待执行的门控指令，并带上发车自检结果。"""

        policy = depart_policy(self.doors, train_id)
        ids = process_queue(self.doors, train_id)
        self.audit.record("door_process", train_id, "", queued=len(ids))
        return {
            "ok": True,
            "train_id": train_id,
            "ids": ids,
            "ready": policy["allowed"],
            "violations": list(policy["failures"]),
        }

    def clear_fault(self, psd_id: str) -> dict:
        """清除门区故障并按条件解锁。"""

        gate = self.gates.require(psd_id)
        allowed, reason = releasable(self.gates, psd_id, True)
        if not allowed:
            return {"ok": False, "psd_id": psd_id, "locked": True, "violations": [reason]}
        self.faults.clear_fault(self.lockouts, psd_id, self.clock.now())
        self.audit.record("interlock_clear", "", gate.station, psd_id=psd_id)
        return {"ok": True, "psd_id": psd_id, "locked": False, "violations": []}

    def set_fault(self, psd_id: str) -> dict:
        """置位门区故障并锁定。"""

        gate = self.gates.require(psd_id)
        self.faults.set_fault(self.lockouts, psd_id, self.clock.now())
        self.audit.record("interlock_fault", "", gate.station, psd_id=psd_id)
        return {"ok": True, "psd_id": psd_id, "locked": True, "violations": []}

    def arm(self, train_id: str, armed: bool) -> dict:
        """启用或退出一列车的联锁。"""

        state = self.armers.arm(train_id, armed)
        self.audit.record("interlock_arm", train_id, "", armed=state)
        return {"ok": True, "train_id": train_id, "armed": state, "violations": []}

    def refresh_position(self, train_id: str, position_m: int) -> dict:
        """按信号刷新列车位置。"""

        outcome = refresh_position(self.positions, train_id, position_m, self.clock.now())
        if outcome["ok"]:
            self.trend.append("train_position_m", self.clock.now(), position_m)
        else:
            self.audit.record("position_rejected", train_id, "", reason=outcome["reason"])
        return {**outcome, "violations": [] if outcome["ok"] else [outcome["reason"]]}

    def sequence_verdict(self, previous: int, step: int) -> dict:
        """推进门控事件序号并给出回绕判定。"""

        current = next_sequence(previous, step)
        verdict = verdict_of(previous, current)
        self.audit.record(
            "sequence_verdict",
            "",
            "",
            previous=verdict.previous,
            current=verdict.current,
            advanced=verdict.advanced,
        )
        return {"ok": verdict.advanced, "verdict": verdict.as_dict(), "violations": [] if verdict.advanced else [verdict.reason]}

    def batch_open(self, station_id: str) -> dict:
        """按布局里程顺序批量开门。"""

        layout = self.layouts.get(station_id)
        zones = list(layout.zones) if layout else []
        outcome = batch_open(self.doors, station_id, zones)
        nearest = nearest_zone(zones)
        self.audit.record(
            "batch_open",
            "",
            station_id,
            opened=len(outcome["opened"]),
            skipped=len(outcome["skipped"]),
        )
        return {
            "ok": not outcome["skipped"],
            "station_id": station_id,
            "order": outcome["order"],
            "opened": outcome["opened"],
            "deferred": outcome["deferred"],
            "skipped": outcome["skipped"],
            "nearest": nearest.id if nearest else "",
            "violations": [],
        }

    def sweep(self, station_id: str) -> dict:
        """整站清扫；若用于修复失败发车，成功后关闭该作业间隙。"""

        self._require_station(station_id)
        with self._station_guard(station_id):
            outcome = sweep_station(self.gates, station_id)
            gap = self.work_gaps.get(station_id)
            if gap.state == "occupied" and departed(self.doors, gap.train_id):
                work_gap = self.work_gaps.exit(station_id, gap.train_id, self.clock.now())
                activated = self.apply_ready_mappings(station_id)
                outcome["closed_gap_epoch"] = work_gap.epoch
                outcome["activated_mapping"] = activated
        return outcome

    def register_dwell(self, train_id: str, seconds: float, station_id: str = "") -> dict:
        """登记停站时长。"""

        dwell = self.dwells.set(Dwell(train_id=train_id, seconds=seconds, station_id=station_id))
        return {"ok": True, "dwell": dwell.as_dict(), "violations": []}

    def assign_route(self, train_id: str, origin: str, destination: str, distance_m: int) -> dict:
        """登记运行交路。"""

        route = self.routes.assign(
            Route(train_id=train_id, origin=origin, destination=destination, distance_m=distance_m)
        )
        return {"ok": True, "route": route.__dict__, "violations": []}

    # ---- 控制循环 -----------------------------------------------------

    def cycle(self, dt: float = 1.0) -> dict:
        """推进一个控制周期，返回本周期摘要。"""

        if dt <= 0:
            return {"ok": False, "violations": ["步长必须为正"], "cycle": self._cycles}
        self._cycles += 1
        moment = self.clock.now()
        stale = [
            train.id for train in self.trains.list() if self.positions.stale(train.id, moment)
        ]
        interlock = interlock_status(self.faults, self.gates, self.config.interlock.max_locked_ratio)
        holding = [
            train.id
            for train in self.trains.list()
            if not self.armers.armed(train.id) and depart_policy(self.doors, train.id)["allowed"]
        ]
        self.trend.append("locked_count", moment, interlock["locked_count"])
        self.trend.append("queued_commands", moment, queued_total(self.doors))
        self.trend.append("audit_entries", moment, len(self.audit.list()))
        activated: list[dict] = []
        for station_id in self.namespace.station_ids():
            with self._station_lock(station_id).locked():
                mapping = self.apply_ready_mappings(station_id)
            if mapping is not None:
                activated.append(mapping)
        self._persist()
        return {
            "ok": True,
            "cycle": self._cycles,
            "dt": dt,
            "stale_positions": stale,
            "locked_count": interlock["locked_count"],
            "can_release_depart": interlock["can_release_depart"],
            "ready_trains": holding,
            "gaps": gap_summary(self.gaps, [train.id for train in self.trains.list()]),
            "activated_mappings": activated,
            "violations": [],
        }

    # ---- 查询 ---------------------------------------------------------

    def status(self) -> dict:
        """汇总状态快照，供控制台与命令行读取。"""

        trains = self.trains.list()
        ids = [train.id for train in trains]
        return {
            "platform": self.config.name,
            "unit": self.unit,
            "line": self.namespace.label(),
            "operations": self._operations,
            "cycles": self._cycles,
            "utc": self.clock.utc_iso(),
            "namespace": self.namespace.as_dict(),
            "namespaces": self.namespaces.describe(),
            "open_gates": self.gates.count_open(),
            "lockout_changes": self.lockouts.changes,
            "signals_raised": self.signals.raises,
            "signals_cleared": self.signals.clears,
            "interlock_counters": {
                "faults_set": self.faults.sets,
                "faults_cleared": self.faults.clears,
                "armed": self.armers.arms,
                "disarmed": self.armers.disarms,
                "armed_trains": self.armers.armed_trains(),
            },
            "gap_engine": {"checks": self.gaps.checks, "probes": self.gaps.probes},
            "event_counts": dict(sorted(self._event_counts.items())),
            "console_hits": len(self.bus.history("console")),
            "stations": [station.as_dict() for station in self.stations.list()],
            "trains": {train.id: train_state(train.id, self.align, self.train_doors, self.positions) for train in trains},
            "doors": [
                {**gate.as_dict(), "open": gate.open, "locked": gate.locked}
                for gate in self.gates.list()
            ],
            "door_report": door_report(self.doors, self.namespace.station_ids()),
            "signals": signal_summary(self.signals, ids),
            "interlock": interlock_status(
                self.faults, self.gates, self.config.interlock.max_locked_ratio
            ),
            "gaps": gap_summary(self.gaps, ids),
            "work_gaps": [self.work_gaps.status(station_id) for station_id in self.namespace.station_ids()],
            "quota": {
                ARRIVAL: self.quotas.status(ARRIVAL),
                DEPART: self.quotas.status(DEPART),
            },
            "store": self.metrics.summary(),
            "audit": self.audit.summary(),
            "trend": self.trend.snapshot(limit=4),
            "ids_issued": self.ids.issued(),
        }

    def snapshot(self) -> dict:
        """返回只读的整体快照（状态 + 命名空间 + 门区映射 + 审计时间线）。"""

        return {
            "status": self.status(),
            "groups": {
                station_id: group_keys(self.mappings, station_id)
                for station_id in self.namespace.station_ids()
            },
            "physical_groups": {
                station_id: physical_groups(self.mappings, station_id)
                for station_id in self.namespace.station_ids()
            },
            "mapping_revisions": {
                station_id: {
                    "active": self.mappings.revision(station_id),
                    "latest_candidate": self.mappings.history(station_id).latest(),
                    "log": self.mappings.history(station_id).as_dict()["revisions"],
                    "pending": (
                        self.mappings.pending(station_id).as_dict()
                        if self.mappings.pending(station_id)
                        else None
                    ),
                    "work_gap": self.work_gaps.status(station_id),
                }
                for station_id in self.namespace.station_ids()
            },
            "adjacent": self._adjacent_sample(),
            "timeline": build_timeline(self.audit.list(), limit=20),
        }

    def train_view(self, train_id: str) -> dict:
        """返回一列车的综合视图。"""

        return {
            "train": self.trains.get(train_id).as_dict() if self.trains.get(train_id) else None,
            "state": train_state(train_id, self.align, self.train_doors, self.positions),
            "doors": door_state(self.doors, train_id),
            "aspect": aspect_of(self.signals, train_id),
            "gap": self.gaps.state(train_id),
        }

    def station_view(self, station_id: str) -> dict:
        """返回一个站台的综合视图。"""

        layout = self.layouts.get(station_id)
        mapping = self.mappings.current(station_id)
        zones = list(layout.zones) if layout else []
        schedule = self.schedules.get(station_id)
        return {
            "station": self.stations.get(station_id).as_dict() if self.stations.get(station_id) else None,
            "group_count": self.stations.group_count(station_id),
            "zones": [zone.as_dict() for zone in zones],
            "order": [zone.id for zone in zones],
            "zone_distances": {
                zone.id: self.layouts.zone_distance(station_id, zone.id) for zone in zones
            },
            "span_m": spread_m(zones),
            "minimum_headway_s": self.schedules.minimum_headway(station_id) if schedule else 0,
            "revision": mapping.revision if mapping else 0,
            "groups": group_keys(self.mappings, station_id),
            "active_group": active_group(self.doors, station_id),
            "pending_mapping": self.mappings.pending(station_id).as_dict()
            if self.mappings.pending(station_id)
            else None,
            "work_gap": self.work_gaps.status(station_id),
            "pressure": open_pressure(self.doors, station_id),
        }

    def resolve_group(self, station_id: str, group_key: str) -> dict:
        """解析一个逻辑门区。"""

        group = resolve(self.mappings, station_id, group_key)
        return {
            "station_id": station_id,
            "group_key": group_key,
            "group": group,
            "neighbours": adjacent(self.mappings, station_id, group_key),
        }

    def note_error(self, error: RailError) -> dict:
        """把平台异常写入审计，返回落盘记录。"""

        entry = self.audit.record(
            "error",
            "",
            "",
            component=error.component,
            message=error.message,
            error_type=type(error).__name__,
        )
        return entry.as_dict()

    def reset_state(self) -> bool:
        """清掉落盘的平台计数（交付前重新标定用）。"""

        if not self.store.exists("platform/state"):
            return False
        return self.store.remove("platform/state")

    # ---- 内部 ---------------------------------------------------------

    def _reset_train_service(self, train_id: str, station_id: str = "") -> None:
        """进入新车-站作业前清掉上一轮的门控、对位和间隙事实。"""

        for doc_id in (
            f"door/{train_id}/open",
            f"door/{train_id}/close",
            f"door/{train_id}/departed",
            f"train/{train_id}/align",
            f"train/{train_id}/door",
            f"gap/{train_id}",
        ):
            self.store.remove(doc_id)
        if station_id:
            self.store.remove(f"door/{station_id}/active_group")
        self.commands.clear(train_id)

    def _require_station(self, station_id: str) -> None:
        self.namespace.require(station_id)
        if self.stations.get(station_id) is None:
            raise RailError(f"站台未登记: {station_id}", component="console")

    def _validate_mapping_groups(self, station_id: str, groups: dict[str, str]) -> None:
        """提交前验证映射能覆盖逻辑门区并解析到本站现存物理门区。"""

        if not isinstance(groups, dict) or not groups:
            raise MappingError("映射必须是非空对象", component="platform")
        current = self.mappings.current(station_id)
        if current is None:
            raise MappingError(f"站台 {station_id} 没有映射基线", component="platform")
        clean = {str(key): str(value) for key, value in groups.items()}
        if any(not key or not value for key, value in clean.items()):
            raise MappingError("逻辑门区或物理门区不能为空", component="platform")
        physical = {gate.group for gate in self.gates.by_station(station_id)}
        missing = sorted({value for value in clean.values() if value not in physical})
        if missing:
            raise MappingError(
                f"物理门区在站台 {station_id} 不存在: {','.join(missing)}", component="platform"
            )

    def _stop_position(self, station_id: str) -> int:
        """返回站台的停稳位置（取最近门区里程）。"""
        layout = self.layouts.get(station_id)
        zones = list(layout.zones) if layout else []
        nearest = nearest_zone(zones)
        return nearest.distance_m if nearest else 0

    def _primary_group(self, station_id: str) -> str:
        """返回站台的首个门区。"""

        keys = group_keys(self.mappings, station_id)
        return keys[0] if keys else ""

    def _primary_gate(self, station_id: str) -> PSD | None:
        """返回站台的首樘站台门。"""

        gates = self.gates.by_station(station_id)
        return gates[0] if gates else None

    def _adjacent_sample(self) -> dict:
        """给快照提供一个相邻关系样例。"""

        station_ids = self.namespace.station_ids()
        if not station_ids:
            return {}
        station_id = station_ids[0]
        keys = group_keys(self.mappings, station_id)
        if not keys:
            return {}
        return {keys[0]: adjacent(self.mappings, station_id, keys[0])}


def build_platform(
    config: PlatformConfig | None = None,
    clock: Clock | None = None,
    storage_root: str | None = None,
    unit: str | None = None,
) -> Platform:
    """构造一个平台；``storage_root`` 可覆盖配置里的存储目录。"""

    base = config or default_config()
    if storage_root is not None:
        base = with_storage_root(base, storage_root)
    return Platform(base, clock=clock, unit=unit)
