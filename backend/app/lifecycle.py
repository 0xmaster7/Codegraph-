from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_LOCK = threading.RLock()
_ACTIVE_SNAPSHOTS: set[str] = set()


@contextmanager
def snapshot_lifecycle_lock() -> Iterator[None]:
    """Serialize publication, job registration, and snapshot collection in this process."""
    with _LOCK:
        yield


def register_active_snapshot(snapshot_id: str) -> None:
    with _LOCK:
        _ACTIVE_SNAPSHOTS.add(snapshot_id)


def unregister_active_snapshot(snapshot_id: str) -> None:
    with _LOCK:
        _ACTIVE_SNAPSHOTS.discard(snapshot_id)


def active_snapshot_ids() -> frozenset[str]:
    with _LOCK:
        return frozenset(_ACTIVE_SNAPSHOTS)
