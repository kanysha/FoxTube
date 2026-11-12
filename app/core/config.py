from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import (
    EmailStr,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FOXTUBE_", extra="ignore", hide_input_in_errors=True
    )

    env: Literal["development", "production", "test"] = "development"
    public_origin: HttpUrl = HttpUrl("http://localhost:8080")
    db_host: str = "db"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "foxtube"
    db_user: str = "foxtube_api"
    db_password: SecretStr
    media_root: Path = Path("/var/lib/foxtube/media")
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: EmailStr | None = None
    smtp_security: Literal["starttls", "tls"] = "starttls"
    smtp_timeout: float = Field(default=10, ge=1, le=30)

    @field_validator(
        "smtp_host", "smtp_username", "smtp_password", "smtp_from", mode="before"
    )
    @classmethod
    def optional_mail_value(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("smtp_host", "smtp_username")
    @classmethod
    def valid_mail_setting(cls, value: str | None) -> str | None:
        if value is not None and any(char in value for char in "\r\n\x00"):
            raise ValueError("SMTP setting contains a control character")
        return value

    @field_validator("public_origin")
    @classmethod
    def validate_origin(cls, value: HttpUrl) -> HttpUrl:
        if (
            value.username
            or value.password
            or value.path not in {None, "/"}
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError(
                "public_origin must be an origin without credentials or path"
            )
        return value

    @model_validator(mode="after")
    def require_production_https(self) -> "Settings":
        if self.env == "production" and self.public_origin.scheme != "https":
            raise ValueError("production requires an HTTPS public_origin")
        if bool(self.smtp_host) != bool(self.smtp_from):
            raise ValueError("SMTP host and sender must be configured together")
        if bool(self.smtp_username) != bool(self.smtp_password):
            raise ValueError("SMTP username and password must be configured together")
        return self

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    @property
    def allowed_hosts(self) -> list[str]:
        return sorted(
            {"localhost", "127.0.0.1", self.public_origin.host or "localhost"}
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
