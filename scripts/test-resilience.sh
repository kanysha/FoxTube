#!/usr/bin/env bash
set -Eeuo pipefail

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$project_root"
if [[ $# -gt 1 ]]; then
    echo 'Usage: bash scripts/test-resilience.sh [--capacity]' >&2
    exit 2
fi
case "${1:-}" in
    '')
        runner=run.py
        artifact_name=resilience
        ;;
    --capacity)
        runner=capacity.py
        artifact_name=capacity
        ;;
    *)
        echo 'Usage: bash scripts/test-resilience.sh [--capacity]' >&2
        exit 2
        ;;
esac
docker info >/dev/null
docker compose version >/dev/null

umask 077
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/foxtube-resilience.XXXXXX")
test_env="$test_directory/.env"
test_project="foxtube-resilience-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
artifact_directory="$project_root/.artifacts/$artifact_name"
mkdir -p "$artifact_directory"
printf '%s\n' '{"status":"setup","source":"scripts/test-resilience.sh"}' \
    > "$artifact_directory/report.json"

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
        compose logs --no-color --tail 60 api worker nginx >&2
    fi
    if [[ -f "$test_env" ]]; then
        compose down --volumes --remove-orphans --timeout 20
    fi
    rm -f -- "$test_env" "$test_directory/source.mp4"
    rmdir -- "$test_directory"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

bash "$project_root/scripts/configure.sh" "$test_env"
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
python3 "$project_root/tests/resilience/$runner" \
    --project "$test_project" --env-file "$test_env" \
    --source "$test_directory/source.mp4" --artifacts "$artifact_directory"
