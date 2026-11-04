from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.auth.models import Channel
from app.channels.schemas import ChannelView
from app.videos.models import Video
from app.videos.service import readable


def get_channel(
    engine: Engine,
    viewer_id: UUID | None,
    *,
    channel_id: UUID | None = None,
    handle: str | None = None,
) -> ChannelView:
    with Session(engine) as session:
        channel = session.scalar(
            select(Channel).where(
                Channel.id == channel_id
                if channel_id is not None
                else Channel.handle == handle
            )
        )
        if channel is None:
            raise HTTPException(404, "Канал не найден.")
        count = (
            session.scalar(
                select(func.count())
                .select_from(Video)
                .where(Video.owner_id == channel.user_id, readable(viewer_id))
            )
            or 0
        )
        return ChannelView(
            id=channel.id,
            handle=channel.handle,
            title=channel.title,
            created_at=channel.created_at,
            can_manage=viewer_id == channel.user_id,
            videos_count=count,
        )
