import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings


def test_blank_optional_smtp_environment_disables_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("HOST", "FROM", "USERNAME", "PASSWORD"):
        monkeypatch.setenv(f"FOXTUBE_SMTP_{name}", "")
    settings = Settings(db_password=SecretStr("test-only"))
    assert settings.smtp_host is None
    assert settings.smtp_from is None
    assert settings.smtp_username is None
    assert settings.smtp_password is None


@pytest.mark.parametrize(
    "values",
    [
        {"smtp_host": "mail.example.com"},
        {"smtp_from": "foxtube@example.com"},
        {"smtp_username": "mailer"},
        {"smtp_password": "test-only"},
        {"smtp_security": "plain"},
        {"smtp_timeout": 31},
        {"smtp_timeout": 0},
        {"smtp_host": "host\nheader", "smtp_from": "foxtube@example.com"},
    ],
)
def test_invalid_or_partial_mail_configuration_fails_at_startup(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"db_password": "test-only", **values})


@pytest.mark.parametrize(
    "values",
    [
        {"env": "production"},
        {"smtp_password": "synthetic-smtp-secret"},
        {"smtp_port": "synthetic-invalid-port"},
    ],
)
def test_configuration_errors_do_not_log_input_secrets(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError) as error:
        Settings.model_validate({"db_password": "synthetic-db-secret", **values})
    diagnostic = str(error.value)
    assert "synthetic-" not in diagnostic
    assert "input_value" not in diagnostic
