#!/usr/bin/env bash
set -Eeuo pipefail

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$project_root"
docker info >/dev/null
docker compose version >/dev/null

umask 077
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/foxtube-test.XXXXXX")
test_env="$test_directory/.env"
test_project="foxtube-test-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"

compose() {
    docker compose --project-name "$test_project" --env-file "$test_env" \
        -f "$project_root/compose.yaml" -f "$project_root/compose.test.yaml" \
        --profile test "$@"
}

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    set +e
    if [[ $status -ne 0 && -f "$test_env" ]]; then
        compose logs --no-color --tail 100 db api worker nginx >&2
    fi
    if [[ -f "$test_env" ]]; then
        compose down --volumes --remove-orphans --timeout 20
    fi
    rm -f -- "$test_env"
    rmdir -- "$test_directory"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

bash "$project_root/scripts/configure.sh" "$test_env"
# Only the temporary file supplies database identities and secrets in this run.
unset POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD API_DB_PASSWORD WORKER_DB_PASSWORD
unset FOXTUBE_IMAGE_TAG
export FOXTUBE_ENV=test
export FOXTUBE_BIND=127.0.0.1
export FOXTUBE_PORT=0
export FOXTUBE_PUBLIC_ORIGIN=http://nginx:8080
compose config --quiet
compose build db test
compose up -d --wait --wait-timeout 120 db
compose run --rm --no-deps migrate
compose up -d --no-build --wait --wait-timeout 120 db api worker nginx
compose run --rm --no-deps test pytest -m integration "$@"
