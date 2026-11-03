#!/usr/bin/env bash
set -euo pipefail

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
env_path=${1:-"$project_root/.env"}

if [[ $# -gt 1 ]]; then
    printf '%s\n' 'Usage: bash scripts/configure.sh [env-file]' >&2
    exit 2
fi
if [[ -L "$env_path" ]]; then
    printf '%s\n' 'Refusing to use a symbolic link as configuration.' >&2
    exit 1
fi
if [[ -e "$env_path" ]]; then
    if [[ ! -f "$env_path" ]]; then
        printf '%s\n' 'Configuration path must be a regular file.' >&2
        exit 1
    fi
    chmod 600 "$env_path"
    printf '%s\n' 'Existing configuration preserved.'
    exit 0
fi

umask 077
env_directory=$(dirname -- "$env_path")
mkdir -p -- "$env_directory"
temporary=$(mktemp "$env_directory/.foxtube-env.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT

random_password() {
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
}

{
    printf '%s\n' 'POSTGRES_DB=foxtube' 'POSTGRES_USER=foxtube_migrator'
    printf 'POSTGRES_PASSWORD=%s\n' "$(random_password)"
    printf 'API_DB_PASSWORD=%s\n' "$(random_password)"
    printf 'WORKER_DB_PASSWORD=%s\n' "$(random_password)"
    printf '%s\n' \
        'FOXTUBE_ENV=development' \
        'FOXTUBE_BIND=127.0.0.1' \
        'FOXTUBE_PORT=8080' \
        'FOXTUBE_PUBLIC_ORIGIN=http://localhost:8080' \
        'FOXTUBE_IMAGE_TAG=local'
} > "$temporary"

# The temporary file lives on the same filesystem. -n preserves a concurrent creator.
mv -n -- "$temporary" "$env_path"
if [[ ! -f "$env_path" || -L "$env_path" ]]; then
    printf '%s\n' 'Could not create a regular configuration file.' >&2
    exit 1
fi
chmod 600 "$env_path"
printf '%s\n' 'Configuration created; passwords remain in the private env file.'
