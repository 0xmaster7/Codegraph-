# Persistent investigation memory

Investigation memory lets the workspace resume its questions and evidence across
backend restarts. Records live in the FalkorDB metadata graph, separately from
immutable snapshot graphs. Each investigation points to exactly one `READY`
snapshot through a `PINS` relationship, which also protects that snapshot from
retention cleanup.

The persisted record contains the repository and snapshot identity, question
history, selected entities/sites/edges,
only `SUPPORTED` or `QUALIFIED` claim records, snapshot-bound Evidence Paths,
runtime run IDs, the latest validated response, and user notes. Notes are plain
annotations; they are not sent to the graph writer and cannot create structural
relationships. Runtime references retain their original snapshot and run IDs.

## API

- `POST /api/query/investigate` creates a memory record and returns its
  `investigation_id`. Send that ID on the next request to append a question to
  the same investigation.
- `GET /api/investigations?snapshot_id=...` lists recent records for one READY
  snapshot.
- `GET /api/investigations/{investigation_id}?snapshot_id=...` reopens a record.
- `POST /api/investigations/{investigation_id}/notes` appends a bounded user note
  with the same `snapshot_id`.

When a caller supplies a different snapshot ID, the API returns HTTP 409 with
`SNAPSHOT_MISMATCH`, the stored snapshot, and the requested snapshot. It does not
migrate the record or reinterpret its old evidence. Re-run the question on the
new snapshot to create a separate investigation.

The workspace lists investigations for the selected snapshot, restores their
latest validated response, and displays notes separately from claims and graph
evidence. The backend and UI keep question history and run references bounded.
Updates carry a monotonic record revision. FalkorDB compares that revision in the
same write that replaces the JSON record; stale writers retry from the latest
record a bounded number of times. This protects note appends and query updates
across separate backend processes sharing the database.

## M24 coverage

| Requirement | Implementation / verification |
|---|---|
| Investigation records and questions | `InvestigationMemoryRecord` in FalkorDB metadata; restart integration reloads the record |
| Validated claims and Evidence Paths | Only supported/qualified claims are indexed as validated memory; paths are snapshot-checked |
| Selected entities and user notes | Bounded typed records; notes are displayed as annotations and the test verifies they do not add snapshot edges |
| Runtime references | Run IDs are kept with the record snapshot; each observation/call is rejected if its snapshot differs |
| Same-snapshot reopen | GET/list API and real restart integration reopen the stored response and evidence paths |
| Different-snapshot reopen | API returns `SNAPSHOT_MISMATCH`; unit/API tests confirm no migration |
| Concurrent updates | Revision compare-and-swap plus bounded retry; unit races use separate services and the opt-in FalkorDB integration races separate graph clients |

## Verification

Unit/API tests cover persistence across service instances, validated-claim
filtering, selected-entity retention, notes as annotations, snapshot mismatch,
cross-snapshot evidence rejection, and snapshot-scoped listing. The opt-in
FalkorDB integration test saves a real investigation, restarts the container,
reopens the record, and verifies that its pinned snapshot and Evidence Paths
remain available.
