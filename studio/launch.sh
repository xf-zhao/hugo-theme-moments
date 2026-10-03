#!/usr/bin/env bash
set -euo pipefail
moments_repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$moments_repo_dir"
exec "${MOMENTS_PYTHON:-python3}" studio/server.py "$@"
