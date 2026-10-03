"""站台门区映射。

逻辑门区（上位机下发的 ``A`` / ``B``）与物理门区（现场编号）之间的对应关系随
布局更新而变化。映射修改采用双人确认、作业间隙生效和生效前可撤回的流程；历史
修订只追加不可变事件，门控始终读取已激活版本。
"""

from __future__ import annotations

from .adjacency import adjacent, shared_zone
from .groups import group_keys, physical_groups, revision_of
from .mapping import (
    ACTIVE,
    APPROVED,
    PARTIAL,
    WITHDRAWN,
    Mapping,
    MappingRegistry,
    Proposal,
    RevisionLog,
    RevisionRecord,
    content_hash,
    resolve,
)

__all__ = [
    "ACTIVE",
    "APPROVED",
    "PARTIAL",
    "WITHDRAWN",
    "Mapping",
    "MappingRegistry",
    "Proposal",
    "RevisionLog",
    "RevisionRecord",
    "adjacent",
    "content_hash",
    "group_keys",
    "physical_groups",
    "resolve",
    "revision_of",
    "shared_zone",
]
