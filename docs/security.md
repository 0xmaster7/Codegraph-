# Security model

- API and FalkorDB ports bind to loopback in local development.
- Repository inspection is disabled when `ALLOWED_REPOSITORY_ROOTS` is unset or empty. The backend still starts; path-based repository and snapshot operations return a clear configuration error. No filesystem root is inferred from `/`, `$HOME`, or the process working directory.
- When configured, repository paths are resolved canonically and must be inside an allowlisted root. Repository symlinks that resolve outside those roots are rejected.
- File discovery is Git-aware, bounded by source size, excludes common generated/environment directories, and securely opens repository files without following raced symlink components. Symlinks that resolve outside the repository are rejected.
- Static ingestion parses source as data. It does not install dependencies, run setup scripts, or execute repository code.
- `/api/snapshots/inspect` analyzes only; it cannot publish. The managed ingestion job is the sole snapshot publication authority.
- No endpoint accepts arbitrary shell text. Approved pytest collection and execution run only inside disposable Docker containers bound to an immutable runtime materialization and single-use approval. Containers have no network, no host secrets, read-only repository data, resource/time/output limits, and a fixed environment. Docker unavailable fails closed for runtime operations while static investigation remains available.
- Repository content is untrusted input. Hosted embedding transmission requires explicit provider opt-in, benchmark/action policy, a snapshot-bound selected chunk set, secret scanning, and preflight budget approval. An API key alone does not authorize transmission.
- Runtime observations are separate from static facts. They describe what instrumentation observed in a named run; absence of an observation does not disprove a possible execution, and runtime evidence never creates deterministic structural edges. V1 assumes ordinary, non-adversarial OSS repositories and does not defend in-process pytest observations against deliberate tampering by repository code. Isolation and host protections remain in force; externally trusted tracing is post-V1 work.

The local Compose FalkorDB image is pinned by registry digest. SCIP CLI is pinned to v0.10.0; its upstream release download currently has no checksum verification in the installer, so that artifact remains version-pinned rather than checksum-pinned. Docker runtime image identity is additionally recorded by its immutable local image ID or registry digest.

Do not expose the local demo configuration to a network. Network-facing deployment requires authentication and database access controls.
