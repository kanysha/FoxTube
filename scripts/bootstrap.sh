#!/usr/bin/env bash
set -Eeuo pipefail

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$project_root"

if [[ $# -gt 0 ]]; then
    if [[ $# -eq 1 && $1 == --help ]]; then
        printf '%s\n' 'Usage: bash scripts/bootstrap.sh' \
            'On clean Ubuntu 24.04: sudo bash scripts/bootstrap.sh' \
            'Existing configuration and volumes are preserved.'
        exit 0
    fi
    printf '%s\n' 'Unsupported option. Use --help.' >&2
    exit 2
fi

install_docker() {
    if [[ $(uname -s) != Linux || ! -r /etc/os-release ]]; then
        printf '%s\n' 'Install and start Docker Engine/Desktop with Compose, then retry.' >&2
        exit 1
    fi
    if [[ $EUID -ne 0 ]]; then
        printf '%s\n' 'Docker installation needs root: sudo bash scripts/bootstrap.sh' >&2
        exit 1
    fi
    # /etc/os-release belongs to the operating system, not the project configuration.
    # shellcheck source=/dev/null
    source /etc/os-release
    if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 24.04 ]]; then
        printf '%s\n' 'Automatic Docker installation supports Ubuntu 24.04 LTS.' >&2
        exit 1
    fi

    local package
    for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
        if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q '^install ok installed$'; then
            printf 'Conflicting package %s is installed; resolve it before retrying.\n' "$package" >&2
            exit 1
        fi
    done

    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    if [[ ! -s /etc/apt/keyrings/docker.asc ]]; then
        curl -fsSL --retry 3 --connect-timeout 10 --max-time 60 \
            https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
    fi
    if [[ ! -e /etc/apt/sources.list.d/docker.sources ]]; then
        cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    fi
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
}

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    install_docker
fi
compose_version=$(docker compose version --short)
compose_version=${compose_version#v}
IFS=. read -r compose_major compose_minor _ <<< "$compose_version"
if [[ ! $compose_major =~ ^[0-9]+$ || ! $compose_minor =~ ^[0-9]+$ ]] || \
    (( 10#$compose_major < 2 || (10#$compose_major == 2 && 10#$compose_minor < 20) )); then
    printf '%s\n' 'Docker Compose 2.20 or newer is required; update Compose and retry.' >&2
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    if [[ $EUID -eq 0 && $(uname -s) == Linux ]] && command -v systemctl >/dev/null 2>&1; then
        systemctl start docker
    fi
fi
if ! docker info >/dev/null 2>&1; then
    printf '%s\n' 'Docker Engine is unavailable. Start Docker or run with access to its socket.' >&2
    exit 1
fi

bash "$project_root/scripts/configure.sh" "$project_root/.env"
docker compose config --quiet
docker compose build db api
docker compose up -d --wait --wait-timeout 120 db
docker compose run --rm --no-deps migrate
docker compose up -d --no-build --wait --wait-timeout 120 db api worker nginx
docker compose exec -T nginx nginx -t
docker compose exec -T nginx nginx -s reload

public_origin=$(docker compose exec -T api python -c 'from app.core.config import get_settings; print(str(get_settings().public_origin).rstrip("/"))')
printf '\nFoxTube: %s\nSwagger: %s/docs\n' "$public_origin" "$public_origin"
printf '%s\n' 'Stop: docker compose down. Persistent data remains in named volumes.'
