# M21 commit diff analysis

M21 answers “what could this change affect, and what tests should I run?” for two
commits in one explicitly allowed Git repository. The API is
`POST /api/diff/analyze`; it accepts the repository path, base and target commit
SHA (or an unambiguous hexadecimal prefix), an impact hop limit from 1–5, and a
result limit from 1–500. The response records the resolved full SHAs and exact
base/target snapshot IDs. The only supported comparison is `COMMIT_TO_COMMIT`.
Worktree, index, and staged changes are not inferred.

Each commit is materialized from raw Git tree blobs into a temporary isolated
repository. Git checkout filters, hooks, and repository code do not run during
materialization. The source is inspected and ingested through the existing static
pipeline, then published as an immutable snapshot. Temporary commit trees are
removed when analysis finishes. Snapshot IDs continue to use the existing
identity algorithm and are bound to their corresponding full commit and source
manifest. Git submodule contents are outside the containing repository tree and
are not included.

Git rename detection uses a 50% similarity threshold. Changed line ranges are
mapped to supported class, function, and method entities in each snapshot. A
unique structural signature can pair a symbol across a rename or move. Ambiguous
matches remain `UNMAPPED`; the service does not guess. Every mapped change carries
snapshot-scoped source range evidence and immutable content hashes. Results are
ordered deterministically and bounded by fixed path, symbol, impact, and response
limits; truncation is explicit.

Commit materialization accepts blobs up to 32 MiB each and up to 256 MiB total,
with a 120-second wall-clock deadline. The limits are configurable through
`M21_MAX_COMMIT_BLOB_BYTES`, `M21_MAX_COMMIT_MATERIALIZED_BYTES`, and
`M21_COMMIT_MATERIALIZATION_TIMEOUT_SECONDS`. A limit violation, timeout, or
malformed Git stream fails the diff closed and removes the partial temporary
tree; input is never truncated or treated as a complete snapshot.

Impact traversal starts at changed symbols and uses the existing deterministic
graph service on the matching snapshot. Deleted or moved symbols may also be
traversed on the base snapshot. Each path reports its snapshot, edge evidence,
distance, and uncertainty count. Conservative dispatch remains labeled
`CONSERVATIVE_DISPATCH`; it is never represented as a strict exact path.

## Test recommendation tiers

- **Tier A — `RUNTIME_DIRECT`:** an existing target-snapshot runtime observation
  from the exact target runtime materialization and environment directly covers
  a changed symbol.
- **Tier B — `RUNTIME_DEPENDENT`:** an existing target-snapshot runtime
  observation from the exact target runtime materialization and environment
  covers a dependent symbol connected by a reported impact path.
- **Tier C — `COARSE_HEURISTIC`:** isolated `pytest --collect-only` node IDs are
  selected by deterministic source-to-test path mapping or a static test-source
  reference to a changed symbol name.

Every recommendation has a stable pytest node ID, deterministic reason, tier, and
snapshot- and runtime-scoped evidence. Runtime observations remain separated by
run, materialization, and environment; incompatible observations are not unioned
into one recommendation. Impact paths carry their steps, actual edge evidence IDs,
uncertainty, traversal mode, and contextual unresolved barriers. Conservative
dispatch paths are labeled conservative and are not presented as exact paths.
Runtime evidence from other snapshots is ignored. If no
runtime baseline exists, A and B are explicitly unavailable; the service does not
describe collection as coverage. If Docker or the configured runtime image is
unavailable, collection fails closed and Tier C is unavailable. Collection uses
the existing disposable Docker boundary and immutable runtime materialization;
M21 proposes tests but does not execute them or approve execution.

The endpoint has no LLM dependency and does not send source to a hosted service.
It does not create graph entities for files that the static graph pipeline does
not analyze. The frontend M21 panel displays base and target snapshot identities,
changed symbol mappings, impact evidence, and the tiered plan separately.
