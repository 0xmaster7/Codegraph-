# Graph Inspector and review queue

The local UI ingests a repository through the single-process job API, shows the current job stage, and lets the operator cancel before publication. It lists recent snapshot metadata and opens retained snapshot analyses by immutable snapshot ID. Graph queries remain limited to `READY` snapshots.

The Overview page shows snapshot identity, source and environment counts, exact semantic bindings, conservative dispatch counts, unresolved sites, and diagnostics. The Graph Explorer lists definitions, source sites, and provenance-backed edges. Selecting an edge opens its canonical edge ID, relation, extractor and version, rule, file hash, source span, premise edges, replay result, and review state. Selecting a definition or site lists its connected edge provenance.

Source preview requests use the selected snapshot ID, repository-relative path, and UTF-8 byte span. The API reads the content-addressed snapshot blob and verifies its hash before returning text, so a missing or stale blob produces an explicit error instead of falling back to the current checkout.

The review queue includes resolver/review states, failed or non-passing replay outcomes, unresolved call/value/import/callback sites, and analysis diagnostics. Opening a provenance item in the queue selects its exact edge record in the inspector. Replay updates the cached analysis and persisted provenance state; the UI reloads the snapshot after replay so the queue reflects the result.

The inspector's neighbor action uses the strict graph caller/callee APIs. Those APIs only return exact semantic bindings from a `READY` graph. Conservative dispatch remains labeled in the snapshot data and is not presented as an exact binding.

The M20 investigation composer is shown for `READY` snapshots. It attaches the
currently selected node/site/edge as query context and displays only server
validated claim cards. Exact static bindings and conservative possible
dispatch candidates have separate labels and panels. Evidence Path records
preserve ordered node IDs, relationship IDs, provenance IDs, modality, and
snapshot identity. Retrieved passages and run-linked runtime observations use
separate panels and never appear as structural edges. See
[the investigation workspace](investigation-workspace.md) for the response
contract and bounds.
