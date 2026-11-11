#!/usr/bin/env python3
"""Configure Ubuntu LAN publishing without changing SSH or global firewall policy."""

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

LAN_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
MARKER = "Managed by FoxTube LAN bootstrap"
TABLE = "foxtube_lan"
UNIT = "foxtube-lan-firewall.service"


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        # Compose and subprocess errors can contain configuration secrets.
        raise ValueError(
            f"Command failed: {args[0]} {args[1] if len(args) > 1 else ''}"
        )
    return result.stdout


def select_address(
    addresses: list[dict[str, Any]], route: dict[str, Any], requested: str | None = None
) -> ipaddress.IPv4Interface:
    selected = requested or route.get("prefsrc") or route.get("src")
    wanted = None
    if selected is not None:
        try:
            wanted = ipaddress.IPv4Address(selected)
        except (ipaddress.AddressValueError, TypeError) as error:
            raise ValueError(
                "Cannot select a LAN IPv4 address; use --lan-ip ADDRESS."
            ) from error
    elif not route.get("dev"):
        raise ValueError("Cannot select a LAN interface; use --lan-ip ADDRESS.")
    candidates = []
    for interface in addresses:
        if "UP" not in interface.get("flags", []):
            continue
        if not requested and interface.get("ifname") != route.get("dev"):
            continue
        for address in interface.get("addr_info", []):
            if address.get("family") != "inet" or address.get("scope") != "global":
                continue
            value = ipaddress.IPv4Interface(
                f"{address['local']}/{address['prefixlen']}"
            )
            if wanted is None or value.ip == wanted:
                candidates.append(value)
    if len(candidates) != 1 or not any(
        candidates[0].network.subnet_of(network) for network in LAN_NETWORKS
    ):
        raise ValueError(
            "LAN address must belong to one connected RFC1918 IPv4 subnet."
        )
    if candidates[0].network.prefixlen > 30:
        raise ValueError(
            "LAN subnet needs usable client addresses (prefix at most /30)."
        )
    return candidates[0]


def network_settings(original: str) -> dict[str, str]:
    keys = {
        "FOXTUBE_BIND",
        "FOXTUBE_PORT",
        "FOXTUBE_PUBLIC_ORIGIN",
        "FOXTUBE_LAN_LISTEN_ALL",
        "FOXTUBE_LAN_ALLOW_SUBNETS",
        "FOXTUBE_LAN_ALLOW_ANY_SOURCE",
        "FOXTUBE_LAN_TCP_MSS",
    }
    settings: dict[str, str] = {}
    for line in original.splitlines():
        key, separator, value = line.partition("=")
        if key in keys and separator:
            if key in settings:
                raise ValueError("Duplicate network setting in .env.")
            settings[key] = value
    return settings


def resolve_policy(
    original: str,
    listen_all: bool = False,
    allowed_subnets: list[str] | None = None,
    allow_any_source: bool = False,
) -> tuple[bool, tuple[ipaddress.IPv4Network, ...], bool]:
    settings = network_settings(original)
    persisted_all = settings.get("FOXTUBE_LAN_LISTEN_ALL", "false")
    if persisted_all not in {"true", "false"}:
        raise ValueError("FOXTUBE_LAN_LISTEN_ALL must be true or false.")
    persisted_any = settings.get("FOXTUBE_LAN_ALLOW_ANY_SOURCE", "false")
    if persisted_any not in {"true", "false"}:
        raise ValueError("FOXTUBE_LAN_ALLOW_ANY_SOURCE must be true or false.")
    allow_any_source = allow_any_source or persisted_any == "true"
    if allow_any_source and allowed_subnets:
        raise ValueError("--allow-any-source cannot be combined with --allow-subnet.")
    values = allowed_subnets
    if values is None:
        values = (
            []
            if allow_any_source
            else settings.get("FOXTUBE_LAN_ALLOW_SUBNETS", "").split(",")
        )
    if allowed_subnets is None and values == [""]:
        values = []
    if len(values) > 16:
        raise ValueError("At most 16 additional source subnets are supported.")
    networks = []
    for value in values:
        try:
            network = ipaddress.IPv4Network(value, strict=True)
        except ValueError as error:
            raise ValueError(
                "Allowed sources must be canonical RFC1918 IPv4 CIDRs."
            ) from error
        if str(network) != value:
            raise ValueError("Allowed sources must use canonical IPv4 CIDR notation.")
        if not any(network.subnet_of(private) for private in LAN_NETWORKS):
            raise ValueError("Allowed sources must stay within RFC1918 IPv4 networks.")
        if network not in networks:
            networks.append(network)
    return listen_all or persisted_all == "true", tuple(networks), allow_any_source


def resolve_tcp_mss(original: str, tcp_mss: str | None = None) -> int | None:
    value = network_settings(original).get("FOXTUBE_LAN_TCP_MSS", "")
    if tcp_mss is None and value == "":
        return None
    if tcp_mss is not None:
        value = tcp_mss
    if not re.fullmatch(r"[1-9][0-9]{2,3}", value) or not 536 <= int(value) <= 1460:
        raise ValueError("TCP MSS must be an integer between 536 and 1460 bytes.")
    return int(value)


def prepare_env(
    original: str,
    address: ipaddress.IPv4Interface,
    listen_all: bool = False,
    allowed_subnets: list[str] | None = None,
    allow_any_source: bool = False,
    tcp_mss: str | None = None,
) -> tuple[str, int]:
    settings = network_settings(original)
    resolved_mss = resolve_tcp_mss(original, tcp_mss)
    listen_all, networks, allow_any_source = resolve_policy(
        original, listen_all, allowed_subnets, allow_any_source
    )
    port_value = settings.get("FOXTUBE_PORT", "8080")
    if not re.fullmatch(r"[1-9][0-9]{0,4}", port_value):
        raise ValueError("FOXTUBE_PORT must be an integer between 1024 and 65535.")
    port = int(port_value)
    if not 1024 <= port <= 65535:
        raise ValueError("LAN application port must be between 1024 and 65535.")
    ip = str(address.ip)
    origin = f"http://{ip}:{port}"
    bind = settings.get("FOXTUBE_BIND", "127.0.0.1")
    previous_origin = settings.get("FOXTUBE_PUBLIC_ORIGIN", f"http://localhost:{port}")
    previous_endpoints = {
        ("127.0.0.1", f"http://localhost:{port}"),
        ("127.0.0.1", f"http://127.0.0.1:{port}"),
        (ip, origin),
    }
    if listen_all:
        previous_endpoints.add(("0.0.0.0", origin))
    if (bind, previous_origin) not in previous_endpoints:
        raise ValueError(
            "Custom bind/origin preserved. Review it or use --no-lan "
            "for manual networking."
        )
    replacements = {
        "FOXTUBE_BIND": "0.0.0.0" if listen_all else ip,
        "FOXTUBE_PORT": str(port),
        "FOXTUBE_PUBLIC_ORIGIN": origin,
    }
    if resolved_mss is not None or "FOXTUBE_LAN_TCP_MSS" in settings:
        replacements["FOXTUBE_LAN_TCP_MSS"] = (
            "" if resolved_mss is None else str(resolved_mss)
        )
    if (
        listen_all
        or networks
        or allow_any_source
        or any(
            key in settings
            for key in (
                "FOXTUBE_LAN_LISTEN_ALL",
                "FOXTUBE_LAN_ALLOW_SUBNETS",
                "FOXTUBE_LAN_ALLOW_ANY_SOURCE",
            )
        )
    ):
        replacements.update(
            FOXTUBE_LAN_LISTEN_ALL=str(listen_all).lower(),
            FOXTUBE_LAN_ALLOW_SUBNETS=",".join(map(str, networks)),
            FOXTUBE_LAN_ALLOW_ANY_SOURCE=str(allow_any_source).lower(),
        )
    lines = []
    seen = set()
    for line in original.splitlines(keepends=True):
        key = line.partition("=")[0]
        if key in replacements:
            lines.append(f"{key}={replacements[key]}\n")
            seen.add(key)
        else:
            lines.append(line)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.extend(
        f"{key}={value}\n" for key, value in replacements.items() if key not in seen
    )
    return "".join(lines), port


def validate_compose(
    config: dict[str, Any],
    address: ipaddress.IPv4Interface,
    port: int,
    listen_all: bool = False,
) -> None:
    services = config.get("services", {})
    mappings = []
    for name, service in services.items():
        if service.get("network_mode") == "host":
            raise ValueError(
                "Host networking is incompatible with managed LAN publishing."
            )
        for mapping in service.get("ports", []):
            mappings.append((name, mapping))
    if len(mappings) != 1:
        raise ValueError("Only the Nginx application port may be published.")
    name, mapping = mappings[0]
    if (
        name != "nginx"
        or mapping.get("host_ip") != ("0.0.0.0" if listen_all else str(address.ip))
        or str(mapping.get("published")) != str(port)
        or mapping.get("target") != 8080
        or mapping.get("protocol", "tcp") != "tcp"
    ):
        raise ValueError(
            "Effective Compose port differs from the protected LAN endpoint."
        )
    origin = services.get("api", {}).get("environment", {}).get("FOXTUBE_PUBLIC_ORIGIN")
    if origin != f"http://{address.ip}:{port}":
        raise ValueError("Effective PUBLIC_ORIGIN differs from the LAN endpoint.")


def render_rules(
    address: ipaddress.IPv4Interface,
    port: int,
    listen_all: bool = False,
    allowed_subnets: tuple[ipaddress.IPv4Network, ...] = (),
    allow_any_source: bool = False,
    tcp_mss: int | None = None,
) -> str:
    sources = list(ipaddress.collapse_addresses((address.network, *allowed_subnets)))
    allowed = ", ".join(map(str, sources))
    destination = "fib daddr type local" if listen_all else f"ip daddr {address.ip}"
    drop_rule = (
        f"{destination} tcp dport {port} "
        f'ip saddr != {{ {allowed} }} counter drop comment "{MARKER}"'
    )
    if allow_any_source:
        drop_rule = f'{destination} tcp dport {port} counter return comment "{MARKER}"'
    mss_chain = ""
    if tcp_mss is not None:
        resolve_tcp_mss("", str(tcp_mss))
        mss_rule = (
            f"meta nfproto ipv4 {destination} tcp dport {port} "
            "tcp flags & (syn | rst) == syn "
            f"tcp option maxseg size > {tcp_mss} "
            f'tcp option maxseg size set {tcp_mss} counter comment "{MARKER}"'
        )
        mss_chain = f"""    chain tcp_mss {{
        type filter hook prerouting priority mangle; policy accept;
        {mss_rule}
    }}
"""
    return f"""# {MARKER}
add table inet {TABLE}
flush table inet {TABLE}
table inet {TABLE} {{
    chain ingress {{
        type filter hook prerouting priority -110; policy accept;
        ct direction reply return comment "{MARKER}"
        {drop_rule}
    }}
{mss_chain}}}
"""


def validate_owned_table(data: dict[str, Any]) -> None:
    # Jammy's kernel/nft combination does not retain table comments. Rule
    # comments survive both a reload and reboot and identify our own table.
    # Older versions left this single reply rule unmarked. Accept its exact
    # expression during migration, but preserve tables with any foreign rule.
    reply_expression = [
        {"match": {"op": "==", "left": {"ct": {"key": "direction"}}, "right": "reply"}},
        {"return": None},
    ]
    # A flush also removes named objects and empty chains. Rule markers alone
    # must not authorize deleting additions made by another administrator.
    rules = []
    chains = set()
    tables = 0
    for item in data["nftables"]:
        if len(item) != 1:
            raise ValueError("Existing unmanaged nft table preserved.")
        kind, value = next(iter(item.items()))
        if kind == "metainfo":
            continue
        if kind == "rule":
            rules.append(value)
            continue
        if kind == "table":
            tables += 1
            expected = {"family": "inet", "name": TABLE}
        elif kind == "chain" and value.get("name") in {"ingress", "tcp_mss"}:
            name = value["name"]
            if name in chains:
                raise ValueError("Existing unmanaged nft table preserved.")
            chains.add(name)
            expected = {
                "family": "inet",
                "table": TABLE,
                "name": name,
                "type": "filter",
                "hook": "prerouting",
                "prio": -110 if name == "ingress" else -150,
                "policy": "accept",
            }
        else:
            raise ValueError("Existing unmanaged nft table preserved.")
        actual = {key: content for key, content in value.items() if key != "handle"}
        if actual.get("comment") == MARKER:
            del actual["comment"]
        if actual != expected:
            raise ValueError("Existing unmanaged nft table preserved.")
    if tables != 1 or "ingress" not in chains:
        raise ValueError("Existing unmanaged nft table preserved.")
    for rule in rules:
        legacy_reply = (
            rule.get("chain") == "ingress"
            and rule.get("expr") == reply_expression
            and "comment" not in rule
        )
        if (
            rule.get("family") != "inet"
            or rule.get("table") != TABLE
            or rule.get("chain") not in chains
            or (rule.get("comment") != MARKER and not legacy_reply)
        ):
            raise ValueError("Existing unmanaged nft table preserved.")
    ingress = [rule for rule in rules if rule.get("chain") == "ingress"]
    replies = [rule for rule in ingress if rule.get("expr") == reply_expression]
    mss_rules = [rule for rule in rules if rule.get("chain") == "tcp_mss"]
    if (
        len(ingress) - len(replies) != 1
        or len(replies) > 1
        or len(mss_rules) != (1 if "tcp_mss" in chains else 0)
    ):
        raise ValueError("Existing unmanaged nft table preserved.")


def validate_environment() -> None:
    for key in (
        "COMPOSE_ENV_FILES",
        "COMPOSE_DISABLE_ENV_FILE",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
    ):
        if key in os.environ:
            raise ValueError(f"Unset {key} before automatic LAN setup.")


def validate_engine() -> None:
    context = run("docker", "context", "show").strip()
    data = json.loads(run("docker", "context", "inspect", context))[0]
    endpoint = data.get("Endpoints", {}).get("docker", {}).get("Host", "")
    if (
        not endpoint.startswith("unix://")
        or Path(endpoint[7:]).resolve() != Path("/var/run/docker.sock").resolve()
    ):
        raise ValueError("Automatic LAN setup requires the local Docker Engine socket.")


def check_owned_path(path: Path, managed: bool = False) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing symlink: {path}")
    if not path.exists():
        return
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError(
            f"System path must be root-owned and not writable by others: {path}"
        )
    if managed:
        first_line = path.read_text().splitlines()[:1] if path.is_file() else []
        if first_line != [f"# {MARKER}"]:
            raise ValueError(f"Existing unmanaged system file preserved: {path}")


def write_system_file(path: Path, contents: str) -> None:
    for parent in reversed(path.parents):
        check_owned_path(parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    check_owned_path(path, managed=True)
    atomic_write(path, contents, 0o644)


def atomic_write(path: Path, contents: str, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".foxtube-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
            if path.exists():
                info = path.stat()
                os.fchown(output.fileno(), info.st_uid, info.st_gid)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_listener(
    address: ipaddress.IPv4Interface, port: int, listen_all: bool = False
) -> None:
    expected_bind = ipaddress.IPv4Address("0.0.0.0") if listen_all else address.ip
    own_bindings = set()
    container = run("docker", "compose", "ps", "-q", "nginx").strip()
    if container:
        data = json.loads(run("docker", "inspect", container))[0]
        for bindings in data.get("NetworkSettings", {}).get("Ports", {}).values():
            for binding in bindings or []:
                bound_ip = ipaddress.ip_address(binding["HostIp"])
                if not bound_ip.is_loopback and (
                    bound_ip != expected_bind or binding["HostPort"] != str(port)
                ):
                    raise ValueError(
                        "Active LAN endpoint differs; stop Nginx "
                        "before changing its IP/port."
                    )
                if binding["HostPort"] == str(port):
                    own_bindings.add(f"{binding['HostIp']}:{port}")
    for line in run("ss", "-H", "-lntp").splitlines():
        fields = line.split()
        if len(fields) < 4 or not fields[3].endswith(f":{port}"):
            continue
        if '"docker-proxy"' not in line or fields[3] not in own_bindings:
            raise ValueError(
                "Application port already belongs to another listener; preserved."
            )


def configure(
    env_path: Path,
    requested: str | None,
    listen_all: bool = False,
    allowed_subnets: list[str] | None = None,
    allow_any_source: bool = False,
    tcp_mss: str | None = None,
) -> None:
    if os.geteuid() != 0:
        raise ValueError("Ubuntu LAN setup needs root: sudo bash scripts/bootstrap.sh")
    validate_environment()
    validate_engine()
    if env_path.is_symlink() or not env_path.is_file():
        raise ValueError("Configuration must be a regular file, not a symlink.")
    check_owned_path(env_path)
    original = env_path.read_text()
    resolved_mss = resolve_tcp_mss(original, tcp_mss)
    listen_all, networks, allow_any_source = resolve_policy(
        original, listen_all, allowed_subnets, allow_any_source
    )
    addresses = json.loads(run("ip", "-j", "-4", "address", "show", "scope", "global"))
    route = (
        {}
        if requested
        else json.loads(run("ip", "-j", "-4", "route", "get", "1.1.1.1"))[0]
    )
    address = select_address(addresses, route, requested)
    candidate, port = prepare_env(
        original,
        address,
        listen_all,
        list(map(str, networks)),
        allow_any_source,
        tcp_mss,
    )
    descriptor, temporary = tempfile.mkstemp(
        prefix=".foxtube-lan-", dir=env_path.parent
    )
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(candidate)
        effective = json.loads(
            run(
                "docker",
                "compose",
                "--env-file",
                temporary,
                "config",
                "--format",
                "json",
            )
        )
        validate_compose(effective, address, port, listen_all)
    finally:
        Path(temporary).unlink(missing_ok=True)
    validate_listener(address, port, listen_all)
    nft = shutil.which("nft")
    if nft != "/usr/sbin/nft":
        raise ValueError("Expected Ubuntu nft executable at /usr/sbin/nft.")
    existing = subprocess.run(
        [nft, "-j", "list", "table", "inet", TABLE], capture_output=True, text=True
    )
    if existing.returncode == 0:
        validate_owned_table(json.loads(existing.stdout))
    rule_path = Path("/etc/foxtube/lan.nft")
    unit_path = Path("/etc/systemd/system") / UNIT
    dropin_path = Path("/etc/systemd/system/docker.service.d/foxtube-lan.conf")
    rules = render_rules(
        address, port, listen_all, networks, allow_any_source, resolved_mss
    )
    unit = f"""# {MARKER}
[Unit]
Description=FoxTube LAN port filter
After=nftables.service ufw.service
Before=docker.service

[Service]
Type=oneshot
ExecStart=/usr/sbin/nft -f /etc/foxtube/lan.nft
ExecReload=/usr/sbin/nft -f /etc/foxtube/lan.nft
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
"""
    dropin = f"""# {MARKER}
[Unit]
Requires={UNIT}
After={UNIT}
"""
    # Validate all owned destinations before replacing any file.
    for path in (rule_path, unit_path, dropin_path):
        for parent in reversed(path.parents):
            check_owned_path(parent)
        check_owned_path(path, managed=True)
    check = subprocess.run(
        [nft, "--check", "-f", "-"], input=rules, capture_output=True, text=True
    )
    if check.returncode:
        raise ValueError("LAN firewall validation failed; existing rules preserved.")
    write_system_file(rule_path, rules)
    write_system_file(unit_path, unit)
    write_system_file(dropin_path, dropin)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", UNIT)
    run("systemctl", "reload-or-restart", UNIT)
    # Do not overwrite edits made while Docker/network validation was running.
    if env_path.read_text() != original or env_path.is_symlink():
        raise ValueError("Configuration changed during LAN setup; retry.")
    if candidate != original:
        atomic_write(env_path, candidate, 0o600)
    else:
        env_path.chmod(0o600)
    bind = "0.0.0.0" if listen_all else str(address.ip)
    print(f"LAN: http://{address.ip}:{port}; bind: {bind}")
    if allow_any_source:
        print("Allowed sources: any IPv4 address (explicit opt-in).")
    else:
        sources = ", ".join(map(str, (address.network, *networks)))
        print(f"Allowed sources: {sources}")
    print("Only Nginx is published. SSH and existing UFW settings are unchanged.")
    if resolved_mss is not None:
        print(f"Optional IPv4 web TCP MSS clamp: {resolved_mss} bytes.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_path", type=Path)
    parser.add_argument("--lan-ip")
    parser.add_argument("--listen-all", action="store_true")
    parser.add_argument("--allow-subnet", action="append")
    parser.add_argument("--allow-any-source", action="store_true")
    parser.add_argument("--tcp-mss")
    arguments = parser.parse_args()
    try:
        configure(
            arguments.env_path,
            arguments.lan_ip,
            arguments.listen_all,
            arguments.allow_subnet,
            arguments.allow_any_source,
            arguments.tcp_mss,
        )
    except (ValueError, OSError, KeyError, IndexError) as error:
        print(f"LAN setup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
