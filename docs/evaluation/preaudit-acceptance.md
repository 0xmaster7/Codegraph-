# Final pre-audit acceptance evidence

Run date: 2026-10-10. This report covers TV-002 browser acceptance, the supplemental
M25 local natural-language generation arm, and an isolated Requests runtime/static
comparison. No independent release audit was run.

## TV-002 — browser Evidence Path and Graph Inspector

**Requirement.** `spec.md` sections 36.2–36.4 require ordered path nodes and
relationships, path length, static versus conservative dispatch labels, unresolved
barriers, runtime badges, snapshot identity, and Graph Inspector provenance. Retrieved
passages must remain distinct from structural graph evidence.

**Workflow.** Used a genuine Codex in-app browser at `http://127.0.0.1:5174/` against a
disposable isolated CodeGraph backend/database and an isolated Requests v2.32.5 source
snapshot. The source clone is clean at commit
`b25c87d7cb8d6a18a37fa12442b5f883f9e41741`. Asked:

> Show me the shortest evidence path from `src.requests.adapters.HTTPAdapter.build_response`
> to `src.requests.cookies.MockRequest`.

**Observed.** The rendered Evidence Path contained the two ordered call edges:

1. `HTTPAdapter.build_response` → `extract_cookies_to_jar`
2. `extract_cookies_to_jar` → `MockRequest`

The browser showed the snapshot identity, hop count, static-binding labels, provenance
IDs, supporting static-binding/structure records, and the unresolved-call limitation.
Opening the call-site inspector showed `src/requests/adapters.py:365:8–365:30`,
`SINGLE_CANDIDATE`, extractor `scip-python 0.6.6`, rule `scip.binding.v1`, and the
source preview/hash. The browser also showed that no LLM answer provider and no
retrieval passages were configured; no prose or runtime behavior was inferred. A bare
ambiguous `build_response` request showed the no-match state. Browser console
error/warning collection was empty for the isolated tab.

The snapshot's analysis diagnostics pane was not empty: it listed 928 diagnostics
(910 `UNRESOLVED_NAME`, 18 `RESOLVER_ERROR`), including SCIP UTF-8 offset errors in
`tests/test_requests.py`. Those diagnostics are outside the two verified path edges;
they remain a visible graph-coverage limitation and are not counted as browser console
failures or silently treated as valid bindings.

The pre-existing browser at `http://127.0.0.1:5173/` remained bound to the user's
persistent Requests service. It was inspected read-only: the existing Requests snapshot
and exact `request → src.requests.api.request` edge were visible in Graph Inspector.
The Evidence Path workflow writes investigation metadata, so its full query was run
only on the isolated clone; the persistent graph, service, volumes, snapshot identities,
and browser binding were not changed. The isolated clone used the same Requests commit
as the persistent graph. This is real browser acceptance of the documented path and
inspector behavior, while explicitly avoiding a write through the protected binding.

**Outcome: PASS** for the documented TV-002 browser behavior on the isolated clone.
Evidence is the browser-rendered path and inspector state above; no saved screenshot was
produced. No API-only, component-test-only, or production-build-only result is used as
browser evidence. The test-file diagnostics remain a limitation for broader snapshot
coverage, not a blocker for this exact source-backed path.

## M25 — local natural-language answer generation

**Method.** The local model was asked to realize 30 frozen Requests multi-hop answers
from verified snapshot-bound graph paths and to abstain on 20 unresolved/dynamic
annotations. Exact alias-based claim/evidence references, abstention, uncertainty, and
schema-constrained JSON were scored deterministically. This measures generated answer
quality with gold evidence; it does not measure retrieval recall or a production model
integration. The answer-correctness score is exact ordered structured path-claim
correctness against the frozen symbols. The annotation contains no gold prose, so free
text was not given a human semantic score; unsupported-claim and citation rates apply to
the structured claims emitted with that prose.

**Execution.** Provider: local Ollama CPU runtime; model `qwen2.5:0.5b`, quantization
Q4_K_M, local model digest
`a8b0c51577010a279d933d14c2a8ab4b268079d44c5c8830c0a93900f1827c67`. Input repository
is Requests v2.32.5 commit `b25c87d7cb8d6a18a37fa12442b5f883f9e41741`; evaluation
annotations are `docs/evaluation/requests-v2.32.5.annotations.json`, SHA-256
`13a92d28eded53ade44cdf4c519b88b2e161f9598b51abc4d39f31ae6101d5fd`. The graph
snapshot is `snap_840577c9a59b9c5f74756726936a37040b36affc50f843afeb91b36cfa71267a`.
No hosted provider was called; cost was $0 and repository source was not transmitted.

**Results.** See `../../reports/answers-local-qwen25-05b.json` for per-case records and
aggregate measurements. The final run validated 30/30 annotated evidence paths through
contiguous direct edges, each with snapshot-bound verified graph evidence. The model
produced an exact ordered path in 27/30 cases (90.0%); complete supported/cited claims
for every expected path edge in 21/30 cases (70.0%). It emitted 3 unsupported claims
among 63 positive claims (4.76%). The initial pooled-citation score of 51/63 (80.95%)
was invalid because evidence for one edge could be credited to another. Deterministic
claim-specific rescoring against the exact expected edge and snapshot found 47/63
validly cited claims (74.60%). Corrected scores and edge-level provenance are in
`../../reports/answers-local-qwen25-05b-edge-cited.json`; retained model output was
not regenerated. On unresolved cases it abstained correctly in 20/20 cases, made zero
unsupported target claims, and represented uncertainty in 20/20 cases.

The run used 9,195 prompt tokens and 5,252 output tokens. Mean latency was 12.93s and
median latency 11.58s across 50 model calls; local cost was $0. The JSON-schema
required-field mismatch (`evidence_ids` versus prompted/scored `evidence_refs`) was
corrected and covered by a regression test. A later path-grounding review found that an
equally short alternate route could be scored; the final harness now composes only
individually verified frozen adjacent edges. That changed the middle node for
`requests-mh-29` to the annotated route; aggregate scores remained the same. Earlier
runs are preserved as `answers-local-qwen25-05b-before-schema-fix.json` and
`answers-local-qwen25-05b-before-edge-validation.json`.

The first full attempt also stopped at one annotated multi-hop question where the
globally shortest graph path was shallower than the frozen answer path. The harness was
corrected to retrieve and verify every adjacent annotated edge before composing the
frozen path; the full dataset was rerun after this correction.

**Limitations.** The small local model's measured answer quality is not an acceptance
threshold or a claim about production behavior. Unsupported-claim, citation, and exact
path rates must be read alongside per-case output. Hosted answer arms and live
production answer generation remain unmeasured.

## Requests runtime/static mismatch sample

**Provenance.** An isolated source clone of Requests v2.32.5 at commit
`b25c87d7cb8d6a18a37fa12442b5f883f9e41741` was ingested to READY snapshot
`snap_840577c9a59b9c5f74756726936a37040b36affc50f843afeb91b36cfa71267a`. Runtime
materialization: `rtree_e407b4bc52295a5edd3de82dc64a61dd64e4cefe92bc14a9137d74f0827bdb99`.
Environment: `runtime_1b92e9a9bcf12c7fdecfb716242db569fc8cc6d0a8d76ede778f7d75d86b4108`,
Python 3.11.13/Linux, runtime image
`sha256:4ab000cd4d7333b6ff0dcc1831bc5f9d42ef47830901c1a140b64b1b88e1d057`.
The test container had a read-only source mount, no network, and bounded CPU/memory.
The disposable FalkorDB instance was separate from the persistent service.

**Procedure.** Test discovery collected 611 test invocations. One-time action approval
was used for exactly six selected test IDs: cookie redirect, cookie send on redirect,
response Unicode decoding, JSON decode serialization, session-cookie override, and the
known reentrant response-lines expected failure. The final run completed with 5 passed,
1 expected xfail, 0 failed, and 0 skipped. Pytest emitted a read-only cache warning and
Flask deprecation warnings. Runtime data is bound to snapshot, materialization,
environment, selected test invocation, and run identity.

The approved command was the exact argument vector
`python -m pytest -p cg_plugin_206073810474958a0a72 --rootdir=/workspace -o addopts= --import-mode=importlib -q --`
followed by the six node IDs listed in the JSON report. Approval
`approval_3837b2693bc76d26407695a413f2225cf30b1751dd8ea21ddb32b12bb2fbdbcd`
bound the command hash `4f3c66823737bab30cba887d728bf37dfeddd83e510a2c8b1052e4cd5004ce0c`;
the one-time execute call returned the run ID recorded below.

Earlier attempts exposed two runtime-path defects before any test executed: the approved
node validator rejected legitimate pytest parameter IDs with punctuation, and the
isolated plugin copy used an invalid pytest unconfigure argument. The validator now
separates the allowlisted file path from printable item IDs passed as one argv entry;
the hook now uses pytest's `config` argument. A profiler shutdown exception was also
fixed by removing profile hooks during pytest teardown. Focused regression tests cover
these corrections.

The successful action is run
`run_0043bc73563f71c2f68572915ba80a31975cbb032b1d2587ce08bd10484e6ad1`. It collected
858 runtime calls and 1,379 runtime entries. The corrected offline validator mapped
656 calls and left 202 unmapped. Its site-state counts were 81 `MATCH`, 35 `UNRESOLVED`,
2,370 `UNOBSERVED`, and 15 `MISMATCH` sites. The 17 distinct observed
site/target mismatch pairs are detailed in
`../../reports/runtime-mismatch-preaudit.json`.

**Comparison rules and interpretation.** Only observed targets conflicting with a
non-empty static candidate set are classified as `MISMATCH`; empty static candidates
remain `UNRESOLVED`. Unobserved candidates are not evidence of impossibility. Static
binding, conservative dispatch candidates, and run-specific runtime observations remain
separate.

The 17 diagnostics comprise 13 implicit Python constructor targets (`Class` versus
`Class.__init__`), two context-manager protocol targets (`Session.__enter__` and
`Session.__exit__`), and two apparent nested-call attribution cases (`iter_slices`
observed under the `self.iter_content` site, and `CaseInsensitiveDict.__setitem__`
observed under the `to_native_string` site). They are genuine observed
site/target conflicts under the current collector's call-site join; the two nested cases
and implicit protocol/constructor differences do not, by themselves, establish that
SCIP's source binding is wrong. No static graph fact was rewritten. No mismatch
precision/recall is claimed from this six-test sample.

The API process had loaded the pre-correction validator before the final run. Its raw
persisted diagnostic list contained 26 mismatch diagnostics. The report's 17-pair result
was recomputed offline from the same raw calls and immutable snapshot analysis using the
corrected validator, which avoids counting observations at call sites with no static
candidate set. The correction is regression-tested in
`backend/tests/test_runtime_validation.py`.

**Costs and limitations.** The final six-test runtime action lasted about 7.9 seconds.
This is a bounded diagnostic sample, not a Requests-wide coverage or mismatch-rate
estimate. The 202 unmapped calls and 35 unresolved sites remain visible as coverage and
join limitations; no absence of trace is interpreted as a negative result.

## Maintainer verification

- Focused runtime/evaluator regressions: 15 passed; evaluator-only regressions after
  annotated-edge hardening: 3 passed.
- Full backend suite against explicit disposable FalkorDB port 16391 and temporary
  caches: 303 passed, 7 optional integration skips, 1 shared-host startup-cleanup test
  deselected. The all-tests attempt produced 302 passed, 7 skipped, and a failure in
  that startup-cleanup test because Docker context resolution timed out. That test
  enumerates and removes all runtime-labeled containers on its Docker daemon, so it was
  not retried on a host with other active task containers. The only container created
  by the failed attempt, `codegraph-test-orphan-probe`, was removed by exact name.
- Ruff checks and format checks passed on all modified Python files. Mypy passed for
  the modified backend modules. `git diff --check` passed.
- `detect-secrets` found only expected hexadecimal identifiers/digests and the prepared
  command hash in generated reports; no credential or key material was found.
- The frontend was not modified; its production build is verified by the required
  remote frontend CI job for the final commit.

## Independent-audit remediation follow-up

The M18 production query-analysis gap is resolved with an optional loopback-only
Ollama provider at `/api/query`. The real integration test
`CODEGRAPH_LOCAL_LLM_INTEGRATION=1 .venv/bin/python -m pytest -q
backend/tests/test_local_query_analyzer.py::test_production_query_route_uses_local_model_and_keeps_graph_rules`
used installed `qwen2.5:0.5b`, a synthetic question, and a fake read-only graph store.
It passed in 4.25 seconds; the result included local token/cost/latency usage, retained
deterministic `GRAPH` and `CALLERS` requirements, and used the exact test snapshot.
The model schema permits only supplemental entity, route, and operation suggestions;
deterministic code owns mandatory routes, Git/runtime handling, hop limits, tools, and
all graph/evidence facts. Hosted providers remain disabled and answer generation is
still not a production feature. The provider has a 7.5-second request timeout and the
orchestrator enforces an eight-second maximum. Mock tests cover unavailable, malformed, and timed-out
providers falling back to deterministic plans.

F-01 now binds calls only when a simple name/member terminal span exactly matches one
SCIP occurrence. Conditional, nested-call, and computed callees remain unresolved. A
narrow `super()` case additionally requires SCIP to resolve that receiver independently
as the built-in. Real-SCIP compound-callee and Requests checks pass: exact bindings
100/100, zero false exacts, and evidence paths 30/30.
The post-fix reproducible Requests output is in
`../../reports/graph-quality-post-remediation.json` and
`../../reports/graph-quality-post-remediation.md` for frozen source commit
`b25c87d7cb8d6a18a37fa12442b5f883f9e41741` and analysis snapshot
`snap_34b5c73e702277e8060a1f4885bddd584b982f4f6ebdfc6b43641ef6ab605f50`.

F-03 uses atomic revision compare-and-swap in FalkorDB and bounded reread/merge retries.
The real two-client concurrent-note test passed against a volume-free disposable
FalkorDB container. F-04 startup cleanup removes only containers whose owner process
is proven dead or whose PID start marker changed; live and unknown owners are kept.
Focused unit races pass. The host-wide Docker cleanup integration was not run because
it enumerates all runtime containers on the shared daemon; no such sweep was performed.

**Remediation local verification.** With snapshot and embedding caches directed to
`/private/tmp`, the backend suite passed 317 tests with 14 optional integrations
skipped and the live-provider case deselected; real SCIP was enabled. The live-provider
case passed separately with authorized loopback access, as did the real-SCIP controlled
fixture and compound-callee tests and the real FalkorDB concurrent-write test. Ruff and
mypy passed. Docker-backed host cleanup and shared Compose/FalkorDB suites were not run
against the shared daemon; the isolated CAS container was removed by its exact name.
