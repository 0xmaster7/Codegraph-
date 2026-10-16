# Provenance and source coordinates

Every structural relationship published as evidence must have a deterministic edge ID and a provenance record. Provenance identifies repository/snapshot, normalized path, source content hash, absolute UTF-8 byte span, line/column coordinates, syntax node, extractor/version, rule, and premise edges for derived facts.

The parser stores byte offsets from tree-sitter and records structural, semantic, and derived provenance. Parser columns follow tree-sitter byte columns; byte offsets are the canonical coordinate. SCIP positions are converted from each document's declared encoding before joining. Do not compare SCIP character columns directly with these offsets.

Dirty analyzed source is copied into `.codegraph-cache/snapshots/<snapshot_id>/blobs/<sha256>` and referenced by a manifest. Snapshot identity includes repository identity, HEAD commit, source-manifest hash, and analysis configuration. The cache is local and ignored by Git.

Replay outcomes are `NOT_RUN`, `PASS`, `SOURCE_HASH_MISMATCH`, `ENVIRONMENT_MISMATCH`, `EXTRACTOR_VERSION_MISMATCH`, `EDGE_NOT_REPRODUCED`, `PREMISE_MISSING`, and `ERROR`. Semantic replay reruns SCIP over the complete materialized snapshot and requires the same environment fingerprint; a tiny-span parser replay cannot validate name resolution. `REPLAY_FAILED` remains a broad review/workflow label; it does not determine evidence eligibility by itself.

`PASS` confirms the evidence, and `NOT_RUN` preserves normal pre-replay use. `ENVIRONMENT_MISMATCH`, `EXTRACTOR_VERSION_MISMATCH`, `PREMISE_MISSING`, and `ERROR` are inconclusive: the original extraction remains eligible, while the specific replay outcome stays visible and must not be described as replay success. `ERROR` means replay could not complete; it is distinct from a completed deterministic disagreement.

`EDGE_NOT_REPRODUCED` and `SOURCE_HASH_MISMATCH` fail closed for the affected edge only. The historical edge and provenance remain inspectable, but that edge cannot support an active exact claim or strict evidence path. An edge failure does not invalidate unrelated evidence or the whole snapshot. Given identical immutable inputs and matching environment/extractor versions, complete premises, and a successful deterministic replay, an exact structural edge must reproduce. If it does not, this is an integrity/correctness failure, not expected nondeterminism.

The provenance inspector shows the replay outcome and current eligibility separately. A later authoritative `PASS` for the same edge/provenance identity replaces the stored replay state and restores eligibility; a transient or inconclusive retry cannot erase an existing fail-closed outcome. Re-indexing and republication can also create refreshed authoritative evidence under the existing snapshot identity rules. No historical record is deleted or silently rewritten.

`POST /api/evidence/{evidence_id}/replay` accepts a `snapshot_id` and resolves either an edge ID or provenance ID. It returns the replay scope/outcome and updates replay/review state in the local analysis cache and, when the snapshot is published, its FalkorDB provenance node. Unknown evidence IDs and replay/tool failures return `ERROR`; they are not deterministic mismatches.

Scopes are `FILE_OR_SPAN` for parser facts, `SNAPSHOT_SEMANTIC` for SCIP bindings, and `RULE_PREMISES` for derived inheritance/override/dispatch facts. A rule replay checks all recorded premise IDs before rerunning the deterministic derivation.
