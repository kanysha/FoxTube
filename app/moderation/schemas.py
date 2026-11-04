from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

Role = Literal["user", "moderator", "admin"]
ReportStatus = Literal["open", "resolved"]


def normalized_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    if "\x00" in value:
        raise ValueError("Текст содержит недопустимый символ.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Текст должен содержать корректные символы Unicode.") from None
    return value.strip()


class ReasonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value: object) -> object:
        return normalized_text(value)


class ReportInput(ReasonInput):
    pass


class ModerationInput(ReasonInput):
    status: Literal["hidden", "visible"]


class ResolutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: str = Field(min_length=1, max_length=1000)

    @field_validator("resolution", mode="before")
    @classmethod
    def strip_resolution(cls, value: object) -> object:
        return normalized_text(value)


class RoleInput(ReasonInput):
    email: EmailStr | None = Field(default=None, max_length=254)
    user_id: UUID | None = None
    role: Role

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value

    @model_validator(mode="after")
    def one_target(self) -> Self:
        if (self.email is None) == (self.user_id is None):
            raise ValueError("Укажите точный email или ID пользователя.")
        return self


class ReportReceipt(BaseModel):
    id: UUID
    video_id: UUID
    status: str
    created_at: datetime


class ModerationVideoView(BaseModel):
    id: UUID
    title: str
    visibility: str
    status: str
    moderation_status: str
    deleted: bool
    watch_url: str | None


class ReportView(ReportReceipt):
    reason: str
    resolution: str | None
    resolved_at: datetime | None
    video: ModerationVideoView


class ReportPage(BaseModel):
    items: list[ReportView]
    page: int
    page_size: int
    total: int


class RoleUserView(BaseModel):
    id: UUID
    email: str
    display_name: str
    role: str


class AuditView(BaseModel):
    id: UUID
    actor_id: UUID | None
    action: str
    video_id: UUID | None
    report_id: UUID | None
    target_user_id: UUID | None
    reason: str
    previous_value: str | None
    new_value: str | None
    created_at: datetime


class AuditPage(BaseModel):
    items: list[AuditView]
    page: int
    page_size: int
    total: int
