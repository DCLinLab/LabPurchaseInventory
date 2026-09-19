"""Windows startup supervisor: keep the Slack listener alive without a Codex window."""

import ctypes
from ctypes import wintypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import time

from photo_intake import write_json
from runtime_lock import InstanceLock


ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".local"
LOG = logging.getLogger("labpurchase.supervisor")


def child_environment():
    env = os.environ.copy()
    # The reader discovers Codex even when the desktop app's PATH is absent.
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


class ChildJob:
    """Windows closes this handle on supervisor death and kills its child tree."""

    def __init__(self):
        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IOCount(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IOCount),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def rotate_at_launch(path):
    if path.exists() and path.stat().st_size >= 5 * 1024 * 1024:
        path.replace(path.with_suffix(path.suffix + ".previous"))


def main():
    if os.name != "nt":
        raise RuntimeError("This supervisor is for Windows Task Scheduler.")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(RUNTIME / "slack-supervisor.log", maxBytes=1024 * 1024,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    lock = InstanceLock(RUNTIME / "slack-supervisor.lock")
    try:
        lock.acquire()
    except ValueError:
        LOG.info("Another supervisor already owns this bot; exiting.")
        return 0
    try:
        (RUNTIME / "slack-supervisor.pid").write_text(str(os.getpid()), encoding="ascii")
        LOG.info("Supervisor started pid=%s", os.getpid())
        delay = 30
        while True:
            # Do not compete with an operator running a one-off analysis or bot.
            bot_lock = InstanceLock(RUNTIME / "slack-bot.lock")
            try:
                bot_lock.acquire()
            except ValueError:
                time.sleep(30)
                continue
            bot_lock.release()
            job, child = None, None
            started = time.monotonic()
            try:
                stdout_path, stderr_path = RUNTIME / "slack-bot.stdout.log", RUNTIME / "slack-bot.stderr.log"
                rotate_at_launch(stdout_path)
                rotate_at_launch(stderr_path)
                job = ChildJob()
                with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
                    child = subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "python.exe"),
                                              "-X", "utf8", "-u", str(ROOT / "slack_bot.py")],
                                             cwd=ROOT, env=child_environment(), stdin=subprocess.DEVNULL,
                                             stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
                    try:
                        job.assign(child)
                    except Exception:
                        child.kill()
                        child.wait()
                        raise
                    (RUNTIME / "slack-bot.pid").write_text(str(child.pid), encoding="ascii")
                    write_json(RUNTIME / "slack-supervisor-state.json", {
                        "status": "running", "supervisor_pid": os.getpid(),
                        "bot_pid": child.pid, "started_at": time.time(),
                    })
                    LOG.info("Bot started pid=%s", child.pid)
                    code = child.wait()
                LOG.warning("Bot exited pid=%s code=%s", child.pid, code)
            except Exception as error:
                LOG.error("Bot launch failed (%s).", type(error).__name__)
            finally:
                if job:
                    job.close()
            if time.monotonic() - started >= 300:
                delay = 30
            write_json(RUNTIME / "slack-supervisor-state.json", {
                "status": "retry_wait", "supervisor_pid": os.getpid(), "bot_pid": None,
                "retry_at": time.time() + delay,
            })
            LOG.info("Restarting in %s seconds", delay)
            time.sleep(delay)
            delay = min(delay * 2, 300)
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
