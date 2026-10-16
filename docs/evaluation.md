# Evaluation contract

Graph quality and answer quality are measured separately. Structural categories include definitions, imports, inheritance, reference sites, semantic binding, dispatch candidate sets, provenance/replay, schema integrity, and runtime mapping. Retrieval metrics include Recall@k, MRR, and nDCG. Answer metrics include validated-claim correctness, evidence-path validity, modality correctness, abstention correctness, and displayed unsupported structural claims.

The controlled fixture and frozen annotations are the gold source. Do not infer expected SCIP implementation symbols from undocumented behavior. Report failure counts by the canonical failure taxonomy and relationship type; do not conceal category failures in one aggregate score.

The graph-quality evaluator lives in `backend/app/evaluation.py`. It compares a frozen
`SnapshotAnalysis` to a versioned JSON annotation and emits JSON plus Markdown. The command-line
runner is `scripts/evaluate_repository.py`; it refuses to score a checkout whose `HEAD` differs
from the annotation's frozen commit.

Run it with the repository root included in the required allowlist, for example:

```sh
ALLOWED_REPOSITORY_ROOTS=/path/to/allowed/repos \
  python scripts/evaluate_repository.py /path/to/checkout docs/evaluation/requests-v2.32.5.annotations.json \
  --json-output docs/evaluation/reports/requests-v2.32.5.json \
  --markdown-output docs/evaluation/reports/requests-v2.32.5.md
```

Annotations contain:

- `annotation_version` and repository name, URL, and full commit SHA;
- an explicit scope for definitions and imports, with every definition/import in that scope
  manually checked;
- at least 100 individually selected call/reference sites, labeled as exact, unresolved, or
  dynamic, with target qualified names for exact bindings;
- dispatch candidate sets and byte-slice checks where applicable;
- at least 30 multi-hop questions with manually reviewed expected evidence paths;
- at least 20 unresolved/dynamic examples when the selected repository contains them.

The report lists precision and recall by definitions and imports, exact-binding precision/recall,
false-exact count, dispatch mismatches, duplicate edge IDs, provenance coverage, Unicode mapping
errors, and the canonical failure taxonomy. The taxonomy records missing/false definitions and
imports, exact-binding mismatches, false exact bindings, dispatch candidate mismatches, duplicate
edge IDs, missing/orphan provenance, and Unicode coordinate mismatches.

For the real-repository sample, acceptance is DEFINES precision >= .99 and recall >= .98, IMPORTS
precision >= .98 and recall >= .95, exact-binding precision >= .95, false-exact rate <= .02,
provenance coverage 1.00, and evidence path validity >= .95. A failed threshold remains visible in
the report and requires a fix or a documented scope limit; it cannot be hidden by an aggregate.

The repository selection process records public URL, frozen commit, package/test conventions,
source size, and structural features before annotation. M10 selected `psf/requests` v2.32.5 at
`b25c87d7cb8d6a18a37fa12442b5f883f9e41741`: it has conventional packaging, a runnable test suite,
moderate size, and meaningful request/session/adapter call and inheritance structure.

No benchmark or quality claim should be made from this evaluator until the frozen annotation report
is checked in and passes the stated gates.

## M15 code and document retrieval

`docs/evaluation/retrieval-m15.json` freezes six retrieval questions and relevant source paths
across Python and Markdown. `scripts/benchmark_embeddings.py` reports Recall@1, Recall@5,
Precision@5, MRR, nDCG@10, embedding and ranking latency, process memory, model configuration
identity, and hosted token cost. Run the local ONNX baseline with:

```sh
HF_HOME=/path/to/model-cache \
  python scripts/benchmark_embeddings.py /path/to/allowed/repository \
  --provider local --output docs/evaluation/reports/m15-embeddings-local.json
```

For a side-by-side run, use `--provider both`; each provider runs in a fresh process so memory
measurements do not include the previous provider. Add `--benchmark-hosted` only for the approved
hosted comparison.

The hosted OpenAI comparison requires both `HOSTED_EMBEDDING_ENABLED=true` and the explicit
`--benchmark-hosted` flag. Before sending, the command checks each selected chunk against the
approved snapshot manifest and exact byte span, rejects private paths and detected secrets, and
scans the evaluation queries. It batches only selected chunk text and benchmark queries. An API key
by itself never enables transmission. V1 uses local ONNX by default; OpenAI remains explicit opt-in.

The local baseline uses `sentence-transformers/all-MiniLM-L6-v2` at the pinned revision through
ONNX Runtime CPU; model, tokenizer, Sentence Transformers module, runtime, truncation, pooling, and
normalization parameters are recorded in the embedding spec and cache key. Hosted pricing and its
source metadata are configurable and recorded with each run report.

### M15 measured comparison (2026-10-03)

Both reports use snapshot `snap_95052b31935aa777e353c99749cb44fcc81137192f4fa5b7031e3b88f5e91df0`,
source manifest `ff77d960f556db3965cb931f8e4bc554d1ab43c95e354c483fdbd8cbff920baf`, the same 155
chunks from 10 approved paths, and the same six evaluation queries. The local and hosted runs are
recorded in [the ONNX report](evaluation/reports/m15-embeddings-local.json) and
[the OpenAI report](evaluation/reports/m15-embeddings-hosted.json).

| Metric | MiniLM ONNX CPU | OpenAI `text-embedding-3-small` |
| --- | ---: | ---: |
| Dimensions | 384 | 1536 |
| Recall@1 | 0.417 | 0.333 |
| Recall@5 | 0.667 | 0.667 |
| Precision@5 | 0.700 | 0.700 |
| MRR | 0.861 | 0.778 |
| nDCG@10 | 0.699 | 0.754 |
| Chunk embedding time | 37.11 s | 12.41 s |
| Query embedding time (six batched queries) | 0.947 s | 0.626 s |
| Peak process RSS | 1.38 GB | 327 MB |
| Estimated hosted cost | $0 | $0.001113 for 55,628 tokens |
| Vector payload / estimated HNSW memory | 238,080 B / 285,696 B | 952,320 B / 1,142,784 B |

The local run led on Recall@1 and MRR; hosted led on nDCG@10, embedding latency, and peak RSS;
Recall@5 and Precision@5 were equal. Six queries are too few to establish an overall quality winner.
V1 uses local ONNX as its operational default for offline operation, no API-key requirement, and no
hosted text transmission. OpenAI remains an explicit opt-in provider; a larger retrieval evaluation
may justify changing that default. The hosted run's measured cost is below the $0.50 default run
budget. Before any hosted request, the benchmark counts the selected snapshot chunks and evaluation
queries with the pinned model's `cl100k_base` tiktoken encoding and compares the estimate against
`MAX_HOSTED_EMBEDDING_RUN_COST_USD`. Missing tokenization/pricing metadata and estimates above the
limit fail closed. New hosted reports record the preflight estimate, configured limit, and
provider-reported actual tokens and cost. The frozen reports above preserve the measurements from
the benchmark run before this preflight guard was added; they record the provider-reported token
usage and cost but do not claim a preflight estimate.

The parser dependency range is constrained to Tree-sitter 0.25.x and language-pack 0.11.x. The
newer 0.26.0/0.13.0 combination crashed the native parser while traversing real Requests source;
0.25.2/0.11.0 completed the same source analysis on Python 3.11 and 3.14 with real SCIP output.
