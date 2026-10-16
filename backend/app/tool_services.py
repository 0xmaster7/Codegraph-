from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .config import Settings, get_settings
from .graph import GraphStore
from .graph_tools import DeterministicGraphTools
from .ingestion import load_snapshot_analysis
from .models import SnapshotAnalysis, SnapshotStatus

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")


@dataclass(frozen=True)
class SnapshotToolContext:
    """One READY analysis and its shared deterministic query service."""

    analysis: SnapshotAnalysis
    tools: DeterministicGraphTools


def load_snapshot_tool_context(
    snapshot_id: str,
    *,
    settings: Settings | None = None,
    store_factory: Callable[[Settings], GraphStore] = GraphStore,
) -> SnapshotToolContext:
    """Load a snapshot only after confirming it is ready for query operations."""
    if not _ID_RE.fullmatch(snapshot_id):
        raise ValueError("Malformed snapshot ID")
    active_settings = settings or get_settings()
    store = store_factory(active_settings)
    status = store.snapshot_status(snapshot_id)
    if status is None:
        raise LookupError("Snapshot not found")
    if status != SnapshotStatus.READY.value:
        raise ValueError("Investigation tools require a READY snapshot")
    analysis = load_snapshot_analysis(snapshot_id, active_settings)
    if analysis.snapshot_id != snapshot_id:
        raise ValueError("Snapshot analysis identity does not match the request")
    ready_analysis = analysis.model_copy(update={"status": SnapshotStatus.READY})
    tools = DeterministicGraphTools.from_store(ready_analysis, store)
    return SnapshotToolContext(analysis=ready_analysis, tools=tools)
