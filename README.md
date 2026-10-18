# CodeGraph

CodeGraph helps developers investigate Python repositories through source-backed answers, call graphs, highlighted source, Git history, and change-impact analysis. The React interface connects to a Python FastAPI service backed by FalkorDB.

## Features

- Ask questions about callers, dependencies, and paths through code.
- Inspect graph relationships and their supporting source evidence.
- Browse directly connected code in a focused popup.
- Compare commits and inspect potentially affected code and tests.
- Save and reopen investigations.
- Recheck source evidence, or explicitly approve isolated test execution when the required environment is configured.

Exact static bindings, possible targets, and observed test behavior remain distinct evidence types. An exact static call does not prove that code executed.

## Run locally

Use Python 3.11, Node.js 22, Git, and Docker with Compose.

```sh
cp .env.example .env
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-py311.lock
python -m pip install --no-deps -e '.[dev,embeddings]'
npm ci
npm run install:scip-cli
```

Set `ALLOWED_REPOSITORY_ROOTS` in `.env` to the absolute parent directory containing the Python checkouts you want to analyze. Repository access stays disabled until a root is configured. The SCIP installer downloads its CLI from upstream; local embeddings download model files on first use.

Start the database and API:

```sh
docker compose up -d --wait falkordb
.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

In another terminal:

```sh
npm run dev
```

Open <http://127.0.0.1:5173>, add an allowlisted repository, and wait for analysis to finish. Existing local demo snapshots, source caches, database volumes, secrets, and installed dependencies are intentionally excluded from Git. A fresh installation needs its own ingested repository; an empty database will not contain the local Click demo.

Optional settings are documented in `.env.example`. The Ollama query analyzer falls back to deterministic planning when unavailable. Test execution additionally needs a prebuilt Docker image specified by immutable image ID or digest in `RUNTIME_CONTAINER_IMAGE`; repository code only runs after explicit approval.

## Hosted workspace

The production frontend is [codegraph-rho.vercel.app](https://codegraph-rho.vercel.app). GitHub pushes to `main` trigger a Vercel deployment. Production API calls use same-origin rewrites to the HTTPS API on Azure; no API credentials are bundled into the frontend.

The workspace requires a password. The API stores a salted scrypt password hash and signs one-day, Secure, HttpOnly, SameSite cookies. Protected API routes require a valid session; mutating requests must match the configured production origin. Authentication is disabled by default for local development and fails closed if enabled without valid secrets. Secrets are provisioned outside Git.

Azure runs the API as a supervised service and FalkorDB in a separate container with a persistent volume and a loopback-only port. A one-minute deployment timer fetches this repository's `main`, prepares a separate release, runs authentication checks, restarts the API, and restores the previous release if health checks fail. Snapshots and configuration live outside the release folders.

See [hosting operations](deploy/README.md) for service names, updates, recovery, and configuration. The Vercel project uses the [documented build configuration](https://vercel.com/docs/builds/configure-a-build) in `vercel.json`. Preview builds do not grant access to the production API: state-changing requests are restricted to the production origin.

## Verify

```sh
npm run test:frontend
npm run build
.venv/bin/ruff format --check backend
.venv/bin/ruff check backend
.venv/bin/mypy backend/app
.venv/bin/pytest -q
docker compose config --quiet
```

Some integration tests require external services or explicit opt-in. The workflow in `.github/workflows/ci.yml` also installs indexing tools and exercises service integrations.

## Technical references

- [Architecture](docs/architecture.md)
- [Evidence types](docs/evidence-taxonomy.md)
- [Graph schema](docs/graph-schema.md)
- [Provenance and replay](docs/provenance.md)
- [Security boundaries](docs/security.md)
- [Query planning](docs/query-planning.md)
- [MCP tools](docs/mcp.md)
