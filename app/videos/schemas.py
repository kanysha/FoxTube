from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.videos.limits import MAX_SOURCE_BYTES


class VideoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    visibility: Literal["public", "private"] = "private"
    expected_size: int = Field(ge=1, le=MAX_SOURCE_BYTES, strict=True)

    @field_validator("title", "description", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class VideoPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    visibility: Literal["public", "private"] | None = None

    @field_validator("title", "description", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class VideoView(BaseModel):
    id: UUID
    title: str
    description: str
    visibility: str
    status: str
    error: str | None
    duration: float | None
    width: int | None
    height: int | None
    created_at: datetime
    owner_id: UUID
    channel_id: UUID
    channel_name: str
    can_manage: bool
    playback_url: str | None
    poster_url: str | None


class VideoPage(BaseModel):
    items: list[VideoView]
    page: int
    page_size: int
    total: int


class GrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(max_length=254)

    @field_validator("email", mode="before")
    @classmethod
    def normalize(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value


class GrantView(BaseModel):
    user_id: UUID
    email: str
    display_name: str


class GrantPage(BaseModel):
    items: list[GrantView]
