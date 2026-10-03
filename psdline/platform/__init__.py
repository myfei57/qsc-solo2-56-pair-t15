"""站台门区映射。

逻辑门区（上位机下发的 ``A`` / ``B``）与物理门区（现场编号）之间的对应关系随
布局更新而变化。改图走修订流程：提议落盘为不可变修订，第二人确认后等待安全
作业间隙生效，生效前可撤回；门控始终按生效指针解析，并可用相邻关系把同属一
个物理门区的逻辑键找出来。
"""

from __future__ import annotations

from .adjacency import adjacent, shared_zone
from .groups import group_keys, physical_groups, revision_of
from .mapping import LayoutProbe, Mapping, MappingRegistry, RevisionLog, resolve
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
    TERMINAL,
    WITHDRAWN,
    MappingRevision,
)

__all__ = [
    "ACTIVATING",
    "ACTIVE",
    "APPROVED",
    "IN_FLIGHT",
    "KIND_INSTALL",
    "KIND_RENUMBER",
    "KIND_ROLLBACK",
    "LayoutProbe",
    "Mapping",
    "MappingRegistry",
    "MappingRevision",
    "PROPOSED",
    "RevisionLog",
    "SUPERSEDED",
    "TERMINAL",
    "WITHDRAWN",
    "adjacent",
    "group_keys",
    "physical_groups",
    "resolve",
    "revision_of",
    "shared_zone",
]
