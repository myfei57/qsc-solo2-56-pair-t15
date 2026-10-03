"""站台作业间隙与跨进程临界区。"""

from __future__ import annotations

import threading

import pytest

from psdline.errors import InterlockError
from psdline.gap import WorkGapRegistry
from psdline.store import FileLock, JsonStore


def test_work_gap_enter_exit_and_next_clear_epoch(tmp_path):
    gaps = WorkGapRegistry(JsonStore(str(tmp_path)))
    assert gaps.get("ST1").state == "clear"
    entered = gaps.enter("ST1", "T01", at=1.0)
    assert entered.state == "occupied" and entered.service_seq == 1
    assert gaps.clear_epoch("ST1") is None
    assert gaps.active_service("ST1", "T01") == 1
    with pytest.raises(InterlockError):
        gaps.enter("ST1", "T02", at=2.0)
    exited = gaps.exit("ST1", "T01", at=3.0)
    assert exited.state == "clear" and exited.epoch == 2
    marked = gaps.mark_clear("ST1", at=4.0)
    assert marked.epoch == 3


def test_work_gap_rejects_exit_by_other_train(tmp_path):
    gaps = WorkGapRegistry(JsonStore(str(tmp_path)))
    gaps.enter("ST1", "T01", at=1.0)
    with pytest.raises(InterlockError):
        gaps.exit("ST1", "T02", at=2.0)


def test_file_lock_serializes_in_process(tmp_path):
    lock = FileLock(str(tmp_path), "resource")
    order: list[str] = []

    def first() -> None:
        with lock.locked():
            order.append("first")

    thread = threading.Thread(target=first)
    with lock.locked():
        thread.start()
        order.append("holder")
    thread.join()
    assert order == ["holder", "first"]
