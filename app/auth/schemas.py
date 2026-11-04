from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from app.core.text import database_text


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=12, max_length=128)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value

    @field_validator("password", mode="before")
    @classmethod
    def valid_password_encoding(cls, value: object) -> object:
        if isinstance(value, str) and any(
            0xD800 <= ord(char) <= 0xDFFF for char in value
        ):
            raise ValueError("Пароль содержит недопустимый символ.")
        return value


class RegisterInput(LoginInput):
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("display_name", mode="before")
    @classmethod
    def trim_name(cls, value: object) -> object:
        return database_text(value)


class LogoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserView(BaseModel):
    id: UUID
    email: str
    display_name: str
    role: str
    channel_handle: str


class SessionView(BaseModel):
    user: UserView | None
    csrf_token: str
    expires_at: datetime
