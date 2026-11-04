from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app.auth.dependencies import client_ip, required_user
from app.auth.rate_limits import check_limits
from app.auth.schemas import UserView
from app.social import service
from app.social.schemas import (
    CommentInput,
    CommentPage,
    CommentView,
    PlaylistDetail,
    PlaylistInput,
    PlaylistPage,
    PlaylistPatch,
    PlaylistView,
    SubscribedChannelPage,
    SubscriptionView,
    VideoSocial,
)
from app.videos.routes import Mutator, Reader
from app.videos.schemas import VideoPage

router = APIRouter(prefix="/api/v1", tags=["Взаимодействия и плейлисты"])
SignedIn = Annotated[UserView, Depends(required_user)]
Page = Annotated[int, Query(ge=1, le=10000)]
PageSize = Annotated[int, Query(ge=1, le=50)]


def rate_limit(request: Request, user: UserView, kind: str, maximum: int) -> None:
    check_limits(
        request.app.state.engine,
        [
            (f"social-{kind}-user", str(user.id), maximum),
            (f"social-{kind}-ip", client_ip(request), maximum * 5),
        ],
    )


@router.get("/videos/{video_id}/social", response_model=VideoSocial)
def video_social(request: Request, video_id: UUID, state: Reader) -> VideoSocial:
    return service.video_social(
        request.app.state.engine,
        video_id,
        state.user.id if state and state.user else None,
    )


@router.put("/videos/{video_id}/like", status_code=204)
def like(request: Request, video_id: UUID, user: Mutator) -> Response:
    rate_limit(request, user, "like", 600)
    service.set_like(request.app.state.engine, video_id, user.id, True)
    return Response(status_code=204)


@router.delete("/videos/{video_id}/like", status_code=204)
def unlike(request: Request, video_id: UUID, user: Mutator) -> Response:
    rate_limit(request, user, "like", 600)
    service.set_like(request.app.state.engine, video_id, user.id, False)
    return Response(status_code=204)


@router.get("/videos/{video_id}/comments", response_model=CommentPage)
def comments(
    request: Request,
    video_id: UUID,
    state: Reader,
    page: Page = 1,
    page_size: PageSize = 20,
) -> CommentPage:
    return service.comments(
        request.app.state.engine,
        video_id,
        state.user.id if state and state.user else None,
        page,
        page_size,
    )


@router.post("/videos/{video_id}/comments", response_model=CommentView, status_code=201)
def comment(
    request: Request, video_id: UUID, data: CommentInput, user: Mutator
) -> CommentView:
    rate_limit(request, user, "comment", 30)
    return service.add_comment(request.app.state.engine, video_id, user.id, data)


@router.delete("/comments/{comment_id}", status_code=204)
def delete_comment(request: Request, comment_id: UUID, user: Mutator) -> Response:
    rate_limit(request, user, "comment-delete", 120)
    service.delete_comment(request.app.state.engine, comment_id, user.id)
    return Response(status_code=204)


@router.get("/channels/{channel_id}/subscription", response_model=SubscriptionView)
def subscription(request: Request, channel_id: UUID, state: Reader) -> SubscriptionView:
    return service.subscription(
        request.app.state.engine,
        channel_id,
        state.user.id if state and state.user else None,
    )


@router.put("/channels/{channel_id}/subscription", status_code=204)
def subscribe(request: Request, channel_id: UUID, user: Mutator) -> Response:
    rate_limit(request, user, "subscription", 120)
    service.set_subscription(request.app.state.engine, channel_id, user.id, True)
    return Response(status_code=204)


@router.delete("/channels/{channel_id}/subscription", status_code=204)
def unsubscribe(request: Request, channel_id: UUID, user: Mutator) -> Response:
    rate_limit(request, user, "subscription", 120)
    service.set_subscription(request.app.state.engine, channel_id, user.id, False)
    return Response(status_code=204)


@router.get("/subscriptions", response_model=VideoPage)
def feed(
    request: Request, user: SignedIn, page: Page = 1, page_size: PageSize = 20
) -> VideoPage:
    return service.subscriptions_feed(
        request.app.state.engine, user.id, page, page_size
    )


@router.get("/subscriptions/channels", response_model=SubscribedChannelPage)
def subscribed_channels(
    request: Request, user: SignedIn, page: Page = 1, page_size: PageSize = 20
) -> SubscribedChannelPage:
    return service.subscribed_channels(
        request.app.state.engine, user.id, page, page_size
    )


@router.get("/channels/{channel_id}/playlists", response_model=PlaylistPage)
def channel_playlists(
    request: Request,
    channel_id: UUID,
    state: Reader,
    page: Page = 1,
    page_size: PageSize = 20,
) -> PlaylistPage:
    return service.channel_playlists(
        request.app.state.engine,
        channel_id,
        state.user.id if state and state.user else None,
        page,
        page_size,
    )


@router.get("/playlists", response_model=PlaylistPage)
def playlists(
    request: Request, user: SignedIn, page: Page = 1, page_size: PageSize = 20
) -> PlaylistPage:
    return service.playlists(
        request.app.state.engine, user.id, user.id, page, page_size
    )


@router.post("/playlists", response_model=PlaylistView, status_code=201)
def create_playlist(
    request: Request, data: PlaylistInput, user: Mutator
) -> PlaylistView:
    rate_limit(request, user, "playlist", 120)
    return service.create_playlist(request.app.state.engine, user.id, data)


@router.get("/playlists/{playlist_id}", response_model=PlaylistDetail)
def playlist(
    request: Request,
    playlist_id: UUID,
    state: Reader,
    page: Page = 1,
    page_size: PageSize = 20,
) -> PlaylistDetail:
    return service.playlist_detail(
        request.app.state.engine,
        playlist_id,
        state.user.id if state and state.user else None,
        page,
        page_size,
    )


@router.patch("/playlists/{playlist_id}", response_model=PlaylistView)
def update_playlist(
    request: Request, playlist_id: UUID, data: PlaylistPatch, user: Mutator
) -> PlaylistView:
    rate_limit(request, user, "playlist", 120)
    return service.update_playlist(request.app.state.engine, playlist_id, user.id, data)


@router.delete("/playlists/{playlist_id}", status_code=204)
def delete_playlist(request: Request, playlist_id: UUID, user: Mutator) -> Response:
    rate_limit(request, user, "playlist", 120)
    service.delete_playlist(request.app.state.engine, playlist_id, user.id)
    return Response(status_code=204)


@router.put("/playlists/{playlist_id}/items/{video_id}", status_code=204)
def add_item(
    request: Request, playlist_id: UUID, video_id: UUID, user: Mutator
) -> Response:
    rate_limit(request, user, "playlist-item", 240)
    service.set_playlist_item(
        request.app.state.engine, playlist_id, video_id, user.id, True
    )
    return Response(status_code=204)


@router.delete("/playlists/{playlist_id}/items/{video_id}", status_code=204)
def remove_item(
    request: Request, playlist_id: UUID, video_id: UUID, user: Mutator
) -> Response:
    rate_limit(request, user, "playlist-item", 240)
    service.set_playlist_item(
        request.app.state.engine, playlist_id, video_id, user.id, False
    )
    return Response(status_code=204)
