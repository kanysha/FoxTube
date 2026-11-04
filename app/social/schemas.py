from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.videos.schemas import VideoPage


def normalized_text(value: object) -> object:
    if not isinstance(value, str):
        return value
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("Текст содержит недопустимый символ.")
    return value.strip()


class VideoSocial(BaseModel):
    likes_count: int
    comments_count: int
    liked: bool


class CommentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("body", mode="before")
    @classmethod
    def strip_body(cls, value: object) -> object:
        return normalized_text(value)


class CommentView(BaseModel):
    id: UUID
    body: str
    created_at: datetime
    author_id: UUID
    author_name: str
    channel_id: UUID
    can_delete: bool


class CommentPage(BaseModel):
    items: list[CommentView]
    total: int
    page: int
    page_size: int


class SubscriptionView(BaseModel):
    subscribers_count: int
    subscribed: bool
    can_subscribe: bool


class SubscribedChannel(BaseModel):
    id: UUID
    title: str
    handle: str


class SubscribedChannelPage(BaseModel):
    items: list[SubscribedChannel]
    total: int
    page: int
    page_size: int


class PlaylistInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    visibility: Literal["public", "private"] = "private"

    @field_validator("title", "description", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return normalized_text(value)


class PlaylistPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    visibility: Literal["public", "private"] | None = None

    @field_validator("title", "description", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return normalized_text(value)


class PlaylistView(BaseModel):
    id: UUID
    title: str
    description: str
    visibility: str
    owner_id: UUID
    channel_id: UUID
    channel_name: str
    created_at: datetime
    can_manage: bool
    videos_count: int


class PlaylistPage(BaseModel):
    items: list[PlaylistView]
    total: int
    page: int
    page_size: int


class PlaylistDetail(BaseModel):
    playlist: PlaylistView
    videos: VideoPage
