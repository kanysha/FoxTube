"""Read-only HTTP/TCP checks for an already running local/LAN deployment.

Run from the user's computer, not the target VM:
python3 tests/deployment/network.py --base-url http://192.168.1.16:8080 \
    --output .artifacts/lan/network.json

Only localhost, loopback literals and RFC1918 IPv4 literals are accepted. No DNS
names, proxies, redirects, login, SSH commands or service changes are used. TCP
success proves reachability, not service identity; a timeout proves no successful
connection during the configured interval, not a closed listening socket.
"""

import argparse
import errno
import http.client
import ipaddress
import json
import os
import re
import socket
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
ORIGIN_ERROR = "Use a bare HTTP(S) loopback or RFC1918 IPv4 origin"
UTC_ZONE = timezone.utc  # noqa: UP017 - Ubuntu 22.04's stdlib probe uses Python 3.10.


class SwaggerDocument(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: set[str] = set()
        self.mounts = 0
        self.scripts: list[str] = []
        self.styles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.elements.add(tag)
        if tag == "div" and attributes.get("id") == "swagger-ui":
            self.mounts += 1
        if tag == "script" and attributes.get("src"):
            self.scripts.append(str(attributes["src"]))
        if tag == "link" and "stylesheet" in (attributes.get("rel") or "").split():
            self.styles.append(attributes.get("href") or "")


def validate_origin(value: str) -> str:
    """Return a canonical permitted origin without reflecting invalid input."""
    try:
        if not value.isascii() or any(ord(char) <= 32 for char in value):
            raise ValueError
        if "?" in value or "#" in value:
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or re.fullmatch(
                r"(?:localhost|[0-9.]+|\[[0-9a-f:]+\])(?::[0-9]+)?",
                parsed.netloc,
                flags=re.IGNORECASE,
            )
            is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or (port is not None and not 1 <= port <= 65535)
            or parsed.netloc.endswith(":")
        ):
            raise ValueError
        if host != "localhost":
            address = ipaddress.ip_address(host)
            if not (
                address.is_loopback
                or (
                    isinstance(address, ipaddress.IPv4Address)
                    and any(address in network for network in PRIVATE_NETWORKS)
                )
            ):
                raise ValueError
            host = address.compressed
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority += f":{port}"
        return f"{parsed.scheme}://{authority}"
    except (ValueError, TypeError):
        raise ValueError(ORIGIN_ERROR) from None


def public_response(
    base_url: str, path: str, timeout: float, limit: int = 131072, method: str = "GET"
) -> tuple[int, str, bytes]:
    """Read a bounded public response without proxy/redirect/cookie handling."""
    parsed = urlsplit(base_url)
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    try:
        connection.request(method, path, headers={"Accept": "*/*"})
        response = connection.getresponse()
        return (
            response.status,
            response.getheader("Content-Type", "").split(";", 1)[0].lower(),
            response.read(limit + 1),
        )
    finally:
        connection.close()


def check_swagger(base_url: str, body: bytes, timeout: float) -> dict[str, Any]:
    """Check the real HTML mount, local JS/CSS and FoxTube OpenAPI document.

    This HTTP probe does not execute JavaScript or replace Chrome Try it out.
    """
    document = SwaggerDocument()
    document.feed(body.decode("utf-8"))
    references = document.scripts + document.styles
    result: dict[str, Any] = {
        "status": "failed",
        "mount_present": document.mounts == 1,
        "assets": [],
    }
    if not (
        {"html", "head", "body"} <= document.elements
        and document.mounts == 1
        and len(document.scripts) >= 2
        and document.styles
        and len(references) <= 8
    ):
        return result
    for reference in references:
        parsed = urlsplit(reference)
        if not (
            reference.startswith("/static/")
            and not parsed.scheme
            and not parsed.netloc
            and not parsed.query
            and not parsed.fragment
            and ".." not in parsed.path.split("/")
            and "\\" not in reference
        ):
            return result
    bundle = initializer = stylesheet = False
    for reference in references:
        is_bundle = reference.endswith("/swagger-ui-bundle.js")
        method = "HEAD" if is_bundle or reference in document.styles else "GET"
        status, content_type, asset = public_response(
            base_url, reference, timeout, method=method
        )
        valid = status == 200 and (method == "HEAD" or 0 < len(asset) <= 131072)
        if reference in document.scripts:
            valid = valid and content_type in {
                "application/javascript",
                "text/javascript",
            }
            if valid:
                bundle = bundle or is_bundle
                initializer = initializer or (
                    re.search(rb"SwaggerUIBundle\s*\(", asset) is not None
                    and re.search(rb"dom_id\s*:\s*['\"]#swagger-ui['\"]", asset)
                    is not None
                    and re.search(rb"url\s*:\s*['\"]/openapi\.json['\"]", asset)
                    is not None
                )
        else:
            valid = valid and content_type == "text/css"
            stylesheet = stylesheet or valid
        result["assets"].append(
            {
                "path": reference,
                "method": method,
                "http_status": status,
                "status": "passed" if valid else "failed",
            }
        )
    status, content_type, schema = public_response(
        base_url, "/openapi.json", timeout, limit=2097152
    )
    openapi: Any = json.loads(schema) if len(schema) <= 2097152 else None
    schema_valid = (
        status == 200
        and content_type == "application/json"
        and isinstance(openapi, dict)
        and str(openapi.get("openapi", "")).startswith("3.")
        and openapi.get("info", {}).get("title") == "FoxTube API"
        and "post" in openapi.get("paths", {}).get("/api/v1/auth/register", {})
        and {"get", "post"} <= openapi.get("paths", {}).get("/api/v1/videos", {}).keys()
    )
    result.update(
        bundle_available=bundle,
        initializer_configured=initializer,
        stylesheet_available=stylesheet,
        openapi={
            "http_status": status,
            "status": "passed" if schema_valid else "failed",
        },
    )
    if (
        bundle
        and initializer
        and stylesheet
        and schema_valid
        and all(item["status"] == "passed" for item in result["assets"])
    ):
        result["status"] = "passed"
    return result


def check_http(base_url: str, path: str, timeout: float) -> dict[str, Any]:
    """Read a bounded public response; never save bodies, headers or cookies."""
    result: dict[str, Any] = {"path": path, "status": "failed"}
    started = time.monotonic()
    try:
        status, content_type, body = public_response(base_url, path, timeout)
        result["http_status"] = status
        bounded = len(body) <= 131072
        valid = status == 200 and bounded
        if path == "/health/ready":
            payload = json.loads(body) if bounded else None
            valid = (
                valid
                and content_type == "application/json"
                and isinstance(payload, dict)
                and payload.get("status") == "ok"
                and isinstance(payload.get("checks"), dict)
                and bool(payload["checks"])
                and all(value == "ok" for value in payload["checks"].values())
            )
        elif path == "/docs":
            valid = valid and content_type == "text/html"
            if valid:
                result["swagger"] = check_swagger(base_url, body, timeout)
                valid = result["swagger"]["status"] == "passed"
        else:
            valid = valid and content_type == "text/html" and b"FoxTube" in body
        result["status"] = "passed" if valid else "failed"
    except Exception as error:
        result["failure_type"] = type(error).__name__
    finally:
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


def check_tcp(
    host: str, port: int, expected_open: bool, timeout: float
) -> dict[str, Any]:
    """Attempt one TCP handshake without sending an application payload."""
    result: dict[str, Any] = {"port": port, "expected_open": expected_open}
    connected = False
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            connected = True
            result["observation"] = "connected"
    except TimeoutError:
        result["observation"] = "timeout"
    except OSError as error:
        result["observation"] = (
            "refused" if error.errno == errno.ECONNREFUSED else "unreachable"
        )
        result["failure_type"] = type(error).__name__
    result["status"] = "passed" if connected == expected_open else "failed"
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("FOXTUBE_TEST_BASE_URL", "http://localhost:8080"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path(".artifacts/lan/network.json")
    )
    parser.add_argument("--timeout", type=float, default=3)
    arguments = parser.parse_args()
    report: dict[str, Any] = {
        "status": "failed",
        "phase": "origin_validation",
        "checked_at": datetime.now(UTC_ZONE).isoformat(),
        "not_covered": [
            "installation, database state, authenticated UI and private media",
            "service identity, firewall rules and ports other than the listed five",
            "Swagger JavaScript execution and Try it out",
        ],
    }
    try:
        base_url = validate_origin(arguments.base_url)
        if not 0 < arguments.timeout <= 30:
            raise ValueError("Timeout must be positive and at most 30 seconds")
        report["base_url"] = base_url
        report["phase"] = "http"
        report["http"] = [
            check_http(base_url, path, arguments.timeout)
            for path in ("/health/ready", "/", "/docs")
        ]
        report["phase"] = "tcp"
        host = urlsplit(base_url).hostname
        report["tcp"] = [
            check_tcp(host, port, expected_open, arguments.timeout)
            for port, expected_open in (
                (8080, True),
                (22, True),
                (5432, False),
                (6379, False),
                (8000, False),
            )
        ]
        report["phase"] = "complete"
        if all(item["status"] == "passed" for item in report["http"] + report["tcp"]):
            report["status"] = "passed"
    except Exception as error:
        report["failure_type"] = type(error).__name__
    try:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
    except OSError:
        print("Deployment network: failed; phase=report_write")
        return 1
    print(f"Deployment network: {report['status']}; phase={report['phase']}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
