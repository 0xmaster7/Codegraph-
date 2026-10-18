"""Read-only replay prerequisites, without re-running analysis or granting execution."""

from .config import Settings
from .environment import capture_environment
from .models import SnapshotAnalysis
from .scip import scip_toolchain


def replay_availability(
    analysis: SnapshotAnalysis, evidence_id: str, settings: Settings
) -> dict[str, object]:
    record = next(
        (
            record
            for record in analysis.provenance
            if evidence_id in {record.edge_id, record.provenance_id}
        ),
        None,
    )
    if record is None:
        return {
            "available": False,
            "reason": "No replayable evidence is attached to this selection.",
        }
    root = settings.snapshot_cache_dir.expanduser().resolve() / analysis.snapshot_id
    entries = [entry for entry in analysis.manifest if entry.analyzed]
    if not entries or any(
        len(entry.blob_hash) != 64
        or any(char not in "0123456789abcdef" for char in entry.blob_hash)
        or not (root / "blobs" / entry.blob_hash).is_file()
        for entry in entries
    ):
        return {"available": False, "reason": "Saved source files are missing from this snapshot."}
    if record.extractor_name == "scip-python":
        if analysis.analysis_environment is None:
            return {
                "available": False,
                "reason": "The original analysis environment was not recorded.",
            }
        python_bin, cli_bin, python_version, cli_version = scip_toolchain(settings)
        if not python_bin or not cli_bin:
            return {
                "available": False,
                "reason": "The code indexing tools are not configured on this server.",
            }
        current = capture_environment(
            analysis.analysis_environment.source_manifest_hash, python_version, cli_version
        )
        if current.environment_id != analysis.analysis_environment.environment_id:
            return {
                "available": False,
                "reason": "The current analysis environment differs from the saved version.",
            }
    return {
        "available": True,
        "reason": (
            "Prerequisites are present. A recheck may still be inconclusive; "
            "it does not run your application."
        ),
    }
