# Structured claims and deterministic validation

`POST /api/query/validate` runs the same bounded M18 query against one `READY`
snapshot, then checks an untrusted `DraftAnswer` against the resulting M18
evidence pool and tool results. The draft is never allowed to supply its own
trusted evidence, graph path, validation status, or rendered answer.

## Claim contract

Drafts are bounded to 100 claims, 32 evidence IDs per claim, and 32 optional
draft limitations. Unknown fields and unsupported enum values are rejected.
Claim and evidence IDs are unique within their respective scopes. The draft's
snapshot, the query's snapshot, the evidence pool, and every tool result must
match exactly.

Claim kinds cover exact direct calls, graph paths, observed runtime calls, Git
changes, declared dependencies, and documentation statements. Each kind has a
closed predicate and modality contract. Structural claims are checked against
the canonical static bindings or a returned verified path. A conservative
dispatch path must use `MAY_REACH` and `MAY`; an exact/derived path must identify
all of its edge evidence and retain its exact or derived modality. Ambiguous or
unresolved results do not validate as exact relationships.

Runtime claims require one call-observation record whose source and target match
the claim and whose snapshot, run, invocation, and runtime materialization are
present on that same record. Aggregated invocation summaries cannot support a
call relationship by unioning entities from separate observations. Runtime
claims remain `OBSERVED`. Passage claims require the matching lexical or
semantic evidence, exact chunk ID, and source identity. Their renderer uses the
validated chunk kind: code passages are described as code, Markdown passages as
documentation, and unknown kinds as retrieved passages. They do not become
structural claims. Git and dependency claims require both the matching evidence
type and a tool result containing the claimed identifiers.

Missing evidence IDs are rejected. Cross-snapshot inputs fail closed. Conflicts
are represented as `CONFLICTING`; they are not resolved by ranking or claim
order. Rejected and conflicting claims have no rendered answer text. Validation
retains each accepted claim's evidence records, provenance pointers, and
snapshot identity.

Replay eligibility is resolved from the server's snapshot-scoped provenance,
never from a client-supplied replay state. `EDGE_NOT_REPRODUCED` and
`SOURCE_HASH_MISMATCH` reject exact claims that depend on the affected edge.
Inconclusive outcomes (`ENVIRONMENT_MISMATCH`, `EXTRACTOR_VERSION_MISMATCH`,
`PREMISE_MISSING`, or `ERROR`) leave the original exact evidence eligible and
remain visible as replay status. A strict path cannot contain a fail-closed
edge.

## Strict rendering

Draft prose is treated as untrusted and is never copied to answer text. The
renderer produces fixed wording from validated structured fields. For a
documentation claim, it quotes only the exact retrieved passage and identifies
its repository path. Model-supplied limitation prose is also omitted; the
limitation renderer emits validator reasons and deterministic query/tool
diagnostics. This prevents unvalidated prose from appearing above or beside
validated claims as though it were authoritative.

There is no answer-generation provider configured by the API in M19. A caller
that has a structured draft can submit it to `/api/query/validate`; the service
re-runs the scoped deterministic query and validates the draft against that
fresh evidence. No hosted model is called, and no repository text is transmitted
by this endpoint. The ordinary `/api/query` response remains the M18 evidence
pool contract.

M20 may present the validated claims and their evidence interactively. It must
use `rendered_text` and the attached evidence records rather than draft prose.
