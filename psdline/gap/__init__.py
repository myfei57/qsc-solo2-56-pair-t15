"""间隙探测与作业间隙。

发车前必须先确认站台与列车之间没有异物：探测结果按列车保存，"确认"与"可疑"
两种取值都要保留下来，供审计与联锁互查。映射切换使用站台级作业间隙，避免在
一轮到发作业中途改变开门解释。
"""

from __future__ import annotations

from .gap import BLOCKED as GAP_BLOCKED
from .gap import CLEAR as GAP_CLEAR
from .gap import GapRegistry, ProbeResult
from .status import gap_status, gap_summary
from .workgap import CLEAR as WORK_CLEAR
from .workgap import OCCUPIED, WorkGap, WorkGapRegistry

__all__ = [
    "GAP_BLOCKED",
    "GAP_CLEAR",
    "GapRegistry",
    "OCCUPIED",
    "ProbeResult",
    "WORK_CLEAR",
    "WorkGap",
    "WorkGapRegistry",
    "gap_status",
    "gap_summary",
]
