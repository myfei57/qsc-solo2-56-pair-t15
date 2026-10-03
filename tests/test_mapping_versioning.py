"""门区映射版本化、双人确认和不可变历史。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from psdline.clock import ManualClock
from psdline.errors import MappingError
from psdline.platform import MappingRegistry, content_hash
from psdline.store import JsonStore


def _registry(tmp_path):
    store = JsonStore(str(tmp_path))
    clock = ManualClock()
    return MappingRegistry(store, None, clock), store, clock


def test_bootstrap_propose_approve_wait_next_gap_and_activate(tmp_path):
    mappings, _store, clock = _registry(tmp_path)
    mappings.bootstrap_initial("ST1", {"G01": "G01", "G02": "G02"})
    proposal = mappings.propose("ST1", {"A": "G02"}, "op1", at=clock.now())
    assert proposal.revision == 2
    assert mappings.revision("ST1") == 1
    mappings.approve("ST1", 2, "r1")
    with pytest.raises(MappingError):
        mappings.approve("ST1", 2, "op1")
    approved = mappings.approve("ST1", 2, "r2", ready_after_epoch=0)
    assert approved.status == "approved"
    with pytest.raises(MappingError):
        mappings.activate("ST1", 2, current_gap_epoch=0)
    activated = mappings.activate("ST1", 2, current_gap_epoch=2)
    assert activated.revision == 2
    assert mappings.resolve("ST1", "A") == "G02"


def test_withdraw_keeps_old_version_and_history(tmp_path):
    mappings, _store, _clock = _registry(tmp_path)
    mappings.bootstrap_initial("ST1", {"G01": "G01"})
    revision = mappings.propose("ST1", {"A": "G01"}, "op").revision
    mappings.approve(revision=revision, station_id="ST1", approver="r1")
    mappings.withdraw("ST1", revision, "op2")
    assert mappings.revision("ST1") == 1
    assert mappings.history("ST1").revisions == [1, 2]
    with pytest.raises(MappingError):
        mappings.withdraw("ST1", revision, "op2")


def test_concurrent_proposal_and_base_revision_are_rejected(tmp_path):
    mappings, _store, _clock = _registry(tmp_path)
    mappings.bootstrap_initial("ST1", {"G01": "G01"})
    mappings.propose("ST1", {"A": "G01"}, "op1")
    with pytest.raises(MappingError):
        mappings.propose("ST1", {"B": "G01"}, "op2")
    with pytest.raises(MappingError):
        mappings.propose("ST1", {"A": "G01"}, "op3", base_revision=99)


def test_immutable_event_chain_is_validated(tmp_path):
    mappings, store, _clock = _registry(tmp_path)
    mappings.bootstrap_initial("ST1", {"G01": "G01"})
    mappings.propose("ST1", {"A": "G01"}, "op")
    journal_path = next(Path(store.root).glob("mapping-ST1-*.jsonl"))
    records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    records[0]["submitted_by"] = "tampered"
    journal_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
        encoding="utf-8",
    )
    # JsonlJournal 缓存了计数，重新构造一个登记表强制重放。
    rebuilt = MappingRegistry(store, None, ManualClock())
    with pytest.raises(MappingError):
        rebuilt.current("ST1")


def test_content_hash_is_stable_for_key_order(tmp_path):
    first = content_hash({"A": "G01", "B": "G02"})
    second = content_hash({"B": "G02", "A": "G01"})
    assert first == second
