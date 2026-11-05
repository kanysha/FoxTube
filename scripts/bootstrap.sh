#!/usr/bin/env bash
set -Eeuo pipefail

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$project_root"

with_demo=false
with_lan=true
lan_ip=''
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help)
            printf '%s\n' 'Usage: bash scripts/bootstrap.sh [--demo] [--no-lan | --lan-ip ADDRESS]' \
                'On clean Ubuntu 22.04/24.04: sudo bash scripts/bootstrap.sh' \
                'Root Ubuntu installs publish only Nginx to the connected private LAN.' \
                '--no-lan skips automatic networking; it does not remove existing rules.' \
                'Passwords and persistent volumes are preserved.'
            exit 0
            ;;
        --demo) with_demo=true; shift ;;
        --no-lan) with_lan=false; shift ;;
        --lan-ip)
            if [[ $# -lt 2 || -z $2 ]]; then
                printf '%s\n' '--lan-ip requires an assigned private IPv4 address.' >&2
                exit 2
            fi
            lan_ip=$2
            shift 2
            ;;
        *) printf '%s\n' 'Unsupported option. Use --help.' >&2; exit 2 ;;
    esac
done
if [[ $with_lan == false && -n $lan_ip ]]; then
    printf '%s\n' '--no-lan and --lan-ip cannot be combined.' >&2
    exit 2
fi

supported_ubuntu_codename() {
    case "$1:$2:$3" in
        ubuntu:22.04:jammy|ubuntu:24.04:noble)
            printf '%s\n' "$3"
            ;;
        *)
            printf '%s\n' 'Automatic Docker installation supports Ubuntu 22.04 (jammy) and 24.04 (noble).' >&2
            return 1
            ;;
    esac
}

repository_file_matches() {
    local candidate=$1 destination=$2
    if [[ -e $destination || -L $destination ]]; then
        if [[ ! -f $destination || -L $destination ]] || ! cmp -s -- "$candidate" "$destination"; then
            printf 'Existing Docker repository file differs or is unsafe: %s. Review it before retrying.\n' "$destination" >&2
            return 1
        fi
    fi
}

publish_repository_file() {
    local candidate=$1 destination=$2
    repository_file_matches "$candidate" "$destination"
    if [[ ! -e $destination && ! -L $destination ]]; then
        # A hard link publishes the complete file atomically without overwriting
        # another installer's file. -T also rejects a concurrent directory.
        ln -T -- "$candidate" "$destination"
    fi
}

configure_docker_repository() (
    # Keep cleanup traps local to this installation step, including curl failure.
    local codename=$1 apt_directory=$2 key_temporary='' source_temporary='' architecture
    local key_path="$apt_directory/keyrings/docker.asc"
    local source_path="$apt_directory/sources.list.d/docker.sources"
    trap 'rm -f -- "$key_temporary" "$source_temporary"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    install -m 0755 -d "$apt_directory/keyrings" "$apt_directory/sources.list.d"
    key_temporary=$(mktemp "$apt_directory/keyrings/.foxtube-docker-key.XXXXXX")
    source_temporary=$(mktemp "$apt_directory/sources.list.d/.foxtube-docker-source.XXXXXX")
    curl -fsSL --retry 3 --connect-timeout 10 --max-time 60 \
        https://download.docker.com/linux/ubuntu/gpg -o "$key_temporary"
    if [[ ! -s $key_temporary ]] || \
        ! grep -q '^-----BEGIN PGP PUBLIC KEY BLOCK-----$' "$key_temporary" || \
        ! grep -q '^-----END PGP PUBLIC KEY BLOCK-----$' "$key_temporary"; then
        printf '%s\n' 'The downloaded Docker signing key is incomplete; retry installation.' >&2
        exit 1
    fi
    architecture=$(dpkg --print-architecture)
    cat > "$source_temporary" <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $codename
Components: stable
Architectures: $architecture
Signed-By: $key_path
EOF
    chmod 0644 "$key_temporary" "$source_temporary"
    # Check both existing files before publishing either new file.
    repository_file_matches "$key_temporary" "$key_path"
    repository_file_matches "$source_temporary" "$source_path"
    publish_repository_file "$key_temporary" "$key_path"
    publish_repository_file "$source_temporary" "$source_path"
)

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
    local codename
    codename=$(supported_ubuntu_codename "${ID:-}" "${VERSION_ID:-}" \
        "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}")

    local package
    for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
        if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q '^install ok installed$'; then
            printf 'Conflicting package %s is installed; resolve it before retrying.\n' "$package" >&2
            exit 1
        fi
    done

    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl
    configure_docker_repository "$codename" /etc/apt
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
}

# Regression tests source the helpers with stubbed tools and temporary apt paths.
if [[ ${BASH_SOURCE[0]} != "$0" ]]; then
    return
fi

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
if [[ $with_lan == true && $(uname -s) == Linux && -r /etc/os-release ]]; then
    # This is the operating system's own file, never the project .env.
    # shellcheck source=/dev/null
    source /etc/os-release
    if [[ ${ID:-} == ubuntu ]]; then
        if [[ $EUID -ne 0 ]]; then
            printf '%s\n' 'LAN setup needs root. Use sudo, or --no-lan for manual networking.' >&2
            exit 1
        fi
        if ! command -v python3 >/dev/null 2>&1 || ! command -v nft >/dev/null 2>&1; then
            apt-get update
            DEBIAN_FRONTEND=noninteractive apt-get install -y python3 nftables
        fi
        lan_arguments=("$project_root/.env")
        if [[ -n $lan_ip ]]; then
            lan_arguments+=(--lan-ip "$lan_ip")
        fi
        python3 "$project_root/scripts/configure_lan.py" "${lan_arguments[@]}"
    elif [[ -n $lan_ip ]]; then
        printf '%s\n' 'Automatic LAN setup supports Ubuntu only.' >&2
        exit 1
    fi
elif [[ -n $lan_ip ]]; then
    printf '%s\n' 'Automatic LAN setup requires root Ubuntu.' >&2
    exit 1
fi
docker compose config --quiet
docker compose build db api
docker compose up -d --wait --wait-timeout 120 db
docker compose run --rm --no-deps migrate
docker compose up -d --no-build --wait --wait-timeout 120 db api worker nginx
docker compose exec -T nginx nginx -t
docker compose exec -T nginx nginx -s reload

if [[ $with_demo == true ]]; then
    bash "$project_root/scripts/demo.sh"
fi

public_origin=$(docker compose exec -T api python -c 'from app.core.config import get_settings; print(str(get_settings().public_origin).rstrip("/"))')
printf '\nFoxTube: %s\nSwagger: %s/docs\n' "$public_origin" "$public_origin"
printf '%s\n' 'Stop: docker compose down. Persistent data remains in named volumes.'
