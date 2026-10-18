#!/usr/bin/env bash
# Runs as the deployment owner; persistent data and secrets stay outside releases.
set -euo pipefail
umask 077
state="${CODEGRAPH_STATE:-$HOME/.local/share/codegraph}"
control="${CODEGRAPH_REPOSITORY:-$HOME/projects/codegraph}"
mkdir -p "$state/releases"
exec 9>"$state/deploy.lock"
flock -n 9 || exit 0
export PATH="$HOME/.local/bin:$PATH"
if [[ -f "$HOME/.nvm/nvm.sh" ]]; then
  source "$HOME/.nvm/nvm.sh"
  nvm use 22 >/dev/null
fi
[[ "$(git -C "$control" remote get-url origin)" == "https://github.com/0xmaster7/Codegraph-" ]]
git -C "$control" fetch --quiet origin main
revision="$(git -C "$control" rev-parse origin/main)"
release="$state/releases/$revision"
if [[ -L "$state/current" && "$(readlink "$state/current")" == "$release" ]] && systemctl --user is-active --quiet codegraph-api.service; then
  exit 0
fi
previous="$(readlink "$state/current" || true)"
# An interrupted dependency install is retried; the serving release stays intact.
if [[ ! -f "$release/.prepared" ]]; then
  mkdir -p "$release"
  git -C "$control" archive "$revision" | tar -x -C "$release"
  cd "$release"
  uv venv --python 3.11 --clear .venv
  .venv/bin/python -m ensurepip
  uv pip install --python .venv/bin/python -r requirements-py311.lock
  uv pip install --python .venv/bin/python --no-deps -e '.[dev,embeddings]'
  npm ci --no-audit --no-fund
  mkdir -p .tools/scip
  if [[ -x "$state/tools/scip" ]]; then
    ln -sf "$state/tools/scip" .tools/scip/scip
  else
    npm run install:scip-cli
    mkdir -p "$state/tools"
    cp .tools/scip/scip "$state/tools/scip"
  fi
  export PATH="$release/.venv/bin:$PATH"
  .venv/bin/pytest -q backend/tests/test_auth.py
  printf 'CODEGRAPH_REVISION=%s\n' "$revision" > deployment.env
  touch .prepared
fi
ln -sfn "$release" "$state/next"
mv -Tf "$state/next" "$state/current"
systemctl --user restart codegraph-api.service
for attempt in {1..30}; do
  if curl -fsS --max-time 3 http://127.0.0.1:18091/health | python3 -c 'import json,sys; x=json.load(sys.stdin); sys.exit(0 if x.get("falkordb")=="connected" else 1)' 2>/dev/null; then
    printf 'Deployed %s\n' "$revision"
    exit 0
  fi
  sleep 2
done
if [[ -n "$previous" ]]; then
  ln -sfn "$previous" "$state/next"
  mv -Tf "$state/next" "$state/current"
  systemctl --user restart codegraph-api.service
fi
printf 'Deployment failed health checks; restored previous release.\n' >&2
exit 1
