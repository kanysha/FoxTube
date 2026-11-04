#!/usr/bin/env bash
set -Eeuo pipefail

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$project_root"
docker info >/dev/null
docker compose version >/dev/null

umask 077
test_directory=$(mktemp -d "${TMPDIR:-/tmp}/foxtube-browser.XXXXXX")
test_env="$test_directory/.env"
test_project="foxtube-browser-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
browser_container="$test_project-result"
artifact_directory="$project_root/.artifacts/video-browser"
mkdir -p "$artifact_directory"
# A setup failure must never leave the previous run's successful report current.
printf '%s\n' '{"status":"setup","source":"scripts/test-browser.sh"}' \
    > "$artifact_directory/report.json"

compose() {
    docker compose --project-name "$test_project" --env-file "$test_env" \
        -f "$project_root/compose.yaml" -f "$project_root/compose.test.yaml" \
        -f "$project_root/compose.browser.yaml" --profile browser "$@"
}

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    set +e
    if docker container inspect "$browser_container" >/dev/null 2>&1; then
        docker cp "$browser_container:/artifacts/." "$artifact_directory/"
        docker container rm --force "$browser_container" >/dev/null
    elif [[ $status -ne 0 ]]; then
        printf '%s\n' '{"status":"failed","phase":"setup"}' \
            > "$artifact_directory/report.json"
    fi
    if [[ $status -ne 0 && -f "$test_env" ]]; then
        compose logs --no-color --tail 60 api worker nginx >&2
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
unset POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD API_DB_PASSWORD WORKER_DB_PASSWORD
unset FOXTUBE_IMAGE_TAG
export FOXTUBE_ENV=test
export FOXTUBE_BIND=127.0.0.1
export FOXTUBE_PORT=0
export FOXTUBE_PUBLIC_ORIGIN=http://nginx:8080

compose config --quiet
compose build db api browser
compose up -d --wait --wait-timeout 120 db
compose run --rm --no-deps migrate
compose up -d --no-build --wait --wait-timeout 120 db api worker nginx
compose exec -T api python - < "$project_root/tests/browser/staff.py"
compose run --rm --no-deps migrate \
    python -m app.moderation.bootstrap --email browser-admin@example.com
compose run --name "$browser_container" --no-deps browser
