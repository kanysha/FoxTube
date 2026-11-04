#!/usr/bin/env bash
set -Eeuo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$project_root"
umask 077
mkdir -p -- "$project_root/secrets/demo"
chmod 700 -- "$project_root/secrets/demo"
docker compose run --rm --no-deps --user "$(id -u):$(id -g)" \
    -v "$project_root/secrets/demo:/run/foxtube-demo" api \
    python -m app.demo --manifest /run/foxtube-demo/manifest.json
printf '%s\n' 'Demo credentials: secrets/demo/manifest.json (private local file; do not publish).'
