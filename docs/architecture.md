# Architecture

## Product boundary

CodeGraph investigates Python repositories. It does not autonomously change source files, install project dependencies during ingestion, or execute repository code during static analysis. Syntax establishes source structure and spans. A semantic indexer must establish name binding. An LLM, when added, may explain only validated evidence.

## Current data flow

```text
Git worktree
  → allowlist and tracked-file discovery
  → content hashes and immutable source blobs
  → analysis environment fingerprint and deterministic snapshot ID
  → tree-sitter syntax extraction
  → optional scip-python index over the materialized snapshot
  → source entities, sites, bindings, diagnostics, provenance
  → schema/provenance validation
  → FalkorDB structural-slice persistence
```

The API and web UI bind to loopback by default. FalkorDB data uses a Docker named volume. Snapshot graphs and repository metadata are intended to be separate logical graph families; cross-graph joins belong in the application layer.

M21 compares two Git commits from one allowlisted repository by ingesting each
commit into its own immutable snapshot. Changed source ranges, graph impact, and
test recommendations remain scoped to the base or target snapshot that supports
them. Test recommendations use existing target-snapshot runtime observations and
Docker-isolated pytest collection; M21 does not execute a proposed test.

## Repository identity and snapshot replay

Repository inspection requires a configured `ALLOWED_REPOSITORY_ROOTS` entry. A repository with a remote uses the normalized remote identity. A local repository without a remote uses its Git common directory and root commit identity; the assigned repository ID is persisted under the local snapshot cache. Inspection also records whether the checkout is dirty, shallow, or detached.

Snapshot IDs include the repository ID, base commit, source/config manifest hash, and analysis configuration fingerprint. Tracked source and configuration bytes are materialized in content-addressed blobs before parsing. Untracked files are opt-in; when included, they participate in the manifest and snapshot ID. The cache preserves the analyzed bytes if the worktree changes later. Discovery skips virtual environments and generated build/cache directories, includes `.pyi`, diagnoses notebooks and over-limit source files, and keeps tracked vendored source with an explicit classification.

V1 analysis environment identity intentionally includes the absolute Python executable path and absolute `sys.path` entries. Moving a checkout or its Python environment can therefore produce a distinct `AnalysisEnvironment` identity and distinct future snapshot IDs. This is an environment boundary; historical IDs, provenance, runtime observations, evaluations, and evidence remain immutable and scoped to their original environment and snapshot. Retrieval and evaluation must not silently combine evidence across those identities. Path-independent environment identity and any explicit migration or aliasing strategy are post-V1 architecture work.

Ingestion runs through a single-process async job manager with one local worker. Active duplicate submissions for the same snapshot attach to the existing job. Cancellation is accepted only before publication starts and stops subsequent stages. Startup recovery marks persisted in-progress jobs `INTERRUPTED` and their associated snapshots `FAILED`, allowing a later request to retry safely. The process-local job queue is not durable: persistent restart resumption and multi-worker coordination are post-V1 work.

The controlled `ground-truth-v1` corpus under `backend/tests/fixtures/python_ground_truth_repo` pins UTF-8 byte spans and a deterministic four-commit Git history. Its tests cover fixture collection, identity, discovery, replay, rename/deletion/merge history, shallow clones, and checkout state.

Tree-sitter emits File, Module, Class, Function, and Method entities, plus import, base-class, call-callee, decorator, and callback-reference sites. Site contexts distinguish module scope, class bodies, and callable bodies. A callback-reference site marks a syntactic callback-shaped argument and does not claim a resolved target. Tree-sitter byte offsets and byte columns are canonical. Parser `ERROR` and missing-node regions produce partial-parse diagnostics; overlapping definitions and sites are omitted while clean regions remain available.

## Truth boundaries

- A parsed `CallSite` means the source contains a call expression at the recorded span.
- `BINDS_TO` represents exact SCIP source binding only after an unambiguous occurrence-to-site-to-definition join.
- `IMPORTS` represents deterministic syntax-backed imports resolved to exact indexed local modules or exact SCIP external symbols; unresolved targets produce diagnostics and no edge.
- `MAY_DISPATCH_TO` is distinct from source binding and is derived from deterministic hierarchy analysis.
- Runtime observations describe what instrumentation observed during one explicit run and snapshot; they do not establish what can never execute, and they never create deterministic structural graph relationships.
- Text/vector retrieval is never a structural edge.
- Missing resolution is reported as a limitation, not inferred by a language model.

The SCIP adapter converts UTF-8, UTF-16, and UTF-32 line-relative positions to canonical UTF-8 byte offsets. It withholds bindings when an occurrence is ambiguous, unmapped, or belongs to a dynamic call-through expression.

## Planned services

The service separates static snapshot identity from runtime materialization identity. Static ingestion parses only graph-relevant source/config/document inputs; runtime materialization captures the complete permitted Git-tracked tree. Runtime-only changes may create a new materialization for the same static snapshot. Approved execution names the exact immutable materialization and never reads from the live worktree.

The managed ingestion job is the sole publication authority. `/api/snapshots/inspect` performs analysis and returns results without persisting graph state or transitioning snapshots to `READY`. M17 adds a snapshot-scoped deterministic graph-tool service with bounded paths, strict/conservative traversal modes, unresolved barriers, provenance replay, and runtime/retrieval adapters. M18 adds deterministic-first query planning and bounded orchestration over those tools; optional analyzer proposals are strictly typed and cannot invoke tools directly. `/api/query` requires a READY snapshot and returns a modality-preserving evidence pool. M19 adds `/api/query/validate`, a structured draft contract, deterministic evidence validation, and strict/limitation renderers; it does not configure an answer-generation provider. M18 is checkpointed at `m18-green`.

## ADR template

Create decisions in `docs/decisions/` using `NNNN-short-title.md`:

```markdown
# Decision: <title>
Status: Proposed | Accepted | Superseded
Date: YYYY-MM-DD

## Context
## Decision
## Consequences
## Evidence / alternatives
```
