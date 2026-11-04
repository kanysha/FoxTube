from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ChannelView(BaseModel):
    id: UUID
    handle: str
    title: str
    created_at: datetime
    can_manage: bool
    videos_count: int
