#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
ruff check app migrations tests scripts
ruff format --check app migrations tests scripts
mypy app
shellcheck scripts/*.sh deploy/postgres/*.sh
pytest -m 'not integration'
