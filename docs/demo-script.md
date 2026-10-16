# V1 demo runbook

This runbook describes the observable CodeGraph workflow and the evidence to show at each step. It uses the checked-in Python `ground-truth-v1` fixture to avoid cloning an external repository. The fixture is a controlled example, not a substitute for the frozen Requests evaluation.

## Before the demo

1. Install the dependencies from the [README](../README.md) and configure `.env` with `ALLOWED_REPOSITORY_ROOTS` set to this repository's `backend/tests/fixtures` directory. Use a dedicated demo environment and local FalkorDB database.
2. Ensure SCIP CLI and `scip-python` are available. For semantic passages, allow the local ONNX model to finish its first download and warm-up before recording. Hosted embedding is not needed.
3. Start FalkorDB, the FastAPI backend, and Vite using the README commands. Open the UI and inspect the health/configuration state.
4. Keep a copy of the fixture's `ground_truth.json` and the M25/M26 reports available for reference. Do not change fixture source to force an expected outcome.

## Walkthrough

| Step | Action | Evidence to show |
| --- | --- | --- |
| 1. Register and ingest | In the repository screen, select the fixture path and ingest it. | Allowlisted repo identity, immutable snapshot ID, ingestion stages, and final `READY` status. Ingestion reads source as data; it does not install fixture dependencies or execute it. |
| 2. Graph quality | Open the snapshot overview and diagnostics. | Counts and diagnostics are scoped to the selected snapshot. Explain that measured graph-quality scores live in the frozen reports, not in an invented live score. |
| 3. Exact binding | Open Graph Explorer, choose a resolved call site, and inspect it. | Source span and hash, `BINDS_TO` target, binding status, SCIP resolver provenance, and the matching source definition. |
| 4. Possible dispatch | The checked-in `ground-truth-v1` fixture demonstrates a single supported candidate at `pkg/base_dispatch.py::child.run`. For the required multi-candidate screen, ingest a separate temporary Git repo with the `dispatch_probe.py` example constructed by `test_real_scip_python_output_binds_aliases_bases_and_external_symbols` in `backend/tests/test_scip_real_integration.py`. | `dispatch_state = MULTI_CANDIDATE`, all three `MAY_DISPATCH_TO` targets, rule/premise provenance, and the distinction between exact source binding and possible runtime dispatch. Keep this companion snapshot separate from the frozen gold corpus. |
| 5. Replay | Open provenance for a structural/derived edge and request replay. | Replay scope and outcome; explain that an inconclusive environment/tool outcome is not a successful replay and that only the documented fail-closed outcomes suppress an affected edge. |
| 6. Multi-hop impact | Ask a question that routes through a graph path; compare strict and conservative modes where available. | Ordered Evidence Path, hop and uncertainty metadata, every supporting edge/provenance item, and unresolved barriers. A passage match is not part of the structural path. |
| 7. Unresolved dynamic case | Inspect `getattr(target, name)()` in the dispatch companion repo (or another dynamic call-through fixture). | Explicit `UNKNOWN` dispatch / unresolved state and diagnostic. State that absence of an exact binding does not mean the call is impossible. |
| 8. Hybrid retrieval and claims | Search for a source/document passage, then review the resulting validated evidence/claim cards. | Exact hash-checked chunk, lexical/vector signals where present, evidence type, validated source pointer, and validator-owned limitation. Similarity is a ranking value, not confidence or a graph edge. |
| 9. Diff analysis | Select two commits supported by the fixture history (or use the current target and a known base commit). | Changed ranges, base/target snapshot identities, mapped symbols and candidate status, impact evidence, and test recommendations. Never describe file-level candidates as exact symbol-body changes. |
| 10. Review test plan | Generate the evidence-backed plan for selected entities. | Selected tests, evidence tier, missing coverage/uncertainty, and the exact collection/execution action. Stop before approval until the command and target snapshot/materialization are visible. |
| 11. Approve and run | For a disposable, controlled fixture only, review the exact plan and approve it in the UI. | Single-use approval, fixed pytest selection, immutable read-only materialization, network-disabled isolated Docker execution, resource limits, and resulting status. Do not run this step against an arbitrary repository or ambiguous plan. |
| 12. Runtime evidence and refresh | Open the named run's observations and refresh the investigation. | Runtime evidence tied to the exact run/materialization and kept separate from static graph facts. An unobserved candidate remains unknown. |
| 13. Evaluation and performance | Open the checked-in graph-quality, retrieval, answer, runtime, routing, hop-depth, latency, cost, and performance reports. | Frozen inputs, measured metrics, documented hardware/workload, a clear distinction between local answer evaluation and production answer generation, and zero hosted embedding cost for M25. |

## Safe stop conditions

- If the local DB, SCIP, model, or runtime image is unavailable, show the typed diagnostic and continue with independent read-only steps. Do not silently switch to guessed bindings or hosted services.
- Do not approve a test plan unless its target, fixed command, snapshot/materialization, and limits are visible and the controlled fixture is the intended target.
- Do not imply that local UI acceptance proves an external browser requirement, long-duration stability, generated natural-language answer quality, or malicious-code-resistant runtime tracing.
- Never put API keys or private repository text in a recording. Hosted embeddings are disabled by default.
- Query analysis may use the configured local Ollama model on loopback and receives only the question. It cannot call tools or create graph facts; deterministic routes and evidence validation remain authoritative. This is separate from answer generation, which is not configured.

## Reproduction record

Record the CodeGraph commit, fixture commit, snapshot ID, local dependency/tool versions, and exact screens/actions in the demo notes. M25 and M26 measurement results and limits are maintained in [evaluation](evaluation/m25-methodology.md) and [performance](evaluation/m26-performance.md); milestone and external verification status is tracked in [pending work](pending-work.md).
