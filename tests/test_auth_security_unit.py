import asyncio
import ssl
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import SecretStr, ValidationError
from starlette.requests import Request

from app.auth import mail, recovery, routes
from app.auth.schemas import (
    PasswordChangeInput,
    PasswordForgotInput,
    PasswordResetInput,
)
from app.auth.service import new_session
from app.core.config import Settings


def smtp_settings(security: str = "starttls") -> Settings:
    return Settings(
        db_password=SecretStr("unit-only-database-credential"),
        public_origin="https://video.example.com",
        smtp_host="smtp.example.com",
        smtp_port=465 if security == "tls" else 587,
        smtp_username="unit-smtp-user",
        smtp_password=SecretStr("unit-smtp-password"),
        smtp_from="noreply@example.com",
        smtp_security=security,
        smtp_timeout=7,
    )


@pytest.mark.parametrize("security", ["starttls", "tls"])
def test_smtp_uses_verified_tls_and_fragment_link(
    monkeypatch: pytest.MonkeyPatch, security: str
) -> None:
    calls: list[str] = []
    messages: list[EmailMessage] = []

    class FakeSMTP:
        def __init__(
            self,
            host: str,
            port: int,
            *,
            timeout: float,
            context: ssl.SSLContext | None = None,
        ) -> None:
            assert host == "smtp.example.com" and timeout == 7
            assert port == (465 if security == "tls" else 587)
            if security == "tls":
                assert context is not None
                assert (
                    context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
                )
            calls.append("connect")

        def __enter__(self) -> "FakeSMTP":
            return self

        def __exit__(self, *args: object) -> None:
            calls.append("quit")

        def ehlo(self) -> None:
            calls.append("ehlo")

        def starttls(self, *, context: ssl.SSLContext) -> None:
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            calls.append("starttls")

        def login(self, username: str, password: str) -> None:
            assert username and password
            calls.append("login")

        def send_message(self, message: EmailMessage) -> None:
            messages.append(message)
            calls.append("send")

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSMTP)
    token = "a" * 43
    mail.send_recovery(smtp_settings(security), "recipient@example.com", token)
    assert calls == (
        ["connect", "ehlo", "starttls", "ehlo", "login", "send", "quit", "close"]
        if security == "starttls"
        else ["connect", "login", "send", "quit", "close"]
    )
    assert len(messages) == 1
    body = str(messages[0].get_content())
    link = next(line for line in body.splitlines() if line.startswith("https://"))
    parsed = urlsplit(link)
    assert parsed.path == "/password/reset" and not parsed.query
    assert parsed.fragment == f"token={token}"
    assert "30 минут" in body


def test_missing_smtp_never_attempts_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("No SMTP connection expected")

    monkeypatch.setattr(mail.smtplib, "SMTP", forbidden)
    settings = Settings(db_password=SecretStr("unit-only-db"))
    assert not mail.configured(settings)
    with pytest.raises(ValueError, match="not configured"):
        mail.send_recovery(settings, "recipient@example.com", "a" * 43)


@pytest.mark.parametrize("field", ["current_password", "new_password"])
def test_password_change_rejects_unencodable_input(field: str) -> None:
    values = {
        "current_password": "Unit-current-password-42!",
        "new_password": "Unit-new-password-43!",
    }
    values[field] = "Unit-password-\ud800-42!"
    with pytest.raises(ValidationError):
        PasswordChangeInput.model_validate(values)


def test_recovery_schemas_keep_secrets_out_of_representation() -> None:
    values = PasswordResetInput(token="a" * 43, new_password="Unit-new-password-43!")
    assert "a" * 43 not in repr(values)
    assert "Unit-new-password-43!" not in repr(values)
    assert str(PasswordForgotInput(email="  PERSON@example.com ").email) == (
        "person@example.com"
    )
    with pytest.raises(ValidationError):
        PasswordResetInput(token="a" * 43, new_password="Unit-password-\ud800-42!")


def test_forgot_schedules_lookup_only_after_generic_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = smtp_settings()
    engine = object()
    calls: list[str] = []
    monkeypatch.setattr(recovery, "check_limits", lambda *args: calls.append("rate"))
    monkeypatch.setattr(
        recovery, "deliver_recovery", lambda *args: calls.append("lookup-and-deliver")
    )
    request = Request(
        {
            "type": "http",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "app": SimpleNamespace(
                state=SimpleNamespace(engine=engine, settings=settings)
            ),
        }
    )
    tasks = BackgroundTasks()
    response = routes.forgot_password(
        request,
        PasswordForgotInput(email="recipient@example.com"),
        new_session(),
        tasks,
    )
    assert response.message == recovery.GENERIC_MESSAGE
    assert calls == ["rate"] and len(tasks.tasks) == 1
    asyncio.run(tasks())
    assert calls == ["rate", "lookup-and-deliver"]


def test_disabled_smtp_is_same_for_all_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recovery, "check_limits", lambda *args: None)
    settings = Settings(db_password=SecretStr("unit-only-db"))
    results = []
    for email in ["registered@example.com", "unknown@example.com"]:
        with pytest.raises(HTTPException) as failure:
            recovery.prepare_recovery(Any, settings, email, "127.0.0.1")
        results.append((failure.value.status_code, failure.value.detail))
    assert results[0] == results[1]
    assert results[0][0] == 503
