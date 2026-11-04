from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.auth.dependencies import optional_session
from app.auth.service import SessionContext
from app.channels.schemas import ChannelView
from app.channels.service import get_channel
from app.videos.schemas import VideoPage
from app.videos.service import list_videos

router = APIRouter(prefix="/api/v1/channels", tags=["Каналы"])
Reader = Annotated[SessionContext | None, Depends(optional_session)]


@router.get("/{channel_id}", response_model=ChannelView)
def detail(request: Request, channel_id: UUID, state: Reader) -> ChannelView:
    return get_channel(
        request.app.state.engine,
        state.user.id if state and state.user else None,
        channel_id=channel_id,
    )


@router.get("/{channel_id}/videos", response_model=VideoPage)
def videos(
    request: Request,
    channel_id: UUID,
    state: Reader,
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=50),
) -> VideoPage:
    viewer_id = state.user.id if state and state.user else None
    get_channel(request.app.state.engine, viewer_id, channel_id=channel_id)
    return list_videos(
        request.app.state.engine,
        viewer_id,
        page=page,
        page_size=page_size,
        channel_id=channel_id,
    )
