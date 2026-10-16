# Query planning and orchestration

M18 turns a user question into bounded calls to the snapshot-scoped M17 graph tools and returns an inspectable evidence pool. M19 adds deterministic validation for structured answer drafts; neither endpoint lets a model call tools directly.

## Request and response

`POST /api/query` accepts a question, a required `snapshot_id`, optional selected node/edge/site IDs, and a result limit from 1 to 100. The endpoint requires that the requested snapshot exists and is `READY`; it loads that snapshot's analysis and refuses cross-snapshot tool or evidence results. Questions are limited to 4096 characters.

The response contains the validated `QueryPlan`, optional provider and usage metadata, the exact typed tool calls and their bounded results, diagnostics/truncation state, and a `QueryEvidencePool`. Evidence entries preserve their M17 modality and provenance. Occurrences record which tool returned an item, whether the source result was uncertain, and whether it belonged to a verified graph path. A retrieved code passage is not represented as a verified path or structural relationship.

`POST /api/query/plan` remains a deterministic plan preview and does not query a snapshot. Its result may have no `snapshot_id`; `/api/query` always binds the executable plan to the requested snapshot.

`POST /api/query/validate` accepts the same query fields plus a bounded
`DraftAnswer`. It executes a fresh query against the required `READY` snapshot,
then validates every claim against that query's tool results and evidence pool.
The route returns only canonical renderer text for supported or qualified
claims, along with status, evidence provenance, and deterministic limitations.
Unknown evidence IDs and cross-snapshot drafts cannot validate. Draft text and
draft limitation prose are not rendered. See [claim validation](claim-validation.md)
for the claim contract and support rules.

`POST /api/query/investigate` is the M20 UI path. It runs one bounded M18 query,
builds candidate claims only from the returned M17 records, and sends those
candidates through M19 validation before returning them with ordered Evidence
Paths, passages, unresolved barriers, runtime observations, and truncation
metadata. It does not call an answer-generation model. The browser renders only
supported or qualified claims and links each record back to the inspector or
the separate passage/runtime evidence panel. See
[the investigation workspace](investigation-workspace.md).

## Deterministic routes and optional analysis

Deterministic pre-rules run first. They identify exact symbol mentions, graph operations, Git/runtime/dependency requests, and conceptual text/vector requests. Exact identifier, graph, Git, and runtime routes required by those rules remain mandatory.

An optional `LLMProvider` can propose only fields in the strict `QueryPlanProposal` schema: bounded symbol mentions, enumerated routes and graph operations, a hop limit from 1 to 5, typed Git metadata, and a runtime flag. The merger unions proposal routes and operations with deterministic requirements; proposals cannot remove mandatory routes, replace selected context, widen explicit hop limits, supply tool names, or supply commands. Invalid, malformed, or timed-out proposals fall back to deterministic pre-rules.

The production API uses the local Ollama provider when `LOCAL_QUERY_ANALYZER_ENABLED=true` (enabled in `.env.example`), with loopback URL and model selected by `LOCAL_QUERY_ANALYZER_URL` and `LOCAL_QUERY_ANALYZER_MODEL`. It sends only the question and records local token and latency metadata at zero provider cost. The default model is `qwen2.5:0.5b`. An unavailable service, timeout, or invalid proposal falls back to the deterministic plan. Hosted analysis remains unavailable through the production API; no source or evidence is sent to a hosted provider. Claim validation itself is deterministic and makes no provider calls.

## Tool boundary and limits

The orchestrator dispatches through a fixed enum allowlist backed by `GraphToolRegistry`; it never resolves a caller-provided method name or executes shell text. Every call carries the approved snapshot ID and passes M17 validation. The default result limit is 50, configurable per request up to 100. One query can make at most 32 tool calls. Traversals use the plan's bounded hop limit (default 3, maximum 5), preserve strict-static versus conservative-dispatch modes, and surface tool diagnostics, unresolved barriers, and truncation.

Only unambiguous (`UNIQUE`) canonical symbol resolutions seed graph operations. Text and semantic retrieval return passage evidence only. Git, runtime observations, dependency metadata, and graph evidence remain distinct evidence types; the orchestrator does not create or persist graph facts.

## Failure behavior

Invalid request schemas return a validation error. Missing snapshots return 404; non-READY snapshots or snapshot mismatches return 409. A failed analysis load or graph-store operation returns a service error. Optional analyzer errors use the deterministic plan and include a diagnostic. Missing, ambiguous, or out-of-snapshot selected entities are reported without guessing an identity.

## Routing evaluation

`docs/evaluation/query-routing-m18.json` is the frozen M18 set of 195 questions across 13 routing categories, including exact identifiers, graph traversals, semantic and hybrid retrieval, Git, runtime/tests, diffs, selected-node/site follow-ups, and out-of-scope questions. The local evaluator validates every plan against the typed schema and measures recall of each example's mandatory routes. The required targets are schema validity 1.0 and mandatory-route recall at least 0.98. The dataset and checks do not require an LLM or hosted API access.

The frozen route dataset remains unchanged. The independent
`docs/evaluation/query-operations-post-m20.json` set measures selected tool
operations, typed arguments, traversal mode, selected-site preservation, and
forbidden operations. `backend/tests/test_planner.py` runs both metrics
separately. A selected call-site follow-up invokes `find_callees` for only that
site; a context-wide question still enumerates the context's call sites.

Questions that explicitly ask whether a path may/could exist or mention
possible dispatch use `CONSERVATIVE_DISPATCH`. Ordinary path questions remain
`STRICT_STATIC_BINDING`. The deterministic traversal mode is preserved through
optional-plan merging; a provider proposal cannot downgrade conservative
semantics.
