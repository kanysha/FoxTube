"""Bootstrap repository regressions without apt, root access, or network calls."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

BOOTSTRAP = Path(__file__).resolve().parents[1] / "scripts/bootstrap.sh"
SIGNING_KEY = (
    "-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
    "isolated-test-fixture\n"
    "-----END PGP PUBLIC KEY BLOCK-----\n"
)
REPOSITORY_STUBS = """
curl() {
    local output=''
    while [ "$#" -gt 0 ]; do
        if [ "$1" = -o ]; then output=$2; shift 2; else shift; fi
    done
    if [ "${FOXTUBE_TEST_CURL_MODE:-}" = partial ]; then
        printf 'partial download' > "$output"
        return 18
    fi
    printf '%s' "$FOXTUBE_TEST_SIGNING_KEY" > "$output"
}
dpkg() { [ "$1" = --print-architecture ] && printf 'amd64\\n'; }
apt-get() { return 97; }
systemctl() { return 98; }
"""


def _helper(
    function: str, *arguments: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f"source {shlex.quote(str(BOOTSTRAP))}; "
            + (REPOSITORY_STUBS if environment is not None else "")
            + shlex.join([function, *arguments]),
        ],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize(
    ("version", "codename"), [("22.04", "jammy"), ("24.04", "noble")]
)
def test_supported_ubuntu_version_and_codename(version: str, codename: str) -> None:
    result = _helper("supported_ubuntu_codename", "ubuntu", version, codename)
    assert result.returncode == 0
    assert result.stdout.strip() == codename


@pytest.mark.parametrize(
    ("distribution", "version", "codename"),
    [
        ("ubuntu", "22.04", "noble"),
        ("ubuntu", "24.04", "jammy"),
        ("ubuntu", "20.04", "focal"),
        ("linuxmint", "22.04", "jammy"),
        ("ubuntu", "22.04", ""),
    ],
)
def test_unsupported_or_inconsistent_os_is_rejected(
    distribution: str, version: str, codename: str
) -> None:
    result = _helper("supported_ubuntu_codename", distribution, version, codename)
    assert result.returncode != 0
    assert not result.stdout


@pytest.fixture
def repository_environment() -> dict[str, str]:
    return {
        **os.environ,
        "FOXTUBE_TEST_SIGNING_KEY": SIGNING_KEY,
    }


def _assert_no_temporary_files(apt: Path) -> None:
    assert not list(apt.rglob(".foxtube-docker-*"))


@pytest.mark.parametrize("codename", ["jammy", "noble"])
def test_repository_uses_requested_suite_and_repeat_preserves_files(
    tmp_path: Path, repository_environment: dict[str, str], codename: str
) -> None:
    apt = tmp_path / "apt"
    result = _helper(
        "configure_docker_repository",
        codename,
        str(apt),
        environment=repository_environment,
    )
    assert result.returncode == 0, result.stderr
    key = apt / "keyrings/docker.asc"
    source = apt / "sources.list.d/docker.sources"
    assert key.read_text() == SIGNING_KEY
    assert f"Suites: {codename}\n" in source.read_text()
    assert f"Signed-By: {key}\n" in source.read_text()
    before = [(path.stat().st_ino, path.stat().st_mtime_ns) for path in (key, source)]
    repeated = _helper(
        "configure_docker_repository",
        codename,
        str(apt),
        environment=repository_environment,
    )
    assert repeated.returncode == 0, repeated.stderr
    assert before == [
        (path.stat().st_ino, path.stat().st_mtime_ns) for path in (key, source)
    ]
    assert key.stat().st_mode & 0o777 == 0o644
    assert source.stat().st_mode & 0o777 == 0o644
    _assert_no_temporary_files(apt)


def test_interrupted_key_download_leaves_no_target_and_retry_succeeds(
    tmp_path: Path, repository_environment: dict[str, str]
) -> None:
    apt = tmp_path / "apt"
    failed = _helper(
        "configure_docker_repository",
        "jammy",
        str(apt),
        environment={**repository_environment, "FOXTUBE_TEST_CURL_MODE": "partial"},
    )
    assert failed.returncode != 0
    assert not (apt / "keyrings/docker.asc").exists()
    assert not (apt / "sources.list.d/docker.sources").exists()
    _assert_no_temporary_files(apt)
    repeated = _helper(
        "configure_docker_repository",
        "jammy",
        str(apt),
        environment=repository_environment,
    )
    assert repeated.returncode == 0, repeated.stderr
    assert (apt / "keyrings/docker.asc").read_text() == SIGNING_KEY
    _assert_no_temporary_files(apt)


@pytest.mark.parametrize("existing", ["key", "source", "key_symlink", "source_symlink"])
def test_existing_custom_repository_files_are_preserved(
    tmp_path: Path, repository_environment: dict[str, str], existing: str
) -> None:
    apt = tmp_path / "apt"
    key = apt / "keyrings/docker.asc"
    source = apt / "sources.list.d/docker.sources"
    key.parent.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    target = key if existing.startswith("key") else source
    original = tmp_path / "user-file"
    original.write_text("user-owned configuration")
    if existing.endswith("symlink"):
        target.symlink_to(original)
    else:
        target.write_text(original.read_text())
    result = _helper(
        "configure_docker_repository",
        "jammy",
        str(apt),
        environment=repository_environment,
    )
    assert result.returncode != 0
    assert "Existing Docker repository file" in result.stderr
    assert target.read_text() == "user-owned configuration"
    assert original.read_text() == "user-owned configuration"
    assert not (source if target == key else key).exists()
    _assert_no_temporary_files(apt)


@pytest.mark.parametrize(
    "options",
    [
        ["--no-lan", "--listen-all"],
        ["--no-lan", "--allow-any-source"],
        ["--no-lan", "--allow-subnet", "10.8.0.62/32"],
        ["--no-lan", "--tcp-mss", "1200"],
        ["--allow-any-source", "--allow-subnet", "10.8.0.62/32"],
        ["--allow-subnet"],
        ["--allow-subnet", "--listen-all"],
        ["--tcp-mss"],
        ["--tcp-mss", "--listen-all"],
        ["--tcp-mss", "535"],
        ["--tcp-mss", "1461"],
        ["--tcp-mss", "01200"],
        ["--tcp-mss", "1200; true"],
    ],
)
def test_invalid_network_option_combinations_fail_before_docker(
    tmp_path: Path, options: list[str]
) -> None:
    # A real parser run must stop before inspecting Docker or writing configuration.
    result = subprocess.run(
        ["/bin/bash", str(BOOTSTRAP), *options],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "cannot be combined" in result.stderr or "requires" in result.stderr
    assert not list(tmp_path.iterdir())
