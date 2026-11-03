from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FOXTUBE_", extra="ignore")

    env: Literal["development", "production", "test"] = "development"
    public_origin: HttpUrl = HttpUrl("http://localhost:8080")
    db_host: str = "db"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "foxtube"
    db_user: str = "foxtube_api"
    db_password: SecretStr
    media_root: Path = Path("/var/lib/foxtube/media")

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
