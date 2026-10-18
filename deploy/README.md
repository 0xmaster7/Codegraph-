# Hosting operations

## Production

- Frontend: https://codegraph-rho.vercel.app
- Azure API: https://codegraph-pancakes-0xmaster7.indiasouthcentral.cloudapp.azure.com
- Canonical GitHub repository: https://github.com/0xmaster7/Codegraph-
- Production branch: `main`.

Vercel deploys the frontend on pushes. Rewrites send `/api/*` and `/health` to Azure. API requests retain the browser's same-origin session cookie; credentials are not exposed in frontend environment variables.

The workspace is a private, single-owner deployment. Share its password only with trusted users: authenticated users can inspect the allowlisted repositories, start analyses and, if a runtime image is configured, explicitly approve sandboxed tests. This is not a multi-user tenant isolation system. Test execution remains unavailable until an immutable runtime image is provisioned.

## Azure layout

Connect with `ssh pancakes`.

- Control Git clone: `~/projects/codegraph` (remote is the canonical repository).
- Releases: `~/.local/share/codegraph/releases/<commit>`.
- Active release: `~/.local/share/codegraph/current`.
- Snapshots and embeddings: `~/.local/share/codegraph/{snapshots,embeddings}`.
- Private service configuration: `~/.config/codegraph/api.env` (mode 600).
- Allowlisted repository checkouts: `~/codegraph-repositories`.
- Deployment entrypoint: `~/.local/share/codegraph/update.sh`.
- API listener: `127.0.0.1:18091`.
- Database listener: `127.0.0.1:6391`, Docker Compose project `codegraph-production`.
- HTTPS: system Caddy service with `/etc/caddy/Caddyfile`.

`api.env` must set `HOSTED_AUTH_ENABLED=true`, a 16-byte salt and 64-byte scrypt digest as hex separated by a colon in `HOSTED_PASSWORD_HASH`, a random `HOSTED_SESSION_SECRET` of at least 32 characters, and `PUBLIC_APP_ORIGIN=https://codegraph-rho.vercel.app`. The digest uses scrypt N=16384, r=8, p=1. Rotate the signing secret to invalidate all existing sessions. Never put credentials into `VITE_` variables or commit them.

Only HTTP(S) ports 80/443 were added to Azure's inbound rules. The API and this deployment's database are bound to loopback. Existing services belonging to other projects are separate.

## Updates and recovery

```sh
systemctl --user status codegraph-api.service codegraph-update.timer
journalctl --user -u codegraph-api.service -u codegraph-update.service -n 80 --no-pager
systemctl --user start codegraph-update.service
curl -fsS http://127.0.0.1:18091/health
```

The health response includes the deployed Git revision and database connectivity. User services persist across SSH disconnects and start after boot because user lingering is enabled. Caddy renews the HTTPS certificate automatically.

The service PATH includes the shared Node installation as well as its Python environment; SCIP requires both Node and pip. Commit comparisons run as bounded background jobs, so browsers poll short requests rather than holding a proxy connection open. Jobs are scoped to the API process; after a restart or expiry the page asks you to retry. The update timer defers deployment while a comparison is active and checks every minute. Dependency installation or health-check failures leave the previous release active. Before deployment, each release runs the authentication and comparison-job tests. General tests still belong in development/CI; successful deployment is not a substitute for the full suite. The deployment script and unit files are in `deploy/azure`; if changing those, install the revised bootstrap files on the VM and reload systemd as well as pushing code.

To pause automatic backend updates:

```sh
systemctl --user stop codegraph-update.timer
```

To restore an older prepared release, stop the timer, repoint `current` to that release, restart `codegraph-api.service`, and check `/health`. Resume the timer when `main` contains the desired version. Release folders are retained for recovery; monitor disk use and remove obsolete inactive releases when needed.

Do not run `docker compose down -v` against the production database. It deletes the persistent volume. Source snapshots and the database must be backed up together for a complete restore; the original local demo database is not part of this deployment.
