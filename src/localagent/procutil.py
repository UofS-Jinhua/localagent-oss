"""Subprocess flags shared by every child process we start, and what a task leaves running.

On Windows a process without a console of its own (the desktop window is started
with ``pythonw``) gives each child a brand-new console window. The UI polls git
every few seconds, so that means console windows flashing over the user's screen
forever. ``CREATE_NO_WINDOW`` prevents it, and costs nothing when a console
exists, so every call site passes these flags.
"""

import ctypes
import os
import signal
import subprocess
import sys
import time
from typing import Any

#: Hide the console of a child process (Windows only; 0 elsewhere).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def quiet(extra_flags: int = 0, **kwargs: Any) -> dict[str, Any]:
    """Keyword arguments for ``subprocess.run``/``Popen`` that keep children windowless.

    Args:
        extra_flags: Further Windows creation flags to combine, e.g.
            ``subprocess.CREATE_NEW_PROCESS_GROUP`` for a process tree we may kill.
    """
    if sys.platform == "win32":
        kwargs["creationflags"] = NO_WINDOW | extra_flags
    return kwargs


def new_group() -> dict[str, Any]:
    """Keyword arguments that start a child in its own process group, so ``kill_tree`` can end all of it."""
    if sys.platform == "win32":
        return quiet(subprocess.CREATE_NEW_PROCESS_GROUP)
    return {"start_new_session": True}


def kill_tree(process: subprocess.Popen) -> None:
    """Kill *process* and everything it started (it must have been started with ``new_group``)."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, **quiet())
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class Background:
    """What a task's commands started and left running, so the task can end them.

    A Bash call returns when bash exits, but whatever it launched with ``&`` or ``nohup`` keeps
    running -- deliberately, because the next step curls the dev server this one started. Nothing
    ended them afterwards, so a finished task left its server holding a port until the user noticed.
    Every command joins one of these; when the task is over, what is still alive is killed.

    On Windows the processes go into a job object, which owns every descendant however it was
    spawned. Elsewhere each command keeps the process group ``new_group`` gave it, and the count
    that ``stop`` returns is therefore of groups, not of single processes.
    """

    def __init__(self) -> None:
        self._groups: list[int] = []  # POSIX: one process-group id per command
        self._job: int | None = None  # Windows: the job object every command joins

    def adopt(self, process: subprocess.Popen) -> None:
        """Take responsibility for *process* and its descendants (it must come from ``new_group``)."""
        if sys.platform != "win32":
            self._groups.append(process.pid)  # start_new_session made it the group leader
            return
        if self._job is None:
            self._job = _kernel32().CreateJobObjectW(None, None) or None
        if self._job is not None:
            # A child started in the microseconds before this call escapes the job; bash has not
            # run its command by then, so what escapes is nothing.
            _kernel32().AssignProcessToJobObject(self._job, int(process._handle))

    def stop(self) -> int:
        """Kill everything still running and return how much was left (0 when the task cleaned up)."""
        if sys.platform == "win32":
            return self._stop_job()
        groups, self._groups = self._groups, []
        alive = [group for group in groups if _group_alive(group)]
        for group in alive:
            _signal_group(group, signal.SIGTERM)
        for _ in range(20):  # a server usually closes its port in well under a second
            if not any(_group_alive(group) for group in alive):
                return len(alive)
            time.sleep(0.1)
        for group in alive:
            _signal_group(group, signal.SIGKILL)
        return len(alive)

    def _stop_job(self) -> int:
        job, self._job = self._job, None
        if job is None:
            return 0
        kernel32 = _kernel32()
        info = _Accounting()
        alive = 0
        if kernel32.QueryInformationJobObject(job, _JOB_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None):
            alive = int(info.ActiveProcesses)
        if alive:
            kernel32.TerminateJobObject(job, 1)
        kernel32.CloseHandle(job)
        return alive


def _group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except OSError:
        return False
    return True


def _signal_group(group: int, sig: int) -> None:
    try:
        os.killpg(group, sig)
    except OSError:
        pass  # it ended between the check and the signal


_JOB_ACCOUNTING = 1  # JobObjectBasicAccountingInformation


class _Accounting(ctypes.Structure):
    """JOBOBJECT_BASIC_ACCOUNTING_INFORMATION; only ActiveProcesses is read."""

    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", ctypes.c_uint32), ("TotalProcesses", ctypes.c_uint32),
                ("ActiveProcesses", ctypes.c_uint32), ("TotalTerminatedProcesses", ctypes.c_uint32)]


def _kernel32() -> Any:
    """The Windows API calls used above, with return types the job handle needs."""
    global _KERNEL32
    if _KERNEL32 is None:
        _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _KERNEL32.CreateJobObjectW.restype = ctypes.c_void_p
        _KERNEL32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _KERNEL32.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                        ctypes.c_uint32, ctypes.c_void_p]
        _KERNEL32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        _KERNEL32.CloseHandle.argtypes = [ctypes.c_void_p]
    return _KERNEL32


_KERNEL32: Any = None
