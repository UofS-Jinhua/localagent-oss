"""Per-provider concurrency limits.

A single local GPU gains no throughput from extra parallel requests and each
extra request can evict another's prompt cache, so requests beyond a
provider's ``max_concurrency`` wait here instead of queueing on the server.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_lock = threading.Lock()
_semaphores: dict[tuple[str, int], threading.BoundedSemaphore] = {}


def _semaphore(provider: str, limit: int) -> threading.BoundedSemaphore:
    with _lock:
        key = (provider, limit)
        if key not in _semaphores:
            _semaphores[key] = threading.BoundedSemaphore(limit)
        return _semaphores[key]


@contextmanager
def provider_slot(provider: str, limit: int, on_wait=None) -> Iterator[None]:
    """Hold one of *provider*'s request slots for the duration of the block.

    Args:
        provider: Provider name.
        limit: The provider's ``max_concurrency``.
        on_wait: Called once if the slot is not immediately available.
    """
    sem = _semaphore(provider, limit)
    if not sem.acquire(blocking=False):
        if on_wait is not None:
            on_wait()
        sem.acquire()
    try:
        yield
    finally:
        sem.release()
