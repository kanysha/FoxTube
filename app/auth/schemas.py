from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=12, max_length=128)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value


class RegisterInput(LoginInput):
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("display_name", mode="before")
    @classmethod
    def trim_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


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
