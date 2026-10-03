#!/usr/bin/env bash
set -euo pipefail
moments_repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$moments_repo_dir"
exec python3 studio/server.py "$@"
