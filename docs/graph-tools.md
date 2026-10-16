# Deterministic graph tools

`backend/app/graph_tools.py` provides the M17 deterministic service layer over one
immutable `READY` snapshot. `DeterministicGraphTools.from_store` binds it to that
snapshot's FalkorDB graph, immutable source blobs, replay service, local ONNX query
embeddings, and invocation-scoped runtime observations. Every request carries the
snapshot ID; using an ID from another snapshot is rejected.

The service implements symbol/source lookup, reference and call-site queries,
callers/callees, importers, inheritance, overrides and dispatch candidates; bounded
paths, shortest paths and neighborhoods; provenance lookup/replay; Git history,
changed paths/symbol candidates, observed tests and test plans; and lexical and
semantic passage search. Outputs share a typed result envelope with the snapshot ID,
candidate count, result limit, truncation flag, stable ordering, diagnostics, and path
mode/hop metadata where applicable.

## Relationship and evidence semantics

- Strict paths traverse deterministic structure, exact SCIP call bindings, imports,
  inheritance and overrides. A projected call step retains the call-site ID and the
  underlying `OCCURS_IN` and `BINDS_TO` edge IDs. It does not create a `CALLS` edge.
- Conservative dispatch paths add only persisted `MAY_DISPATCH_TO` derivations and
  mark those steps uncertain. Unresolved call sites encountered during traversal are
  returned as barriers, not omitted as though resolution had succeeded.
- Path ordering is hop count, uncertainty count, then canonical node/edge identity.
  Traversal is limited to 1–5 hops and stops with truncation metadata at a fixed
  candidate bound.
- Lexical/vector hits are accepted only when chunk ID, snapshot, source ID, path,
  content hash, text, kind and byte span match the immutable retrieval chunk. They
  remain `LEXICAL_PASSAGE` or `SEMANTIC_PASSAGE` evidence and always have
  `verified_graph_path: null`. They never add structural edges.
- Runtime-backed test selection uses invocation-scoped observations, explicitly
  labeled `RUNTIME_OBSERVATION`. These are useful CodeGraph-managed evidence and are
  not authenticated against deliberate tampering by repository code.
- `find_changed_symbols` maps changed paths to symbols currently present in those
  files. It labels them `FILE_LEVEL_CANDIDATE` and `exact_symbol_diff: false`; it does
  not claim that each symbol's body changed. M21's separate commit-to-commit diff
  service maps changed line ranges against two immutable snapshots and preserves
  ambiguous symbol pairs as unmapped.

The service does not call an LLM and does not send repository or query text to a
hosted provider. Semantic query embedding uses the existing local ONNX MiniLM
provider. Missing local embedding/index data produces an empty result or an explicit
adapter diagnostic; it never changes graph structure.
