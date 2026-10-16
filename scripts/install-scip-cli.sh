#!/usr/bin/env bash
set -euo pipefail

version="v0.10.0"
os="$(uname -s | tr '[:upper:]' '[:lower:]')"
machine="$(uname -m)"
case "$machine" in
  x86_64|amd64) arch="amd64" ;;
  arm64|aarch64) arch="arm64" ;;
  *) echo "Unsupported SCIP CLI architecture: $machine" >&2; exit 1 ;;
esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tools_dir="$root/.tools/scip"
archive="$(mktemp)"
trap 'rm -f "$archive"' EXIT
mkdir -p "$tools_dir"
curl --fail --location --silent --show-error \
  "https://github.com/scip-code/scip/releases/download/$version/scip-$os-$arch.tar.gz" \
  --output "$archive"
tar -xzf "$archive" -C "$tools_dir" scip
"$tools_dir/scip" --version
