# V1 release and three-stage demo validation

Date: 2026-10-11
Branch: `codex/m01-environment-persistence`
Audited implementation: `a1348bffb0bdea01efc198406ac25e3c11cebb36`

This report records what was actually exercised. It does not claim that every acceptance item in the release/demo checklist passed.

## Release checkpoint

- Local `HEAD` at the start and end of implementation work: `a1348bffb0bdea01efc198406ac25e3c11cebb36`.
- Exact-SHA GitHub Actions run previously verified green: [run 38066487927](https://github.com/0xmaster7/wemakedevs---track-1/actions/runs/38066487927), including backend, frontend, and integrations.
- The annotated `v1.0.0` tag points to the audited implementation. Local tag object: `defbb06625f2db12d67f7ab74c42bd724c179c36`; local peeled target: `a1348bffb0bdea01efc198406ac25e3c11cebb36`. Its creation and remote target were verified at the release checkpoint.
- `submission-candidate` was not changed. Its local tag object is `34a4e162b163a4adabdbf70e5b57e9694f491de5`, peeled to `6998ca1fd8daa5515730aa7206d239162954ba39`.
- No implementation files were changed during the demo validation. A fresh `git ls-remote` check during report preparation could not resolve `github.com`; therefore the remote branch/tag state was not re-queried in this run. The prior exact-SHA CI and tag verification remain the last successful remote observations.
- The two pre-existing untracked `.DS_Store` files (`.DS_Store` and `docs/.DS_Store`) were left untouched.

## Stage A — controlled fixture (partial)

Temporary fixture: `/private/tmp/wemakedevs-v1-demo-stagea-mvvxsjax/python-ground-truth`
Pinned fixture merge commit: `8bfeaee906ce862b7063534b88e6083668195176`
Snapshot: `snap_020ee89344846564391f18574ddd4f333afad79ad0ef2f518faa41519f14c68d`
Repository identity: `repo_42de5492bef233b06f6d413fe40610200588a7c2a88f15ae9a415d87815e0ff2`

The real SCIP/FalkorDB fixture reached READY with 16 Python files, 81 definitions, 78 source sites, 50 review items, 42 exact bindings, 0 multi-candidate bindings, 36 unresolved sites, and 18 diagnostics. Tool versions were Python 3.11.13, scip-python 0.6.6, and SCIP CLI 0.10.0.

The focused real-SCIP checks passed:

```text
SCIP_PYTHON_BIN='<repo>/node_modules/.bin/scip-python' SCIP_CLI_BIN='<repo>/.tools/scip/scip' \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  backend/tests/test_scip_real_integration.py::test_controlled_fixture_meets_gold_graph_quality_gates \
  backend/tests/test_scip_real_integration.py::test_real_scip_withholds_exact_binding_for_compound_callable_expressions
2 passed in 18.48s
```

The compound conditional callee remained unresolved rather than receiving a false exact binding. In the real browser, the Graph Inspector showed a source span and SCIP provenance for `pkg.auth.verify_token`; replay passed. A three-hop Evidence Path included unresolved barriers and did not promote them to certainty.

**Not completed:** this fixture history contained only initial content, a marker addition, a file rename, and an obsolete-file deletion. Its actual diff yielded no impact paths or relevant deterministic test recommendations. To honor the instruction not to alter the ground-truth fixture, no approval could be issued and no sandbox test/runtime observation could be produced for Stage A. Thus Stage A is a useful partial graph/browser check, not a pass of the complete 20-item checklist.

## Stage B — Requests v2.32.5 (substantial E2E coverage)

Disposable clone: `/private/tmp/wemakedevs-v1-demo-stageb-yn4gbwdm/requests-shallow`
Pinned commit: `b25c87d7cb8d6a18a37fa12442b5f883f9e41741` (`v2.32.5`)
Primary snapshot: `snap_e247b5c2346712338e0a5029de6d48d6c27e3836416d60132f01ac91b3b753bf`
Repository identity: `repo_f31f7619873672d8036abdd0b8dbafc97fc0859ef52eb10e1b155463e3bb0dd8`
Runtime materialization: `rtree_28ee436e363b435464b4f77747d22091c71151fabbecac828e28e15c05f72cfa`

The isolated graph reached READY: 36 Python files, 827 definitions, 2,937 sites, 2,359 review items, 1,680 exact bindings, 0 multi-candidate bindings, and 1,257 unresolved sites. The persistent Requests service, graph, volumes, snapshots, and existing browser binding were not changed.

In the real browser UI, the query “Who calls get_netrc_auth, and show the source evidence for each caller?” returned the two exact callers `SessionRedirectMixin.rebuild_auth` and `Session.prepare_request`. Graph Inspector opened `src/requests/sessions.py:298:19–298:33` (bytes 10976–10990), target `src.requests.utils.get_netrc_auth`, extractor scip-python 0.6.6, provenance `scip.binding.v1`; replay passed. The answer used deterministic graph routing after the optional query analyzer timed out; no retrieval passage was used.

A committed diff from `7bc45877a86192af77645e156eb3744f95b47dae` to `5b4b64c3467fd7a3c03f91ee641aaa348b6bed3b` covered `src/requests/utils.py` and `tests/test_utils.py`. Among the capped recommendations, the two relevant tests were selected in the product UI. The reviewed isolated command SHA-256 was `beff820e7384c4264cef6f504eda452ad398ad56316546aa5c7fd6bce2657d66`; the command ran only:

```text
python -m pytest -p cg_plugin_8fe095518774068ef7e6 --rootdir=/workspace -o addopts= --import-mode=importlib -q -- tests/test_utils.py::TestGetNetrcAuth::test_works tests/test_utils.py::TestGetNetrcAuth::test_not_vulnerable_to_bad_url_parsing
```

The UI showed approval followed by `2 passed · 0 failed · 0 skipped`, exit 0, run `run_006accdbbb13aa5800afb9e4be04417f21d6f3cbe6185a68dafe7bdeec56e20b`. Runtime validation recorded 2 observed calls, 2 mapped, 0 unmapped, and 0 mismatches. A subsequent browser investigation displayed static evidence and those observations separately, tied to their run and materialization identities.

The selected diff had 13 exact impact paths and Tier C recommendations; Tier A/B were unavailable without a runtime baseline. The response was about 1.4 MB due to the capped candidate set. No latency percentiles or test-run duration were returned by the product and none are inferred here. A disposable earlier partial-clone attempt failed and was not used for the final test. The optional query analyzer timeout caused deterministic routing; the required graph answer and evidence remained available.

**Not independently verified:** a dedicated empty/error-state browser walkthrough and a controlled latency/performance profile were not captured. Stage B validates a real graph investigation, exact evidence, the approval UI, sandbox execution, runtime mapping, and investigation refresh, but should not be read as exhaustive UX acceptance.

## Stage C — sktime full-repository stress attempt (not completed)

Official repository pinned at `cba5617432c5e527dcc786dd513d739afa9e42c7`. It was cloned shallowly into `/private/tmp/wemakedevs-v1-demo-stagec-sktime`, detached at that SHA, with no tracked modifications.

Read-only inventory: 2,222 tracked files, 1,910 Python files, 431,158 Python lines, 52,209,942 bytes of checkout files, 25,010,192 bytes under `.git`, about 80 MiB total disk usage. The largest Python file measured 4,623 lines (`sktime/libs/granite_ttm/modeling_tinytimemixer.py`).

The application supports repository-root ingestion only; inspection showed no supported include/exclude or subtree-scope ingestion mode. No subtree feature or manually trimmed pseudo-project was introduced. Given that constraint, the actual full repository was attempted sequentially against a dedicated loopback backend and a newly created volume-free FalkorDB container capped at 3 GiB RAM, 3 GiB swap, and 1 CPU. SCIP indexing had the existing 900-second timeout. Host free-memory observations fluctuated between 48% and 72%; the isolated FalkorDB process stayed below 100 MiB.

The snapshot `snap_d4518c78168b7c3f00aef9f9a97b75f77986545de52286588f0a8245016a1b56` reached parsing and then semantic indexing. SCIP did not complete within 900 seconds; the snapshot never reached READY, so graph publication, source-backed investigations, and query/performance measurements could not be performed. The job was cancelled, then only the task-created loopback worker and its disposable database container were stopped after post-index analysis did not respond promptly. No pre-existing container or volume was stopped, removed, or modified. The sktime test suite was not run.

This is an observed scale limitation of the tested environment/configuration, not evidence that full sktime is a supported V1 requirement or that graph correctness failed. No largest successful sktime workload exists from this attempt; no counts, accuracy, query latency, or memory peak beyond the observations above are claimed. A representative Stage C1 workload could not be selected because the current product has no supported repository subsetting mode.

## Overall disposition

- Release tag and audited implementation checkpoint: verified locally and previously verified remotely; tag targets remain the audited SHA.
- Stage A: partial; no approved execution/runtime observations because the ground-truth diff produced no test recommendations.
- Stage B: substantial real-browser and approved-execution path passed; optional analyzer timed out and dedicated error/empty UX plus latency profiling remain unverified.
- Stage C: full sktime ingestion did not reach READY within the configured SCIP timeout; no sktime query claims are made.
- No implementation defect was identified and no implementation code changed. The v1.0.0 implementation SHA therefore remains `a1348bffb0bdea01efc198406ac25e3c11cebb36`; this report is documentation-only.
- Demo readiness: the Requests core evidence-to-approval-to-runtime path was demonstrated, but the complete three-stage checklist is **not fully verified**. Do not present this report as a full Stage A or Stage C pass.

## Demo-closure follow-up — 2026-10-11

### Remote documentation checkpoint

The documentation-only checkpoint `c18705e44c9f59e2592f539882fc0a9e5047b61b` is on `codex/m01-environment-persistence` at `origin`. GitHub Actions run [38119448136](https://github.com/0xmaster7/wemakedevs---track-1/actions/runs/38119448136) completed successfully for that exact SHA; backend, frontend, and integrations jobs all passed. A read-only `git ls-remote` confirmed remote branch `c18705e...`, `v1.0.0` object `defbb06625f2db12d67f7ab74c42bd724c179c36` peeled to `a1348bffb0bdea01efc198406ac25e3c11cebb36`, and `submission-candidate` object `34a4e162b163a4adabdbf70e5b57e9694f491de5` peeled to `6998ca1fd8daa5515730aa7206d239162954ba39`. Neither tag moved.

### Requests browser states and recovery

All work used the already-created disposable Requests graph at `127.0.0.1:16383`, isolated API at `127.0.0.1:18001`, and temporary Vite UI at `127.0.0.1:15174`, bound to snapshot `snap_e247b5c2346712338e0a5029de6d48d6c27e3836416d60132f01ac91b3b753bf` (Requests `v2.32.5`, commit `b25c87d7cb8d6a18a37fa12442b5f883f9e41741`). The persistent API, database, volumes, snapshots, and browser binding were not accessed for writes or restarted.

Actual UI interactions produced these visible states:

- Graph Explorer search for `__codex_no_such_symbol_987654__` returned no matching graph records.
- Asking for an evidence path to that nonexistent symbol produced a validated answer with **no claim passed validation**. The limitations panel showed no retrieval passages, “Symbol was not found,” and a local analyzer `JSONDecodeError` fallback. No claim was fabricated.
- Selecting the review-queue import `from pygments.style import Style` opened Graph Inspector at `docs/_themes/flask_theme_support.py:2`, UTF-8 bytes 65–97. The inspector displayed `Binding: UNRESOLVED`, no SCIP symbol, and `Dispatch state: NOT_APPLICABLE`; source preview matched the snapshot bytes.
- Retrying with “Who calls get_netrc_auth? Show source evidence for each caller.” recovered in the browser and showed two **EXACT STATIC EVIDENCE** claims: `SessionRedirectMixin.rebuild_auth` and `Session.prepare_request`, each bound to `src.requests.utils.get_netrc_auth` with a visible `BINDS_TO` evidence record. The limitations panel showed analyzer timeout and deterministic pre-rules. This verifies the fallback/retry path and answer provenance.

The missing-symbol case is the invalid-result state exercised here; an induced complete API outage was not tested. The timeout and malformed-proposal cases were exercised through the actual UI/API route. Screenshots were inspected during the live browser run; no private source was copied into a hosted service.

### Requests query latency measurements

Measurements used serial `urllib` requests from the host to the isolated API. Each case ran 10 times. The values below include API, snapshot/graph access, response serialization and client JSON parsing, but exclude browser rendering. P95 is the nearest-rank estimate; with n=10 it equals the sample maximum and is descriptive only, not a stable service-level percentile.

| Operation and endpoint | n | HTTP | P50 ms | P95 ms | Observed result |
| --- | ---: | ---: | ---: | ---: | --- |
| Exact lookup, `GET /api/snapshots/{id}/graph/symbols/resolve?query=get_netrc_auth&limit=10` | 10 | 200: 10 | 1,346.3 | 3,246.2 | Unique exact symbol resolution |
| Caller lookup, `GET /api/snapshots/{id}/graph/callers?target_id={get_netrc_auth_id}&limit=10` | 10 | 200: 10 | 1,616.3 | 4,162.9 | Exact caller graph records |
| Source evidence, `GET /api/snapshots/{id}/source?...utils.py&start_byte=6400&end_byte=6450` | 10 | 200: 10 | 1,128.1 | 1,986.5 | Snapshot-backed source spans |
| Combined caller/callee natural-language query, `POST /api/query` | 10 | 200: 10 | 5,217.1 | 6,955.4 | Local provider parsed in 10/10; this deliberately combined phrasing did not resolve its requested symbol/call site, so timing is not a correctness pass |
| Multi-hop evidence-path natural-language query, `POST /api/query` | 10 | 200: 10 | 7,028.5 | 9,740.3 | Provider JSON proposal rejected in 10/10; deterministic fallback returned no evidence items for this prompt |
| Natural-language exact/caller/source query, `POST /api/query` | 10 | 200: 10 | 7,326.0 | 9,345.7 | Provider JSON proposal rejected in 10/10; deterministic fallback returned two evidence items, separately verified through the successful browser retry |

The three natural-language sample groups used the same pinned snapshot, result limit 10, and local `ollama-local/qwen2.5:0.5b` provider. No hosted provider or paid service was used. Provider parsing failures and unanswered path prompts are retained as failures/limitations; HTTP 200 alone is not treated as answer correctness. The supported demo path is deterministic graph routing with validated evidence; optional analysis remains best-effort.

### Ollama cold/warm investigation

Ollama reported version `0.21.2`; model `qwen2.5:0.5b`, digest `a8b0c51577010a279d933d14c2a8ab4b268079d44c5c8830c0a93900f1827c67`, quantization `Q4_K_M`, was already installed. All requests were to loopback `127.0.0.1:11434`; the question text was the only model input, with no repository source or evidence sent to a hosted service. Ollama was healthy. Immediately before the first isolated production query, `/api/ps` listed no loaded models; afterward it listed the requested model with 442,191,360 bytes of runtime memory and a 4096-token context.

The first real `POST /api/query` took 13,530 ms end-to-end and returned HTTP 200 with provider/model identity but the configured 8-second analyzer timeout diagnostic; deterministic routes still returned two evidence items. Provider usage latency was absent because the analysis timed out. A warm retry of the same production query completed the actual Ollama request in 4,145.6 ms, completed the API in 7,304.8 ms, and returned `EXACT`, `FULL_TEXT`, and `GRAPH/CALLERS` routes, three tool executions, and two evidence items without analyzer fallback. The model was therefore contacted and successfully parsed on a warm production invocation. This is consistent with cold model loading contributing to the first timeout, but does not prove it was the only cause.

The repeated warm workload was not uniformly reliable: the combined caller/callee group parsed 10/10 proposals (median provider time 3,516.6 ms), while all 20 multi-hop and general natural-language proposals in the other groups were rejected as `JSONDecodeError`. Their deterministic pre-rules remained active. The browser’s valid caller retry also completed using deterministic fallback after an analyzer timeout. The installed timeout is 7.5 seconds in the provider and at most 8 seconds in the orchestrator. No timeout was increased. No evidence in this run indicates Ollama service unavailability or severe host memory pressure; host resource telemetry was not continuously sampled during the earlier rehearsal. The strongest supported diagnosis is that the fixed request deadline can be exceeded on a cold invocation and that this small model’s schema-constrained proposal output is inconsistent for some prompts. Keep the deterministic demo path available; do not rely on this provider for every request.

The existing opt-in U-01 test remains the prior independent integration evidence: `backend/tests/test_local_query_analyzer.py::test_production_query_route_uses_local_model_and_keeps_graph_rules` passed in 4.25 seconds with installed Qwen and a fake read-only graph store. The production Requests calls above independently exercised the same live local provider with the real isolated graph.

### sktime timeout diagnosis and bounded scope

The previous attempt remains pinned to upstream SHA `cba5617432c5e527dcc786dd513d739afa9e42c7`; it was not rerun. Retained evidence shows the product entered semantic indexing after materialization/parsing. The product helper `backend/app/scip.py::run_scip_index` applies a 900-second deadline to the `scip-python index . --project-name ... --project-version ... --output ...` subprocess. It does not perform dependency installation in this step. The index file remained only 168 bytes, so there was no usable partial SCIP index or diagnostic report; snapshot status never reached READY and no graph was published.

At 11:52:03 local time, a retained macOS `sample` of the indexing Node child (launched 11:35:44, parent Python process) reported 1.6 GiB physical footprint and showed the main thread repeatedly in V8 interpreter frames. Earlier process observation recorded about 107.5% CPU; host free-memory observations ranged 48–72%, and the isolated FalkorDB process stayed below 100 MiB. The available sample therefore supports active computation at that instant rather than a network wait or idle stall. It does not establish sustained CPU behavior for all 900 seconds, the package responsible, or a definitive algorithmic root cause. No network sockets or dependency-install child were observed during the retained process inspection. There is no evidence to attribute the timeout to severe memory pressure or blocked dependency resolution. Best-supported diagnosis: the semantic indexer did not finish the full 1,910-file/431,158-line workload within the product’s fixed 900-second limit; the underlying per-package bottleneck is unknown.

A bounded sktime run was not feasible under an existing supported mode. The product accepts repository-root ingestion only and exposes no subtree include/exclude scope. A hand-trimmed set of internal sktime files would break package/import context and would not be a legitimate representative upstream repository, so it was not created. No smaller sktime READY snapshot, query timings, or correctness claims are reported. During the earlier attempt, the snapshot cache was placed under the temporary checkout; the resulting untracked `cache/` made the manifest report `is_dirty: true`, while `git status --untracked-files=no` showed no tracked source changes. This is a benchmark setup/provenance caveat; future large-repository attempts should keep caches outside the source checkout.

### Final disposition after this follow-up

- No implementation source or regression tests changed. Only this documentation report is updated; the audited release implementation remains `a1348bffb0bdea01efc198406ac25e3c11cebb36` and `v1.0.0` still peels to it.
- Requests browser recovery and exact evidence presentation passed on the disposable snapshot. Analyzer cold/warm behavior is variable; a small descriptive latency profile is available above. A full API-outage UI state and a statistically robust P95 benchmark remain unverified.
- Full sktime did not reach READY within the supported 900-second run; bounded sktime is not currently supported. This remains a documented scale limitation, not evidence of an automatic V1 correctness failure.
- The persistent Requests backend, graph, volumes, snapshot identities, and browser binding remain preserved.
