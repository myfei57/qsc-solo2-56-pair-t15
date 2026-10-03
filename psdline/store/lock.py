"""基于文件的跨进程互斥锁。

控制服务可能被控制台和现场守护进程同时启动。涉及一个站台的关键区段必须跨进程
串行：这里用 ``flock`` 保护锁文件，进程内再用可重入锁避免同线程嵌套自锁。
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - 当前交付目标是 Linux
    fcntl = None  # type: ignore[assignment]


class FileLock:
    """目录内一个具名资源的跨进程互斥锁。"""

    def __init__(self, root: str, name: str) -> None:
        self._root = os.path.abspath(root)
        os.makedirs(os.path.join(self._root, "locks"), exist_ok=True)
        safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)
        self._path = os.path.join(self._root, "locks", f"{safe}.lock")
        self._local = threading.RLock()
        self._depth = 0
        self._handle: int | None = None

    @contextmanager
    def locked(self) -> Iterator[None]:
        """获得锁；同进程同线程可重入。"""

        with self._local:
            if self._depth == 0:
                handle = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                self._handle = handle
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0 and self._handle is not None:
                    if fcntl is not None:
                        fcntl.flock(self._handle, fcntl.LOCK_UN)
                    os.close(self._handle)
                    self._handle = None
