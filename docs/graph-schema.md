# Graph schema contract

## Node kinds

Canonical V1 static nodes: `RepositorySnapshot`, `File`, `Module`, `Class`, `Function`, `Method`, `ReferenceSite`, `CallSite`, `ExternalSymbol`, `PackageDeclaration`, `PackageDistribution`, `Chunk`, `EntryPoint`, `ProvenanceRecord`, and `Diagnostic`.

SCIP resolver-known builtins and standard-library references are persisted as `ExternalSymbol` nodes with `external_kind` set to `BUILTIN` or `STDLIB` and their module name recorded. Other SCIP external records retain `UNKNOWN_EXTERNAL` unless their symbol namespace identifies a package. The analysis environment and distribution name/version fields are stored when available. A failed SCIP run reports failed semantic indexing and emits no exact bindings.

Repository metadata nodes: `Developer`, `Commit`, `ChangedPath`, `Investigation`, and ingestion/evaluation records. Runtime graph nodes: `RuntimeRun`, `TestDefinition`, `TestInvocation`, `RuntimeFunctionEntry`, and `RuntimeCallObservation`.

## Canonical relationships

```text
CONTAINS              RepositorySnapshot → File
DECLARES_MODULE       File → Module
DEFINES               File/Module/Class → Class/Function/Method
DEFINES_METHOD        Class → Method
OCCURS_IN             ReferenceSite/CallSite → Function/Method/Module/Class
BINDS_TO              ReferenceSite → in-repository symbol or ExternalSymbol
MAY_DISPATCH_TO       CallSite → Function/Method/Class/ExternalSymbol
INHERITS              Class → Class/ExternalSymbol
OVERRIDES             Method → Method (deterministic derivation)
IMPORTS               Module → Module/ExternalSymbol
REFERENCES            execution context → symbol
DESCRIBES             Chunk → source node
```

Source nodes carry the common `SourceEntity` label and their typed label (`File`, `Module`, `Class`, `Function`, or `Method`). Each source node stores `identifier_raw` and a lowercase, snake/camel-split `identifier_search_text`; a FalkorDB full-text index supports token retrieval after exact and normalized property lookup. Text matches remain retrieval evidence and never create structural relationships. Module declarations use `DECLARES_MODULE`; class, function, and method definitions use `DEFINES`, while class-to-method definitions use `DEFINES_METHOD`. Snapshot loading validates node kinds, endpoints, bindings, and provenance before publication. Stable IDs make retries idempotent, and graph indexes are created only when absent.

Publication keeps the metadata snapshot unavailable as `READY` while the per-snapshot graph, repository history, dependency records, and count checks are written. The per-snapshot graph is marked `READY` first; the metadata snapshot status is the final visibility gate. Only `READY` snapshots can serve graph queries. A load error marks both statuses `FAILED`, so partially written data cannot be treated as a ready snapshot. A subsequent ingestion can retry the same snapshot.

Retention keeps the newest five `READY` snapshots per repository. Older snapshots remain when `manually_pinned = true` or when any investigation, evaluation report, demo fixture, or other pin record has a `PINS` relationship to the snapshot. `POST /api/snapshots/{snapshot_id}/pin` sets the manual pin using `{"pinned": true}` or clears it with `false`. GC marks candidates `GC_PENDING`, drops their immutable snapshot graph, then removes the metadata record; a later run resumes any interrupted cleanup. `FAILED` and `CANCELLED` snapshots become eligible after `FAILED_SNAPSHOT_GC_GRACE_SECONDS`. In-progress statuses and snapshots referenced by active ingestion jobs are never collected. V1 serializes job registration, publish, and GC within the single backend process.

Edge identity includes graph scope, relation, source ID, target ID, and occurrence key; parallel source occurrences must remain distinct.

`INHERITS`, `OVERRIDES`, and `MAY_DISPATCH_TO` are stored as derived edges with a rule ID and premise edge IDs. Their provenance records retain the source span and premise IDs. Dispatch state is stored on each call site separately from exact SCIP binding. C3 candidates are emitted only when the receiver type and local hierarchy are supported; unresolved bases and dynamic metaclasses produce `UNKNOWN` without candidates.

Each persisted provenance record stores repository, snapshot, and analysis-environment identity; source path and content hash; exact byte span plus line/column coordinates and coordinate encoding; extractor/version and rule; SCIP document/symbol/position encoding when applicable; premises for derived facts; and creation time. It also stores replay scope, last replay outcome, and review state. `REPLAY_FAILED` is a broad review label, while `replay_state` controls whether exact evidence is eligible: only `EDGE_NOT_REPRODUCED` and `SOURCE_HASH_MISMATCH` suppress the affected edge from exact claims and strict paths. Environment/version/premise/tool failures remain inconclusive and preserve original eligibility. Historical records remain available for inspection, and unrelated edges are unaffected.

## State dimensions

Binding: `EXACT`, `UNRESOLVED`.

Dispatch: `SINGLE_CANDIDATE`, `MULTI_CANDIDATE`, `UNKNOWN`, `NOT_APPLICABLE`.

Review: `NONE`, `RESOLVER_DISAGREEMENT`, `REPLAY_FAILED`, `RUNTIME_MISMATCH`, `SCHEMA_WARNING`, `HUMAN_REVIEW_REQUIRED`.

These dimensions are independent. A syntax-only site has no binding state until semantic indexing runs.

## Evidence taxonomy

The single taxonomy is `STATIC_STRUCTURE`, `STATIC_BINDING`, `STATIC_DERIVATION`, `RUNTIME_OBSERVATION`, `GIT_HISTORY`, `DEPENDENCY_METADATA`, `LEXICAL_PASSAGE`, `SEMANTIC_PASSAGE`, `UNRESOLVED_DIAGNOSTIC`, and `USER_CONTEXT`.

M16 normalizes evidence into records with `evidence_id`, canonical `type`, `snapshot_id`,
`source_record_id`, `display_label`, and `provenance_pointer`. Passage fusion uses RRF with `k=60`
at `chunk_id`, merges a lexical and vector hit for the same immutable chunk into one passage with
both retrieval signals, then aggregates its fused score by canonical `source_node_id`. Exact entity
IDs remain mandatory graph seeds outside the RRF pool. Passage records never contain a graph path;
verified graph paths are a separate result type.

Consistency review is limited to comparable facts: resolver disagreement at the same source
occurrence, snapshot source-hash mismatch, conflicting passage identity/provenance, and a runtime
target outside its call site's known static candidates. A manifest declaration and lock resolution
are different dependency evidence; an unobserved static candidate is not disproven; and a semantic
binding to a base implementation can coexist with runtime dispatch to a compatible derived
candidate. These cases do not create conflicts. RRF values are deterministic ranking signals, not
calibrated probabilities.

M17's deterministic graph tools are implemented in a shared snapshot-scoped service;
their path ordering, strict/conservative modes, unresolved barriers, and passage
separation are documented in [graph-tools.md](graph-tools.md). Tool output does not
create graph relationships.
