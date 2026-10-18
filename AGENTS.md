# Development workspace

This is the canonical CodeGraph development repository:
https://github.com/0xmaster7/Codegraph-

Make all future CodeGraph code changes, tests, and commits in this repository. Treat other CodeGraph / WeMakeDevs Track 1 checkouts as historical reference unless the user explicitly asks otherwise. Verify the Git remote before pushing.

The frontend is React/TypeScript with Vite; the backend is Python 3.11/FastAPI backed by FalkorDB. See README.md for setup and deployment requirements. Keep secrets, local snapshot data, database files, dependency installations, and generated outputs out of Git.

Run checks appropriate to each change. Frontend checks: `npm run test:frontend` and `npm run build`. Backend checks: Ruff, mypy, and the relevant pytest tests. Semantic indexing tests need SCIP installed and the Python environment activated. Vercel configuration covers only the frontend; the API and persistent services are hosted separately.
