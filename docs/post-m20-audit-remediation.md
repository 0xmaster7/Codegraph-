# Post-M20 adversarial audit remediation

Status: complete and checkpointed. Implementation commit
`95465f86cc23d60d6b39456060bc5d4f81190414` passed the complete local verification and final
diff/secrets review, was pushed, and passed all remote CI jobs in run `37647210379`. The pushed
`post-m20-audit-green` tag resolves to that commit. The existing M20 checkpoint and frozen
historical M18 evaluation remain unchanged. M21 work has not started.

## Audit findings

| Finding | Remediation | Verification |
| --- | --- | --- |
| AUDIT-001: runtime call pairs could be assembled across different observations or runs | Preserve snapshot, materialization, environment, test definition, invocation, run, observation, source, target, and call-site identity on each observation. Claim validation requires the exact pair and evidence record. | Unit tests reject cross-run/split-observation pairs and preserve conflicting duplicate observation payloads; real FalkorDB persistence coverage records the exact observation link. |
| AUDIT-002: an unresolved selected call site could borrow a sibling site's target | Selected-site callee and dispatch operations query only that site. Unknown selected IDs yield diagnostics without fallback to sibling sites. | Selected unresolved, exact selected-site, and context-wide query tests. |
| AUDIT-003: passage claims could assert an object unrelated to the retrieved chunk | Require the claim object to equal the evidence chunk ID. | Positive exact-chunk and negative wrong-chunk claim tests. |
| AUDIT-004: conservative dispatch was inaccessible or represented as exact | Add a typed dispatch operation and conservative traversal mode; expose `MAY_DISPATCH` in path evidence and qualified claim language. | Planner/orchestrator tests cover supported candidates, selected-site scope, conservative modes, and MAY-qualified paths. |
| AUDIT-005: code passages could be described as documentation | Carry the authoritative chunk kind into evidence and choose wording from that kind. | Tests distinguish document, code, and unknown passage kinds while preserving source and chunk provenance. |
| AUDIT-006: the frozen M18 route metric did not independently measure requested operations | Leave the historical M18 routing fixture/report unchanged and add a separate post-M20 operation fixture with argument, selection, forbidden-operation, and negative expectations. | Independent operation evaluation covers 16 cases; planner tests pass that fixture. |

## Targeted verification records

- Before the bounded top-k change, a synthetic 25,002-candidate caller query returned 100
  results in 3.012 seconds and used 58,892,602 bytes peak Python RSS.
- After the change, the same workload returned the same 100 results and exact candidate count in
  2.828 seconds using 9,547,680 bytes peak Python RSS (about 84% less peak RSS).
- A 2,000-edge regression fixture asserts exact counts, bounded result size, and deterministic
  ordering. Caller, call-site, reference, callee, and importer lookups now stream candidates into
  bounded top-k selection.
- TV-002 browser workflow remains **blocked by environment state**. A read-only FalkorDB metadata
  query found no snapshot rooted at the current repository path. The two local analysis-cache
  entries are `VALIDATING` and belong to a temporary Requests checkout and the relocated project;
  neither is a matching `READY` snapshot. The earlier browser selection also returned
  “Snapshot analysis not found” for a temporary-repository snapshot. Establishing a fresh READY
  graph would publish new project data into the shared FalkorDB instance, so no graph was created
  solely for this browser check. Component/API and real database tests provide the available
  coverage, but they do not change TV-002's blocked status.
- Real Docker cancellation exposed an OrbStack race in which cancellation and executor cleanup
  attempted removal concurrently. Cleanup now polls for confirmed container disappearance for a
  bounded period. The focused real-container cancellation test and cleanup unit test pass after
  this fix.

## TV-001 — outcome-aware replay eligibility

The architecture decision is implemented: the broad `REPLAY_FAILED` review state is retained for
compatibility/workflow, while the specific `replay_state` determines semantics. `PASS` and
`NOT_RUN` preserve normal exact eligibility. `ENVIRONMENT_MISMATCH`,
`EXTRACTOR_VERSION_MISMATCH`, `PREMISE_MISSING`, and `ERROR` are inconclusive and preserve the
original evidence eligibility while exposing the outcome. Only `EDGE_NOT_REPRODUCED` and
`SOURCE_HASH_MISMATCH` fail closed, scoped to the affected edge. Historical evidence/provenance
remains inspectable, unrelated edges remain usable, claims consult server-owned scoped provenance,
and strict paths omit failed edges. The inspector shows replay outcome and current eligibility.

An unknown evidence ID and replay/tooling failures are `ERROR`, not deterministic disagreement.
A transient or inconclusive retry cannot erase a prior fail-closed outcome; an authoritative PASS
for the same evidence replaces it and restores eligibility. Under identical immutable inputs,
matching environment/extractor versions, complete premises, and successful deterministic replay,
an exact edge that does not reproduce is an integrity/correctness failure and is ineligible until
resolved.

The outcome matrix covers `PASS`, `NOT_RUN`, `EDGE_NOT_REPRODUCED`, `SOURCE_HASH_MISMATCH`,
`ENVIRONMENT_MISMATCH`, `EXTRACTOR_VERSION_MISMATCH`, `PREMISE_MISSING`, and `ERROR`. Claim tests
reject forged PASS metadata for fail-closed edges; path and graph tests preserve unrelated and
parallel evidence; recovery tests keep a transient retry from erasing the mismatch and verify
authoritative PASS recovery by edge and provenance identity. The Graph Inspector and Graph Explorer
show current eligibility separately from historical extraction/replay state.

Final local gates after these changes: 256 backend tests passed with the real SCIP, FalkorDB,
ONNX, and Docker integrations enabled; the M18 frozen route evaluation passed schema validation
for 195/195 examples and retained all 315/315 mandatory routes (1.000 recall); the independent
operation evaluation passed 16/16 cases; the Requests v2.32.5 frozen evaluation passed with
definitions 53/53, imports 58/58, exact bindings 100/100 with zero false exacts, and evidence
paths 30/30. Provenance coverage was 1.000, with zero Unicode coordinate errors and duplicate edge
IDs. Ruff, mypy, dependency checks, Compose validation, frontend production build, and
`git diff --check` passed. The changed-file secret scan found only the reviewed synthetic fixtures
in `backend/tests/test_chunking.py` (the fake credential sentinel and private-key rejection data);
neither contains a usable secret and neither was altered to evade scanning. No hosted API was
called.

## TV-003 — bounded-scaling probe

The controlled 25,002-candidate experiment confirmed the materialization/RSS issue and the bounded
top-k change reduced peak RSS by about 84% while preserving exact candidate counts and the returned
ordering. The fixed-size result and ordering are covered by regression tests. This is a targeted
synthetic scalability probe, not a claim that a larger production workload has been benchmarked.

## Checkpoint gate

The complete local gate passed, the final diff and secret review passed, implementation commit
`95465f86cc23d60d6b39456060bc5d4f81190414` was pushed, remote CI run `37647210379` passed for
that exact commit, and `post-m20-audit-green` was created and pushed at that commit. The
remediation checklist is complete. M21 work is outside this remediation task and has not started.
