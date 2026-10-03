"""门区映射修订流程：两人确认、间隙生效、撤回、回滚与恢复。"""

from __future__ import annotations

import threading

import pytest

from psdline.app import build_platform
from psdline.config import default_config
from psdline.door import active_group, active_group_revision, open_group
from psdline.errors import MappingError

ROTATED = {"G01": "G02", "G02": "G01", "G03": "G03", "G04": "G04"}


def _rebuild(platform):
    """在同一存储目录上重建平台（模拟重启）。"""

    return build_platform(
        default_config(), clock=platform.clock, storage_root=platform.store.root
    )


def _propose_and_confirm(platform, groups=None, actor_a="planner", actor_b="reviewer"):
    proposed = platform.propose_mapping("ST1", groups or {"A": "G02", "B": "G03"}, actor=actor_a)
    platform.confirm_mapping("ST1", proposed["revision"], actor=actor_b)
    return proposed["revision"]


def _free_station(platform, train_id="T01"):
    """把站台带回可换图的作业间隙：发车、清扫。"""

    platform.check_gap(train_id)
    platform.depart(train_id, "ST1")
    platform.sweep("ST1")


def test_propose_validates_groups_and_actor(platform):
    with pytest.raises(MappingError):
        platform.propose_mapping("ST1", {}, actor="planner")
    with pytest.raises(MappingError):
        platform.propose_mapping("ST1", {"A": "G01"}, actor="")
    with pytest.raises(MappingError):
        platform.propose_mapping("ST1", {"A": "G99"}, actor="planner")  # 物理门区不在布局
    with pytest.raises(MappingError):
        platform.propose_mapping("ST9", {"A": "G01"}, actor="planner")  # 站台没有映射
    proposed = platform.propose_mapping("ST1", {"A": "G02"}, actor="planner")
    assert proposed["revision"] == 2
    assert proposed["base_revision"] == 1
    assert platform.mappings.revision("ST1") == 1  # 提议不生效


def test_propose_rejects_noop_change(platform):
    identity = {f"G0{index}": f"G0{index}" for index in range(1, 5)}
    with pytest.raises(MappingError):
        platform.propose_mapping("ST1", identity, actor="planner")  # 与生效版本一致


def test_two_person_rule(platform):
    platform.propose_mapping("ST1", {"A": "G02"}, actor="planner")
    with pytest.raises(MappingError):
        platform.confirm_mapping("ST1", 2, actor="planner")  # 提议人不能自确认
    confirmed = platform.confirm_mapping("ST1", 2, actor="reviewer")
    assert confirmed["state"] == "approved"
    with pytest.raises(MappingError):
        platform.confirm_mapping("ST1", 2, actor="third")  # 已确认不能重复确认
    with pytest.raises(MappingError):
        platform.confirm_mapping("ST1", 99, actor="reviewer")  # 修订不存在
    assert platform.mappings.revision("ST1") == 1  # 确认也不生效


def test_single_in_flight_proposal(platform):
    platform.propose_mapping("ST1", {"A": "G02"}, actor="planner")
    with pytest.raises(MappingError):
        platform.propose_mapping("ST1", {"A": "G03"}, actor="other")
    platform.withdraw_mapping("ST1", 2, actor="planner")
    again = platform.propose_mapping("ST1", {"A": "G03"}, actor="other")
    assert again["revision"] == 3  # 撤回的修订号不复用


def test_activation_waits_for_safe_gap(platform):
    _propose_and_confirm(platform)
    platform.arrive("T01", "ST1", 0)
    report = platform.cycle()
    assert report["mapping_activated"] == []
    assert report["mapping_deferred"][0]["revision"] == 2
    assert platform.mappings.revision("ST1") == 1  # 占用期间不生效
    _free_station(platform)
    report = platform.cycle()
    assert report["mapping_activated"] == [
        {"station_id": "ST1", "revision": 2, "previous_revision": 1}
    ]
    assert platform.mappings.revision("ST1") == 2
    assert platform.mappings.resolve("ST1", "A") == "G02"


def test_window_reports_blockers(platform):
    assert platform.mapping_window("ST1")["safe"] is True
    platform.arrive("T01", "ST1", 0)
    window = platform.mapping_window("ST1")
    assert window["safe"] is False
    assert "站台有列车对位未发车" in window["reasons"]
    assert "站台门未全部关闭" in window["reasons"]
    gate = platform.gates.by_station("ST1")[0]
    platform.set_fault(gate.id)
    assert "站台门处于联锁锁定" in platform.mapping_window("ST1")["reasons"]


def test_manual_activate_checks_time_and_window(platform, clock):
    platform.propose_mapping("ST1", {"A": "G02"}, actor="planner", not_before=clock.now() + 100)
    platform.confirm_mapping("ST1", 2, actor="reviewer")
    early = platform.activate_mapping("ST1")
    assert early["ok"] is False and "未到生效时刻" in early["violations"][0]
    clock.advance(101)
    platform.arrive("T01", "ST1", 0)
    blocked = platform.activate_mapping("ST1")
    assert blocked["ok"] is False and blocked["violations"]
    _free_station(platform)
    done = platform.activate_mapping("ST1", actor="supervisor")
    assert done["ok"] is True and done["previous_revision"] == 1
    assert platform.mappings.revision("ST1") == 2


def test_withdraw_keeps_old_version(platform):
    _propose_and_confirm(platform)
    result = platform.withdraw_mapping("ST1", 2, actor="planner")
    assert result["active_revision"] == 1
    assert platform.mappings.pending("ST1") is None
    assert platform.mappings.resolve("ST1", "G01") == "G01"  # 旧版本继续可用
    platform.cycle()
    assert platform.mappings.revision("ST1") == 1  # 撤回的修订不会生效
    with pytest.raises(MappingError):
        platform.withdraw_mapping("ST1", 2, actor="planner")  # 已撤回不能重复撤回
    doc = platform.mappings.revision_doc("ST1", 2)
    assert doc.state == "withdrawn" and doc.groups == {"A": "G02", "B": "G03"}  # 内容留痕


def test_withdraw_after_activation_rejected(platform):
    _propose_and_confirm(platform)
    platform.activate_mapping("ST1")
    assert platform.mappings.revision("ST1") == 2
    with pytest.raises(MappingError):
        platform.withdraw_mapping("ST1", 2, actor="planner")


def test_rollback_uses_same_pipeline(platform):
    _propose_and_confirm(platform, groups=ROTATED)
    platform.activate_mapping("ST1")
    assert platform.mappings.resolve("ST1", "G01") == "G02"
    with pytest.raises(MappingError):
        platform.rollback_mapping("ST1", 2, actor="planner")  # 正在生效无需回滚
    with pytest.raises(MappingError):
        platform.rollback_mapping("ST1", 99, actor="planner")  # 目标不存在
    proposed = platform.rollback_mapping("ST1", 1, actor="planner")
    assert proposed["revision"] == 3 and proposed["target_revision"] == 1
    platform.confirm_mapping("ST1", 3, actor="reviewer")
    result = platform.activate_mapping("ST1")
    assert result["ok"] is True
    assert platform.mappings.revision("ST1") == 3  # 修订号单调向前
    assert platform.mappings.resolve("ST1", "G01") == "G01"  # 内容回到修订1
    history = platform.mappings.history("ST1").as_dict()["revisions"]
    assert history == [1, 2, 3]


def test_activation_and_rollback_never_open_doors(platform):
    platform.arrive("T01", "ST1", 0)
    _free_station(platform)
    _propose_and_confirm(platform, groups=ROTATED)
    calls = []
    original = platform.gates.set_open

    def counting(gate_id, state):
        calls.append(gate_id)
        return original(gate_id, state)

    platform.gates.set_open = counting
    try:
        platform.cycle()  # 生效
        platform.rollback_mapping("ST1", 1, actor="planner")
        platform.confirm_mapping("ST1", 3, actor="reviewer")
        platform.cycle()  # 回滚生效
    finally:
        platform.gates.set_open = original
    assert calls == []  # 生效与回滚都不碰门
    assert platform.mappings.revision("ST1") == 3


def test_open_group_replay_is_idempotent(platform):
    first = open_group(platform.doors, platform.mappings, "ST1", "G01")
    assert first["replayed"] is False
    assert first["revision"] == 1
    calls = []
    original = platform.gates.set_open

    def counting(gate_id, state):
        calls.append(gate_id)
        return original(gate_id, state)

    platform.gates.set_open = counting
    try:
        again = open_group(platform.doors, platform.mappings, "ST1", "G01")
    finally:
        platform.gates.set_open = original
    assert again["replayed"] is True
    assert calls == []  # 重放不重复开门


def test_sweep_resets_linkage(platform):
    open_group(platform.doors, platform.mappings, "ST1", "G01")
    platform.sweep("ST1")
    assert active_group(platform.doors, "ST1") == ""
    again = open_group(platform.doors, platform.mappings, "ST1", "G01")
    assert again["replayed"] is False  # 清扫后是新的联动，不是重放


def test_door_open_records_active_revision(platform):
    platform.arrive("T01", "ST1", 0)
    entries = [e for e in platform.audit.list() if e.kind == "door_group"]
    assert entries[-1].detail["revision"] == 1
    _free_station(platform)
    _propose_and_confirm(platform, groups=ROTATED)
    platform.cycle()
    platform.arrive("T02", "ST1", 0)
    entries = [e for e in platform.audit.list() if e.kind == "door_group"]
    assert entries[-1].detail["revision"] == 2
    assert entries[-1].detail["replayed"] is False
    assert active_group_revision(platform.doors, "ST1") == 2


def test_audit_trail_covers_lifecycle(platform):
    platform.propose_mapping("ST1", {"A": "G02"}, actor="planner")
    platform.confirm_mapping("ST1", 2, actor="reviewer")
    platform.activate_mapping("ST1", actor="supervisor")
    platform.rollback_mapping("ST1", 1, actor="planner")
    platform.withdraw_mapping("ST1", 3, actor="planner")
    kinds = [entry.kind for entry in platform.audit.list()]
    for expected in (
        "mapping_proposed",
        "mapping_confirmed",
        "mapping_activated",
        "mapping_rollback_proposed",
        "mapping_withdrawn",
    ):
        assert expected in kinds
    activated = [e for e in platform.audit.list() if e.kind == "mapping_activated"][0]
    assert activated.detail["previous_revision"] == 1
    assert activated.detail["revision"] == 2
    assert activated.detail["activation_id"]


def test_stale_base_safety_net(platform):
    _propose_and_confirm(platform)
    # 确认之后指针被外部推进（并发时序安全网）：生效必须被拦下
    platform.store.write(
        "platform/ST1", {"station_id": "ST1", "groups": {"X": "G01"}, "revision": 7}
    )
    with pytest.raises(MappingError):
        platform.mappings.activate("ST1")
    assert platform.mappings.revision_doc("ST1", 2).state == "superseded"
    assert platform.mappings.revision("ST1") == 7  # 指针不被回退


def test_activation_compensates_on_store_failure(platform):
    _propose_and_confirm(platform)
    original = platform.store.write
    state = {"failed": False}

    def flaky(doc_id, payload):
        if doc_id == "platform/ST1" and not state["failed"]:
            state["failed"] = True
            raise OSError("disk full")
        return original(doc_id, payload)

    platform.store.write = flaky
    try:
        with pytest.raises(OSError):
            platform.mappings.activate("ST1")
    finally:
        platform.store.write = original
    assert platform.mappings.revision("ST1") == 1  # 指针保持旧版本
    assert platform.mappings.revision_doc("ST1", 2).state == "approved"  # 可重试
    revision, _previous = platform.mappings.activate("ST1")
    assert revision.revision == 2
    assert platform.mappings.revision("ST1") == 2


def test_recover_finalizes_committed_activation(platform):
    _propose_and_confirm(platform)
    # 重演生效序列并在提交点后"掉电"：意图与指针已写，收尾未写
    doc = platform.mappings.revision_doc("ST1", 2)
    platform.store.write(
        "platform/ST1/rev/00002",
        doc.transition(state="activating", activation_id="crash-1").as_dict(),
    )
    platform.store.write(
        "platform/ST1",
        {
            "station_id": "ST1",
            "groups": {"A": "G02", "B": "G03"},
            "revision": 2,
            "activated_from": 1,
            "activation_id": "crash-1",
        },
    )
    restarted = _rebuild(platform)
    assert restarted.mappings.revision("ST1") == 2
    assert restarted.mappings.revision_doc("ST1", 2).state == "active"
    kinds = [entry.kind for entry in restarted.audit.list()]
    assert "mapping_activation_repaired" in kinds


def test_recover_reverts_uncommitted_activation(platform):
    _propose_and_confirm(platform)
    # 只写了意图就"掉电"：指针仍是修订1，未越过提交点
    doc = platform.mappings.revision_doc("ST1", 2)
    platform.store.write(
        "platform/ST1/rev/00002",
        doc.transition(state="activating", activation_id="crash-2").as_dict(),
    )
    restarted = _rebuild(platform)
    assert restarted.mappings.revision("ST1") == 1
    reverted = restarted.mappings.revision_doc("ST1", 2)
    assert reverted.state == "approved" and reverted.activation_id == ""
    restarted.activate_mapping("ST1")  # 退回已确认后可以重新生效
    assert restarted.mappings.revision("ST1") == 2


def test_history_is_immutable_and_persistent(platform):
    _propose_and_confirm(platform, groups=ROTATED)
    platform.activate_mapping("ST1")
    platform.rollback_mapping("ST1", 1, actor="planner")
    platform.confirm_mapping("ST1", 3, actor="reviewer")
    platform.activate_mapping("ST1")
    assert platform.mappings.revision_doc("ST1", 2).groups == ROTATED  # 历史内容不变
    assert platform.mappings.revision_doc("ST1", 2).state == "superseded"
    restarted = _rebuild(platform)
    assert restarted.mappings.history("ST1").as_dict()["revisions"] == [1, 2, 3]
    assert restarted.mappings.revision("ST1") == 3
    assert restarted.mappings.resolve("ST1", "G01") == "G01"


def test_concurrent_proposes_serialize(platform):
    outcomes = []
    barrier = threading.Barrier(4)
    lock = threading.Lock()

    def worker(index):
        try:
            barrier.wait(timeout=5)
            platform.propose_mapping("ST1", {f"K{index}": "G02"}, actor=f"actor{index}")
            with lock:
                outcomes.append("ok")
        except MappingError:
            with lock:
                outcomes.append("err")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 1
    assert outcomes.count("err") == 3


def test_console_mapping_flow(platform):
    from psdline.console.api import Api

    api = Api(platform)
    status, _payload = api.handle(
        "POST", "/api/renumber", {}, {"station_id": "ST1", "groups": {"A": "G02"}}
    )
    assert status == 400  # 缺少提议人
    status, payload = api.handle(
        "POST",
        "/api/renumber",
        {},
        {"station_id": "ST1", "groups": {"A": "G02"}, "actor": "planner"},
    )
    assert status == 200 and payload["revision"] == 2
    status, _payload = api.handle(
        "POST", "/api/mapping/confirm", {}, {"station_id": "ST1", "revision": 2, "actor": "planner"}
    )
    assert status == 409  # 自确认被两人规则拦下
    status, payload = api.handle(
        "POST", "/api/mapping/confirm", {}, {"station_id": "ST1", "revision": 2, "actor": "reviewer"}
    )
    assert status == 200 and payload["state"] == "approved"
    status, payload = api.handle("GET", "/api/mapping/ST1/revisions", {}, {})
    assert status == 200
    assert payload["active_revision"] == 1
    assert payload["pending"]["state"] == "approved"
    status, payload = api.handle("POST", "/api/mapping/activate", {}, {"station_id": "ST1"})
    assert status == 200 and payload["ok"] is True
    status, payload = api.handle("GET", "/api/mapping/ST1", {}, {})
    assert payload["revision"] == 2
    status, payload = api.handle(
        "POST", "/api/mapping/rollback", {}, {"station_id": "ST1", "target_revision": 1, "actor": "planner"}
    )
    assert status == 200 and payload["revision"] == 3
    status, payload = api.handle(
        "POST", "/api/mapping/withdraw", {}, {"station_id": "ST1", "revision": 3, "actor": "planner"}
    )
    assert status == 200 and payload["active_revision"] == 2
