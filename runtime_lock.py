"""Hold an OS lock so two listeners cannot spend usage or post duplicate replies."""

import os
from pathlib import Path
import time


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self, timeout=0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self._acquire_once()
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def _acquire_once(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.handle.close()
            self.handle = None
            raise ValueError("Another bot or label-analysis process is already running. Stop it before starting another.") from error

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None
