"""LAN installer policy checks without root, host networking, or real secrets."""

import json
import os
import runpy
from copy import deepcopy
from ipaddress import IPv4Interface, ip_interface
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/configure_lan.py"
ADDRESS = IPv4Interface("192.168.50.20/24")
AMBIENT_NETWORK_KEYS = (
    "COMPOSE_ENV_FILES",
    "COMPOSE_DISABLE_ENV_FILE",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
)


@pytest.fixture(scope="module")
def lan() -> SimpleNamespace:
    # The script's __main__ branch must not execute during policy tests.
    return SimpleNamespace(**runpy.run_path(str(SCRIPT), run_name="lan_unit_tests"))


def _nic(name: str, *addresses: str, up: bool = True) -> dict:
    parsed = [ip_interface(address) for address in addresses]
    return {
        "ifname": name,
        "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"] if up else [],
        "operstate": "UP" if up else "DOWN",
        "addr_info": [
            {
                "family": "inet" if address.version == 4 else "inet6",
                "local": str(address.ip),
                "prefixlen": address.network.prefixlen,
                "scope": "global",
            }
            for address in parsed
        ],
    }


@pytest.mark.parametrize(
    "address", ["10.20.30.40/16", "172.20.30.40/20", "192.168.50.20/24"]
)
def test_select_address_preserves_assigned_ip_and_prefix(
    lan: SimpleNamespace, address: str
) -> None:
    expected = IPv4Interface(address)
    selected = lan.select_address(
        [_nic("ens18", address)], {"dev": "ens18", "prefsrc": str(expected.ip)}
    )
    assert isinstance(selected, IPv4Interface)
    assert selected == expected


def test_default_route_selects_interface_and_preferred_source(
    lan: SimpleNamespace,
) -> None:
    addresses = [
        _nic("docker0", "172.17.0.1/16"),
        _nic("ens18", "192.168.50.19/24", str(ADDRESS)),
        _nic("ens19", "10.90.0.2/16"),
    ]
    assert (
        lan.select_address(addresses, {"dev": "ens18", "prefsrc": str(ADDRESS.ip)})
        == ADDRESS
    )


def test_explicit_address_overrides_default_route(lan: SimpleNamespace) -> None:
    addresses = [_nic("ens18", str(ADDRESS)), _nic("ens19", "10.90.0.2/16")]
    assert lan.select_address(
        addresses,
        {"dev": "ens18", "prefsrc": str(ADDRESS.ip)},
        requested="10.90.0.2",
    ) == IPv4Interface("10.90.0.2/16")


def test_route_interface_without_prefsrc_can_select_unique_address(
    lan: SimpleNamespace,
) -> None:
    assert (
        lan.select_address(
            [_nic("ens18", str(ADDRESS)), _nic("ens19", "10.90.0.2/16")],
            {"dev": "ens18"},
        )
        == ADDRESS
    )


def test_ambiguous_addresses_require_route_or_override(lan: SimpleNamespace) -> None:
    with pytest.raises(ValueError):
        lan.select_address(
            [_nic("ens18", str(ADDRESS)), _nic("ens19", "10.90.0.2/16")], {}
        )


@pytest.mark.parametrize(
    "requested", ["192.168.50.99", "127.0.0.1", "8.8.8.8", "::1", "invalid"]
)
def test_explicit_address_must_be_assigned_rfc1918_ipv4(
    lan: SimpleNamespace, requested: str
) -> None:
    with pytest.raises(ValueError):
        lan.select_address(
            [_nic("ens18", str(ADDRESS))],
            {"dev": "ens18", "prefsrc": str(ADDRESS.ip)},
            requested=requested,
        )


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8/24",
        "100.64.0.2/10",
        "127.0.0.1/8",
        "169.254.1.2/16",
        "172.15.0.2/16",
        "172.32.0.2/16",
        "192.0.2.2/24",
        "198.18.0.2/15",
        "224.0.0.2/24",
        "fd00::2/64",
    ],
)
def test_non_rfc1918_candidates_are_rejected(
    lan: SimpleNamespace, address: str
) -> None:
    with pytest.raises(ValueError):
        lan.select_address(
            [_nic("ens18", address)],
            {"dev": "ens18", "prefsrc": str(ip_interface(address).ip)},
        )


def test_down_interface_is_not_a_lan_candidate(lan: SimpleNamespace) -> None:
    with pytest.raises(ValueError):
        lan.select_address(
            [_nic("ens18", str(ADDRESS), up=False)],
            {"dev": "ens18", "prefsrc": str(ADDRESS.ip)},
        )


def _env(bind: str = "127.0.0.1", port: str = "8080", origin: str | None = None) -> str:
    if origin is None:
        origin = f"http://localhost:{port}"
    return (
        "# Keep comments and unrelated configuration verbatim.\n"
        "POSTGRES_PASSWORD=synthetic-fixture-only\n"
        "FOXTUBE_ENV=development\n"
        f"FOXTUBE_BIND={bind}\n"
        f"FOXTUBE_PORT={port}\n"
        f"FOXTUBE_PUBLIC_ORIGIN={origin}\n"
        "UNRELATED_VALUE=literal-$()-not-a-command\n"
    )


@pytest.mark.parametrize("localhost", ["localhost", "127.0.0.1"])
@pytest.mark.parametrize("port", [1024, 8080, 65535])
def test_prepare_env_migrates_loopback_and_preserves_other_lines(
    lan: SimpleNamespace, localhost: str, port: int
) -> None:
    original = _env(port=str(port), origin=f"http://{localhost}:{port}")
    updated, selected_port = lan.prepare_env(original, ADDRESS)
    assert selected_port == port
    assert f"FOXTUBE_BIND={ADDRESS.ip}\n" in updated
    assert f"FOXTUBE_PUBLIC_ORIGIN=http://{ADDRESS.ip}:{port}\n" in updated
    network_keys = ("FOXTUBE_BIND=", "FOXTUBE_PUBLIC_ORIGIN=")
    assert [
        line
        for line in updated.splitlines(keepends=True)
        if not line.startswith(network_keys)
    ] == [
        line
        for line in original.splitlines(keepends=True)
        if not line.startswith(network_keys)
    ]
    assert lan.prepare_env(updated, ADDRESS) == (updated, port)


def test_prepare_env_already_exact_lan_is_unchanged(lan: SimpleNamespace) -> None:
    original = _env(str(ADDRESS.ip), origin=f"http://{ADDRESS.ip}:8080")
    assert lan.prepare_env(original, ADDRESS) == (original, 8080)


@pytest.mark.parametrize(
    ("bind", "origin"),
    [
        ("0.0.0.0", "http://localhost:8080"),
        ("::", "http://localhost:8080"),
        ("192.168.50.99", "http://192.168.50.99:8080"),
        ("127.0.0.1", "https://example.org:8080"),
        ("127.0.0.1", "http://localhost:9000"),
        ("127.0.0.1", "http://localhost:8080/path"),
        ("127.0.0.1", "http://localhost:8080?custom=yes"),
        ("127.0.0.1", "http://user@localhost:8080"),
        ("127.0.0.1", "http://192.168.50.20:8080"),
        ("192.168.50.20", "http://localhost:8080"),
    ],
)
def test_prepare_env_refuses_custom_or_inconsistent_bind_and_origin(
    lan: SimpleNamespace, bind: str, origin: str
) -> None:
    with pytest.raises(ValueError):
        lan.prepare_env(_env(bind, origin=origin), ADDRESS)


@pytest.mark.parametrize(
    "port", ["", "0", "22", "80", "443", "1023", "65536", "8080:8081", "NaN"]
)
def test_prepare_env_refuses_invalid_ssh_or_privileged_ports(
    lan: SimpleNamespace, port: str
) -> None:
    with pytest.raises(ValueError):
        lan.prepare_env(_env(port=port), ADDRESS)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("FOXTUBE_BIND", "127.0.0.1"),
        ("FOXTUBE_PORT", "8080"),
        ("FOXTUBE_PUBLIC_ORIGIN", "http://localhost:8080"),
    ],
)
def test_prepare_env_refuses_even_identical_duplicate_network_keys(
    lan: SimpleNamespace, key: str, value: str
) -> None:
    with pytest.raises(ValueError):
        lan.prepare_env(_env() + f"{key}={value}\n", ADDRESS)


@pytest.fixture
def compose_config() -> dict:
    return {
        "services": {
            "nginx": {
                "ports": [
                    {
                        "host_ip": str(ADDRESS.ip),
                        "published": "8080",
                        "target": 8080,
                        "protocol": "tcp",
                    }
                ]
            },
            "api": {
                "environment": {
                    "FOXTUBE_PUBLIC_ORIGIN": f"http://{ADDRESS.ip}:8080",
                },
                "expose": ["8000"],
            },
            "worker": {},
            "db": {"expose": ["5432"]},
        }
    }


def test_validate_compose_accepts_only_expected_lan_publication(
    lan: SimpleNamespace, compose_config: dict
) -> None:
    before = deepcopy(compose_config)
    assert lan.validate_compose(compose_config, ADDRESS, 8080) is None
    assert compose_config == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host_ip", "0.0.0.0"),
        ("host_ip", "127.0.0.1"),
        ("host_ip", "192.168.50.99"),
        ("published", "9000"),
        ("published", "8080-8082"),
        ("target", 8000),
        ("protocol", "udp"),
    ],
)
def test_validate_compose_rejects_wrong_nginx_publication(
    lan: SimpleNamespace, compose_config: dict, field: str, value: str | int
) -> None:
    compose_config["services"]["nginx"]["ports"][0][field] = value
    with pytest.raises(ValueError):
        lan.validate_compose(compose_config, ADDRESS, 8080)


def test_validate_compose_rejects_unspecified_host_ip(
    lan: SimpleNamespace, compose_config: dict
) -> None:
    del compose_config["services"]["nginx"]["ports"][0]["host_ip"]
    with pytest.raises(ValueError):
        lan.validate_compose(compose_config, ADDRESS, 8080)


@pytest.mark.parametrize("count", [0, 2])
def test_validate_compose_requires_exactly_one_nginx_publication(
    lan: SimpleNamespace, compose_config: dict, count: int
) -> None:
    port = compose_config["services"]["nginx"]["ports"][0]
    compose_config["services"]["nginx"]["ports"] = [
        deepcopy(port) for _ in range(count)
    ]
    with pytest.raises(ValueError):
        lan.validate_compose(compose_config, ADDRESS, 8080)


@pytest.mark.parametrize("service", ["api", "worker", "db", "unexpected"])
def test_validate_compose_refuses_publication_on_any_other_service(
    lan: SimpleNamespace, compose_config: dict, service: str
) -> None:
    compose_config["services"].setdefault(service, {})["ports"] = [
        {"host_ip": str(ADDRESS.ip), "published": "9000", "target": 9000}
    ]
    with pytest.raises(ValueError):
        lan.validate_compose(compose_config, ADDRESS, 8080)


@pytest.mark.parametrize("service", ["nginx", "api", "worker", "db", "unexpected"])
def test_validate_compose_refuses_host_network_on_any_service(
    lan: SimpleNamespace, compose_config: dict, service: str
) -> None:
    compose_config["services"].setdefault(service, {})["network_mode"] = "host"
    with pytest.raises(ValueError):
        lan.validate_compose(compose_config, ADDRESS, 8080)


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:8080",
        "http://192.168.50.99:8080",
        "http://192.168.50.20:9000",
        "https://192.168.50.20:8080",
        "http://192.168.50.20:8080/path",
        None,
    ],
)
def test_validate_compose_requires_exact_api_origin(
    lan: SimpleNamespace, compose_config: dict, origin: str | None
) -> None:
    compose_config["services"]["api"]["environment"]["FOXTUBE_PUBLIC_ORIGIN"] = origin
    with pytest.raises(ValueError):
        lan.validate_compose(compose_config, ADDRESS, 8080)


@pytest.fixture
def clean_lan_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in AMBIENT_NETWORK_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_validate_environment_accepts_unrelated_environment_without_mutation(
    lan: SimpleNamespace,
    clean_lan_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOXTUBE_TEST_UNRELATED", "synthetic-value")
    before = dict(os.environ)
    assert lan.validate_environment() is None
    assert dict(os.environ) == before


@pytest.mark.parametrize("key", AMBIENT_NETWORK_KEYS)
@pytest.mark.parametrize("value", ["", "synthetic-override"])
def test_validate_environment_rejects_ambient_docker_and_compose_overrides(
    lan: SimpleNamespace,
    clean_lan_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    monkeypatch.setenv(key, value)
    before = dict(os.environ)
    with pytest.raises(ValueError):
        lan.validate_environment()
    assert dict(os.environ) == before


def _mock_run(
    lan: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[tuple[str, ...], str],
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake_run(*arguments: str) -> str:
        calls.append(arguments)
        assert arguments in responses, f"Unexpected external command: {arguments}"
        return responses[arguments]

    # runpy returns a namespace copy; functions retain their original globals.
    monkeypatch.setitem(lan.run.__globals__, "run", fake_run)
    return calls


def _engine_responses(endpoint: str) -> dict[tuple[str, ...], str]:
    return {
        ("docker", "context", "show"): "test-context\n",
        ("docker", "context", "inspect", "test-context"): json.dumps(
            [{"Endpoints": {"docker": {"Host": endpoint}}}]
        ),
    }


def test_validate_engine_accepts_local_system_docker_socket(
    lan: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = _engine_responses("unix:///var/run/docker.sock")
    calls = _mock_run(lan, monkeypatch, responses)
    assert lan.validate_engine() is None
    assert calls == list(responses)


@pytest.mark.parametrize(
    "endpoint",
    [
        "tcp://192.168.50.99:2375",
        "tcp://127.0.0.1:2375",
        "ssh://example.invalid",
        "unix:///run/user/1000/docker.sock",
        "unix:///tmp/other-docker.sock",
        "npipe:////./pipe/docker_engine",
        "",
    ],
)
def test_validate_engine_refuses_remote_or_other_engine_sockets(
    lan: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    calls = _mock_run(lan, monkeypatch, _engine_responses(endpoint))
    with pytest.raises(ValueError):
        lan.validate_engine()
    assert len(calls) == 2


def _ss_listener(host: str, port: int, process: str) -> str:
    return f'LISTEN 0 128 {host}:{port} 0.0.0.0:* users:(("{process}",pid=100,fd=3))\n'


def _listener_responses(
    listeners: str, bindings: list[dict[str, str]] | None = None
) -> dict[tuple[str, ...], str]:
    responses = {
        ("docker", "compose", "ps", "-q", "nginx"): "",
        ("ss", "-H", "-lntp"): listeners,
    }
    if bindings is not None:
        responses[("docker", "compose", "ps", "-q", "nginx")] = "own-nginx\n"
        responses[("docker", "inspect", "own-nginx")] = json.dumps(
            [{"NetworkSettings": {"Ports": {"8080/tcp": bindings}}}]
        )
    return responses


def test_validate_listener_accepts_free_app_port_and_leaves_ssh_alone(
    lan: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_run(
        lan,
        monkeypatch,
        _listener_responses(_ss_listener("0.0.0.0", 22, "sshd")),
    )
    assert lan.validate_listener(ADDRESS, 8080) is None


@pytest.mark.parametrize(
    ("port", "process"), [(22, "sshd"), (8080, "python"), (8080, "docker-proxy")]
)
def test_validate_listener_rejects_foreign_listener_including_ssh(
    lan: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    port: int,
    process: str,
) -> None:
    _mock_run(
        lan,
        monkeypatch,
        _listener_responses(_ss_listener("0.0.0.0", port, process)),
    )
    with pytest.raises(ValueError):
        lan.validate_listener(ADDRESS, port)


@pytest.mark.parametrize("docker_proxy", [False, True])
def test_validate_listener_accepts_existing_own_lan_endpoint(
    lan: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, docker_proxy: bool
) -> None:
    listeners = (
        _ss_listener(str(ADDRESS.ip), 8080, "docker-proxy") if docker_proxy else ""
    )
    _mock_run(
        lan,
        monkeypatch,
        _listener_responses(
            listeners, [{"HostIp": str(ADDRESS.ip), "HostPort": "8080"}]
        ),
    )
    assert lan.validate_listener(ADDRESS, 8080) is None


def test_validate_listener_allows_migrating_own_loopback_endpoint(
    lan: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_run(
        lan,
        monkeypatch,
        _listener_responses(
            _ss_listener("127.0.0.1", 8080, "docker-proxy"),
            [{"HostIp": "127.0.0.1", "HostPort": "8080"}],
        ),
    )
    assert lan.validate_listener(ADDRESS, 8080) is None


@pytest.mark.parametrize(
    ("bound_ip", "bound_port"),
    [
        ("192.168.50.99", "8080"),
        (str(ADDRESS.ip), "9000"),
        ("0.0.0.0", "8080"),
        ("::", "8080"),
    ],
)
def test_validate_listener_refuses_changing_active_non_loopback_endpoint(
    lan: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    bound_ip: str,
    bound_port: str,
) -> None:
    _mock_run(
        lan,
        monkeypatch,
        _listener_responses("", [{"HostIp": bound_ip, "HostPort": bound_port}]),
    )
    with pytest.raises(ValueError):
        lan.validate_listener(ADDRESS, 8080)


def test_validate_listener_does_not_trust_foreign_process_on_own_binding(
    lan: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_run(
        lan,
        monkeypatch,
        _listener_responses(
            _ss_listener(str(ADDRESS.ip), 8080, "sshd"),
            [{"HostIp": str(ADDRESS.ip), "HostPort": "8080"}],
        ),
    )
    with pytest.raises(ValueError):
        lan.validate_listener(ADDRESS, 8080)


@pytest.fixture
def owned_nft_table(lan: SimpleNamespace) -> dict:
    # Jammy nft 1.0.2/kernel 5.15 retains rule comments, not table comments.
    return {
        "nftables": [
            {"metainfo": {"version": "1.0.2", "json_schema_version": 1}},
            {"table": {"family": "inet", "name": "foxtube_lan", "handle": 10}},
            {
                "chain": {
                    "family": "inet",
                    "table": "foxtube_lan",
                    "name": "ingress",
                    "type": "filter",
                    "hook": "prerouting",
                    "prio": -110,
                    "policy": "accept",
                }
            },
            {
                "rule": {
                    "family": "inet",
                    "table": "foxtube_lan",
                    "chain": "ingress",
                    "handle": 15,
                    "expr": [{"counter": {"packets": 0, "bytes": 0}}, {"drop": None}],
                    "comment": lan.MARKER,
                }
            },
        ]
    }


@pytest.mark.parametrize("table_comment", [False, True])
def test_owned_nft_table_is_recognized_by_rule_marker_without_table_comment(
    lan: SimpleNamespace, owned_nft_table: dict, table_comment: bool
) -> None:
    if table_comment:
        owned_nft_table["nftables"][1]["table"]["comment"] = lan.MARKER
    before = deepcopy(owned_nft_table)
    assert lan.validate_owned_table(owned_nft_table) is None
    assert owned_nft_table == before


@pytest.mark.parametrize("has_rule", [False, True])
def test_nft_table_without_rule_marker_is_not_owned(
    lan: SimpleNamespace, owned_nft_table: dict, has_rule: bool
) -> None:
    if has_rule:
        del owned_nft_table["nftables"][-1]["rule"]["comment"]
    else:
        owned_nft_table["nftables"].pop()
    with pytest.raises(ValueError):
        lan.validate_owned_table(owned_nft_table)


def test_nft_table_comment_alone_does_not_establish_ownership(
    lan: SimpleNamespace, owned_nft_table: dict
) -> None:
    owned_nft_table["nftables"][1]["table"]["comment"] = lan.MARKER
    del owned_nft_table["nftables"][-1]["rule"]["comment"]
    with pytest.raises(ValueError):
        lan.validate_owned_table(owned_nft_table)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("family", "ip"),
        ("table", "another_table"),
        ("chain", "input"),
        ("comment", "Managed by another application"),
    ],
)
def test_nft_rule_marker_must_identify_expected_family_table_and_chain(
    lan: SimpleNamespace, owned_nft_table: dict, field: str, value: str
) -> None:
    owned_nft_table["nftables"][-1]["rule"][field] = value
    with pytest.raises(ValueError):
        lan.validate_owned_table(owned_nft_table)


def test_duplicate_nft_rule_ownership_markers_are_rejected(
    lan: SimpleNamespace, owned_nft_table: dict
) -> None:
    duplicate = deepcopy(owned_nft_table["nftables"][-1])
    duplicate["rule"]["handle"] = 16
    owned_nft_table["nftables"].append(duplicate)
    with pytest.raises(ValueError):
        lan.validate_owned_table(owned_nft_table)
