"""A cross-process lock per repository.

KISS Sorcar uses ``fcntl.flock``, which does not exist on Windows, so there its
cross-process lock silently degrades to nothing. This lock uses
``msvcrt.locking`` on Windows and ``fcntl.flock`` elsewhere.
"""

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _try_lock(handle) -> bool:
    try:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path, timeout: float = 120) -> Iterator[None]:
    """Hold an exclusive lock on *path* (created if missing) for the block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        deadline = time.monotonic() + timeout
        while not _try_lock(handle):
            if time.monotonic() > deadline:
                raise TimeoutError(f"another LocalAgent process holds {path}")
            time.sleep(0.1)
        try:
            yield
        finally:
            _unlock(handle)
