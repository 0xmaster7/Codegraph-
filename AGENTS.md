# Development workspace

This is the canonical CodeGraph development repository:
https://github.com/0xmaster7/Codegraph-

Make all future CodeGraph code changes, tests, and commits in this repository. Treat other CodeGraph / WeMakeDevs Track 1 checkouts as historical reference unless the user explicitly asks otherwise. Verify the Git remote before pushing.

The frontend is React/TypeScript with Vite; the backend is Python 3.11/FastAPI backed by FalkorDB. See README.md for setup and deployment requirements. Keep secrets, local snapshot data, database files, dependency installations, and generated outputs out of Git.

Run checks appropriate to each change. Frontend checks: `npm run test:frontend` and `npm run build`. Backend checks: Ruff, mypy, and the relevant pytest tests. Semantic indexing tests need SCIP installed and the Python environment activated. Vercel configuration covers only the frontend; the API and persistent services are hosted separately.

## Publishing completed changes

The user expects completed implementation changes to appear on the hosted site. After the required checks pass, commit and push your changes to `origin/main` unless the user asks for local-only work, a draft, or a different branch. Preserve unrelated work and verify the remote before pushing.

Production is https://codegraph-rho.vercel.app. Vercel deploys frontend pushes automatically; Azure checks `main` every minute for API updates and defers restarts while comparisons run. Verify the GitHub deployment status and `/health` revision before reporting that a deployment is live. See `deploy/README.md` for operations. Keep hosting credentials outside Git and do not print them.
