# Evidence taxonomy

CodeGraph treats an evidence record's origin and meaning as part of its type. The canonical values below are stored and displayed as evidence types; they are not interchangeable confidence levels. A retrieved passage, for example, can support a documentation claim but cannot prove a call edge.

| Type | What it records | What it can support | What it does not establish |
| --- | --- | --- | --- |
| `STATIC_STRUCTURE` | Parsed files, declarations, scopes, and source sites | Existence and location of syntax-backed structure | Name resolution or runtime execution |
| `STATIC_BINDING` | Exact SCIP occurrence joined to one source site and target | An exact static reference/call target | Runtime target for every execution |
| `STATIC_DERIVATION` | Rule-derived inheritance, override, or possible-dispatch edges with premises | Reproducible consequences of supported graph facts | An observed execution or unsupported hierarchy behavior |
| `RUNTIME_OBSERVATION` | Events captured during one approved, snapshot/materialization-bound test run | What the named CodeGraph-managed run observed | Exhaustive behavior, absence of other behavior, or authenticated tracing against malicious test code |
| `GIT_HISTORY` | Commits, parents, changed paths, and source ranges | Historical change evidence within the analyzed repository | Semantic impact without a graph mapping |
| `DEPENDENCY_METADATA` | Parsed declarations, lock data, and distribution metadata | What configuration declares or resolves | That a dependency was installed or imported at runtime |
| `LEXICAL_PASSAGE` | A source/document chunk returned by lexical retrieval | The exact cited passage and its source | A structural relationship |
| `SEMANTIC_PASSAGE` | A source/document chunk returned by local vector retrieval | The exact cited passage and its source | A structural relationship or calibrated probability |
| `UNRESOLVED_DIAGNOSTIC` | A bounded parse, resolver, replay, or analysis limitation | The fact that resolution was not established and why | A best-guess relationship |
| `USER_CONTEXT` | User-authored investigation note or selected context | The context supplied by the investigator | A graph fact or independently validated claim |

## How to read common distinctions

- **Exact versus possible:** `STATIC_BINDING` is a resolver-backed source binding. `STATIC_DERIVATION` with a `MAY_DISPATCH_TO` relation is a possible target under a documented rule. Keep both labels visible when one source call has multiple dispatch candidates.
- **Static versus observed:** static facts are extracted from immutable source/config inputs. Runtime observations are bound to a specific approved run and materialization. No runtime event mutates a static binding or dispatch edge.
- **Graph versus retrieval:** lexical and semantic retrieval point to hash-checked chunks. Their fused score ranks passages; it is not a probability and does not add an edge. Verified Evidence Paths are returned separately.
- **Unknown versus false:** an unresolved site, unobserved candidate, failed replay attempt, or missing runtime event is not proof that the relationship cannot occur. Only completed replay failures designated fail-closed suppress the affected exact edge from strict claims.
- **Snapshot compatibility:** evidence IDs, source hashes, replay state, environment, and snapshot identity travel together. Evidence from another snapshot is rejected instead of silently reused.

## Provenance and claim use

Normalized evidence records identify the evidence ID, canonical type, snapshot, source record, display label, and provenance pointer. Structural claims are checked against graph/tool results and replay eligibility. Runtime claims must refer to a single observation record with matching source, target, invocation, run, and materialization. Passage claims retain the exact chunk identity and span. Conflicting or unsupported claims are not rendered as verified text; validator-owned limitations explain the missing or incompatible evidence.

See [graph schema](graph-schema.md), [provenance and replay](provenance.md), [claim validation](claim-validation.md), and [security boundaries](security.md).
