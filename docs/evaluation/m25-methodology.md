# M25 evaluation methodology and results

M25 reports are generated under `reports/`. Frozen annotations and datasets are checked in beside this file. Numeric results below come from the run recorded in those reports; empty or `UNMEASURED` cells remain unmeasured.

## Reproduction

Run from the repository root with the locked local environment and SCIP binaries available:

```sh
# Requests v2.32.5 is pinned by the annotation SHA and cloned into the ignored cache.
git clone --quiet --depth 1 --branch v2.32.5 https://github.com/psf/requests.git .codegraph-cache/evaluations/requests-v2.32.5
ALLOWED_REPOSITORY_ROOTS="$PWD/.codegraph-cache/evaluations/requests-v2.32.5" \
  .venv/bin/python scripts/evaluate_repository.py \
  .codegraph-cache/evaluations/requests-v2.32.5 \
  docs/evaluation/requests-v2.32.5.annotations.json \
  --json-output reports/graph-quality.json \
  --markdown-output reports/graph-quality.md
ALLOWED_REPOSITORY_ROOTS="$PWD" \
  .venv/bin/python scripts/benchmark_embeddings.py "$PWD" \
  --provider local \
  --evaluation-set docs/evaluation/retrieval-m15.json \
  --output reports/retrieval.json
SCIP_PYTHON_BIN="$PWD/node_modules/.bin/scip-python" \
SCIP_CLI_BIN="$PWD/.tools/scip/scip" \
  .venv/bin/pytest -q \
  backend/tests/test_scip_real_integration.py::test_controlled_fixture_meets_gold_graph_quality_gates \
  backend/tests/test_ground_truth_fixture.py::test_ground_truth_manifest_covers_every_planned_fixture_case
.venv/bin/python scripts/evaluate_m25_summary.py
```

The summary generator replays the 195-query routing dataset, 16 operation cases, controlled-fixture assertion record, and source-backed hop paths against the frozen Requests analysis cache. The hop-depth source paths are checked through `DeterministicGraphTools`; the in-memory analysis is set READY for read-only access after schema validation, without publishing or modifying a graph. The 30 multi-hop Requests paths are evaluated by `evaluate_repository.py` against exact-binding provenance.

The supplemental local answer-generation arm is run against the isolated Requests snapshot using:

```sh
.venv/bin/python scripts/evaluate_answers_local.py \
  --snapshot snap_840577c9a59b9c5f74756726936a37040b36affc50f843afeb91b36cfa71267a \
  --output reports/answers-local-qwen25-05b.json
```

It invokes the local Ollama `qwen2.5:0.5b` model with deterministic generation settings and schema-constrained JSON. Graph paths and evidence are retrieved from the snapshot before generation; generated output is scored deterministically against the frozen annotation. This is a gold-evidence answer-realization arm, not a measurement of retrieval recall or a production provider integration.

To reproduce the corrected claim-specific citation scores without generating new model
answers, rescore the retained output against the immutable local snapshot analysis:

```sh
.venv/bin/python -m scripts.rescore_answers_local \
  --input reports/answers-local-qwen25-05b.json \
  --output reports/answers-local-qwen25-05b-edge-cited.json \
  --snapshot-cache .codegraph-cache/preaudit/snapshots
```

This deterministic rescore makes no model or network calls. It reconstructs each strict
path from the snapshot cache and checks each claim only against that edge's binding
evidence. The original live-model report remains unchanged and is retained as the source.

## Results

- **Controlled fixture:** 54 planned fixture cases are represented. Two targeted real-SCIP tests passed. The test asserts 100% definition/import/exact-binding precision and recall, zero dispatch candidate errors, complete provenance, zero Unicode coordinate errors, schema validity, and 100% replay pass rate.
- **Annotated Requests v2.32.5:** frozen commit `b25c87d7cb8d6a18a37fa12442b5f883f9e41741`. Definitions: 53/53; imports: 58/58; exact bindings: 100/100, zero false exact; provenance: 1.0; evidence paths: 30/30; schema-invalid facts, dangling IDs, duplicate edge IDs, and Unicode coordinate errors: zero. Twenty unresolved/dynamic cases are included in the annotation.
- **Routing:** 195/195 plans round-trip through the typed schema; all 315 mandatory route annotations were retained (recall 1.0). The operation benchmark passed all 16 cases, including tool arguments, conservative/strict modes, and negative cases.
- **Retrieval:** local ONNX CPU provider `sentence-transformers/all-MiniLM-L6-v2`, 384 dimensions, 190 chunks, six queries. Recall@1 0.417, Recall@5 0.722, MRR 0.889, nDCG@10 0.644. Cold chunk embedding took 34.50 seconds; query embedding took 0.059 seconds; cosine ranking took 0.065 seconds. Peak process RSS was 1.29 GB. Hosted transmission was disabled; reported external cost is $0.
- **Hop depth:** exact entity resolution at depth 0 and source-backed graph paths at depths 1–5 all validated. The existing Requests annotation contributes 25 two-hop and 5 three-hop paths; six additional source-reviewed cases cover each depth 0–5. Results are in `reports/hop-depth.csv` and `reports/hop-depth-details.json`.
- **Planner latency:** 975 deterministic planner/schema-validation samples, P50 0.147 ms and P95 0.306 ms. This excludes API, graph database, retrieval, and answer generation.
- **Supplemental local answer generation:** actual local Ollama `qwen2.5:0.5b` generation over 30 positive and 20 unresolved Requests annotations. All 30 evidence paths resolved as contiguous verified annotated edges. Exact structured path correctness 27/30 (90%); complete supported and correctly cited paths 21/30 (70%); 3 unsupported claims among 63 (4.76%); corrected claim-specific citation fidelity 47/63 (74.60%). Correct abstention and explicit uncertainty were 20/20. Scores were deterministically rescored from retained model outputs and the immutable snapshot analysis; answers were not regenerated. Across the original 50 model calls, mean/P50 latency was 12.93/11.58 seconds, with 9,195 input tokens and 5,252 output tokens; local cost was $0. Original output is `reports/answers-local-qwen25-05b.json`; corrected scores and edge-level evidence provenance are in `reports/answers-local-qwen25-05b-edge-cited.json`.

## Scope and limitations

This V1 has no production natural-language answer-generation model. `answers.json` continues to measure structural answer grounding and evidence-path correctness. The supplemental local Qwen report measures actual local generated prose against gold snapshot-bound evidence; it does not represent production answer behavior. The six hop-depth cases measure source-backed graph path correctness. Hosted A0/A1/B/D/E answer arms remain unmeasured.

The frozen annotation contains no runtime traces. The supplemental isolated runtime run is a six-test diagnostic sample: 858 runtime calls, 656 mapped and 202 unmapped; it found 17 conflict pairs across 15 sites and no confirmed static-truth defect. It must not be generalized as Requests-wide runtime coverage or a population mismatch rate. The controlled fixture checks static dispatch candidates but does not supply runtime observations. The operation benchmark uses deterministic fixture tools rather than a live FalkorDB service. Full live API and database latency, memory, Docker, and browser rendering are recorded separately in M26/M27.

The local retrieval benchmark inspected the checked-in repository at implementation/status commit `0d0a0a4f8d8f8ad1f4a84b9e562b21f546ed506a`; its source manifest excludes untracked evaluation reports. The Requests structural evaluation uses the annotation's frozen commit SHA. All metrics remain scoped to the stated datasets and hardware.
