from __future__ import annotations

import os
import time
from pathlib import Path
from typing import IO


class FileLock:
    def __init__(self, path: Path, blocking: bool = True, timeout_seconds: float | None = None):
        self.path = path
        self.blocking = blocking
        self.timeout_seconds = timeout_seconds
        self.handle: IO[str] | None = None
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds cannot be negative")

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        deadline = time.monotonic() + self.timeout_seconds if self.timeout_seconds is not None else None
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(" ")
                self.handle.flush()
            while True:
                self.handle.seek(0)
                mode = msvcrt.LK_NBLCK if (not self.blocking or deadline is not None) else msvcrt.LK_LOCK
                try:
                    msvcrt.locking(self.handle.fileno(), mode, 1)
                    break
                except OSError as exc:
                    if not self.blocking or (deadline is not None and time.monotonic() >= deadline):
                        self.handle.close()
                        self.handle = None
                        raise BlockingIOError(f"another Open Health Agent process holds {self.path}") from exc
                    time.sleep(0.1)
        else:
            import fcntl

            while True:
                flags = fcntl.LOCK_EX
                if not self.blocking or deadline is not None:
                    flags |= fcntl.LOCK_NB
                try:
                    fcntl.flock(self.handle.fileno(), flags)
                    break
                except BlockingIOError as exc:
                    if not self.blocking or (deadline is not None and time.monotonic() >= deadline):
                        self.handle.close()
                        self.handle = None
                        raise BlockingIOError(f"another Open Health Agent process holds {self.path}") from exc
                    time.sleep(0.1)
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.handle is None:
            return
        if os.name == "nt":
            import msvcrt

            try:
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None
