from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Engine, and_, delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Subquery

from app.auth.models import Channel, User
from app.social.models import Comment, Like, Playlist, PlaylistItem, Subscription
from app.social.schemas import (
    CommentInput,
    CommentPage,
    CommentView,
    PlaylistDetail,
    PlaylistInput,
    PlaylistPage,
    PlaylistPatch,
    PlaylistView,
    SubscribedChannel,
    SubscribedChannelPage,
    SubscriptionView,
    VideoSocial,
)
from app.videos.models import Video
from app.videos.schemas import VideoPage
from app.videos.service import readable, view

MAX_PLAYLISTS = 100
MAX_PLAYLIST_ITEMS = 500
MAX_SUBSCRIPTIONS = 1000
MAX_VIDEO_COMMENTS = 10000


def pagination(page: int, page_size: int) -> tuple[int, int]:
    return max(1, min(page, 10000)), max(1, min(page_size, 50))


def accessible_video(
    session: Session, video_id: UUID, viewer_id: UUID | None, *, lock: bool = False
) -> Video:
    if lock:
        # Lock first, then read ACL in a new READ COMMITTED statement snapshot.
        # A grant can be revoked while this request waits for the video lock,
        # without changing the video tuple that PostgreSQL would recheck.
        video = session.scalar(
            select(Video).where(Video.id == video_id).with_for_update()
        )
        allowed = session.scalar(
            select(Video.id).where(Video.id == video_id, readable(viewer_id))
        )
        if allowed is None:
            video = None
    else:
        video = session.scalar(
            select(Video).where(Video.id == video_id, readable(viewer_id))
        )
    if video is None:
        raise HTTPException(404, "Видео недоступно.")
    return video


def video_social(engine: Engine, video_id: UUID, viewer_id: UUID | None) -> VideoSocial:
    with Session(engine) as session:
        accessible_video(session, video_id, viewer_id)
        likes = select(func.count()).where(Like.video_id == video_id).scalar_subquery()
        comments = (
            select(func.count()).where(Comment.video_id == video_id).scalar_subquery()
        )
        liked = exists(
            select(1).where(Like.video_id == video_id, Like.user_id == viewer_id)
        )
        row = session.execute(select(likes, comments, liked)).one()
        return VideoSocial(likes_count=row[0], comments_count=row[1], liked=row[2])


def set_like(engine: Engine, video_id: UUID, user_id: UUID, enabled: bool) -> None:
    with Session(engine) as session, session.begin():
        # The ACL and mutation share the video's lock with revoke/hide/delete.
        accessible_video(session, video_id, user_id, lock=True)
        if enabled:
            session.execute(
                insert(Like)
                .values(video_id=video_id, user_id=user_id)
                .on_conflict_do_nothing()
            )
        else:
            session.execute(
                delete(Like).where(Like.video_id == video_id, Like.user_id == user_id)
            )


def comments(
    engine: Engine,
    video_id: UUID,
    viewer_id: UUID | None,
    page: int = 1,
    page_size: int = 20,
) -> CommentPage:
    page, page_size = pagination(page, page_size)
    with Session(engine) as session:
        video = accessible_video(session, video_id, viewer_id)
        predicate = and_(Comment.video_id == video_id, readable(viewer_id))
        total = (
            session.scalar(
                select(func.count())
                .select_from(Comment)
                .join(Video, Video.id == Comment.video_id)
                .where(predicate)
            )
            or 0
        )
        rows = session.execute(
            select(Comment, User.display_name, Channel.id)
            .join(Video, Video.id == Comment.video_id)
            .join(User, User.id == Comment.user_id)
            .join(Channel, Channel.user_id == Comment.user_id)
            .where(predicate)
            .order_by(Comment.created_at.desc(), Comment.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return CommentPage(
            items=[
                CommentView(
                    id=comment.id,
                    body=comment.body,
                    created_at=comment.created_at,
                    author_id=comment.user_id,
                    author_name=name,
                    channel_id=channel_id,
                    can_delete=viewer_id in (comment.user_id, video.owner_id),
                )
                for comment, name, channel_id in rows
            ],
            total=total,
            page=page,
            page_size=page_size,
        )


def add_comment(
    engine: Engine, video_id: UUID, user_id: UUID, data: CommentInput
) -> CommentView:
    with Session(engine) as session, session.begin():
        accessible_video(session, video_id, user_id, lock=True)
        count = (
            session.scalar(select(func.count()).where(Comment.video_id == video_id))
            or 0
        )
        if count >= MAX_VIDEO_COMMENTS:
            raise HTTPException(422, "Для этого видео достигнут лимит комментариев.")
        comment = Comment(video_id=video_id, user_id=user_id, body=data.body)
        session.add(comment)
        session.flush()
        name, channel_id = session.execute(
            select(User.display_name, Channel.id)
            .join(Channel, Channel.user_id == User.id)
            .where(User.id == user_id)
        ).one()
        return CommentView(
            id=comment.id,
            body=comment.body,
            created_at=comment.created_at,
            author_id=user_id,
            author_name=name,
            channel_id=channel_id,
            can_delete=True,
        )


def delete_comment(engine: Engine, comment_id: UUID, user_id: UUID) -> None:
    with Session(engine) as session, session.begin():
        row = session.execute(
            select(Comment, Video)
            .join(Video, Video.id == Comment.video_id)
            .where(Comment.id == comment_id, readable(user_id))
            .with_for_update(of=Video)
        ).first()
        if row is None:
            raise HTTPException(404, "Комментарий недоступен.")
        comment, video = row
        # Refresh ACL after any lock wait (grant revocation need not update Video).
        if (
            session.scalar(
                select(Video.id).where(Video.id == video.id, readable(user_id))
            )
            is None
        ):
            raise HTTPException(404, "Комментарий недоступен.")
        if user_id not in (comment.user_id, video.owner_id):
            raise HTTPException(404, "Комментарий недоступен.")
        session.execute(delete(Comment).where(Comment.id == comment_id))


def subscription(
    engine: Engine, channel_id: UUID, viewer_id: UUID | None
) -> SubscriptionView:
    with Session(engine) as session:
        owner_id = session.scalar(
            select(Channel.user_id).where(Channel.id == channel_id)
        )
        if owner_id is None:
            raise HTTPException(404, "Канал не найден.")
        count, subscribed = session.execute(
            select(
                select(func.count())
                .where(Subscription.channel_id == channel_id)
                .scalar_subquery(),
                exists(
                    select(1).where(
                        Subscription.channel_id == channel_id,
                        Subscription.user_id == viewer_id,
                    )
                ),
            )
        ).one()
        return SubscriptionView(
            subscribers_count=count,
            subscribed=subscribed,
            can_subscribe=viewer_id is not None and viewer_id != owner_id,
        )


def set_subscription(
    engine: Engine, channel_id: UUID, user_id: UUID, enabled: bool
) -> None:
    with Session(engine) as session, session.begin():
        # Per-user lock makes the subscription cap safe across API processes.
        session.scalar(select(User.id).where(User.id == user_id).with_for_update())
        owner_id = session.scalar(
            select(Channel.user_id).where(Channel.id == channel_id)
        )
        if owner_id is None:
            raise HTTPException(404, "Канал не найден.")
        if owner_id == user_id:
            raise HTTPException(422, "На собственный канал подписаться нельзя.")
        if enabled:
            if session.get(Subscription, (channel_id, user_id)) is not None:
                return
            count = (
                session.scalar(
                    select(func.count()).where(Subscription.user_id == user_id)
                )
                or 0
            )
            if count >= MAX_SUBSCRIPTIONS:
                raise HTTPException(422, "Можно подписаться максимум на 1000 каналов.")
            session.execute(
                insert(Subscription)
                .values(channel_id=channel_id, user_id=user_id)
                .on_conflict_do_nothing()
            )
        else:
            session.execute(
                delete(Subscription).where(
                    Subscription.channel_id == channel_id,
                    Subscription.user_id == user_id,
                )
            )


def subscribed_channels(
    engine: Engine, user_id: UUID, page: int = 1, page_size: int = 20
) -> SubscribedChannelPage:
    page, page_size = pagination(page, page_size)
    with Session(engine) as session:
        total = (
            session.scalar(select(func.count()).where(Subscription.user_id == user_id))
            or 0
        )
        rows = (
            session.execute(
                select(Channel)
                .join(Subscription, Subscription.channel_id == Channel.id)
                .where(Subscription.user_id == user_id)
                .order_by(Subscription.created_at.desc(), Channel.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        return SubscribedChannelPage(
            items=[
                SubscribedChannel(id=c.id, title=c.title, handle=c.handle) for c in rows
            ],
            total=total,
            page=page,
            page_size=page_size,
        )


def subscriptions_feed(
    engine: Engine, user_id: UUID, page: int = 1, page_size: int = 20
) -> VideoPage:
    page, page_size = pagination(page, page_size)
    predicate = and_(
        readable(user_id),
        exists(
            select(1).where(
                Subscription.channel_id == Channel.id, Subscription.user_id == user_id
            )
        ),
    )
    with Session(engine) as session:
        total = (
            session.scalar(
                select(func.count())
                .select_from(Video)
                .join(Channel, Channel.user_id == Video.owner_id)
                .where(predicate)
            )
            or 0
        )
        rows = session.execute(
            select(Video, Channel)
            .join(Channel, Channel.user_id == Video.owner_id)
            .where(predicate)
            .order_by(Video.created_at.desc(), Video.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return VideoPage(
            items=[view(video, channel, user_id) for video, channel in rows],
            total=total,
            page=page,
            page_size=page_size,
        )


def playlist_readable(viewer_id: UUID | None) -> ColumnElement[bool]:
    if viewer_id is None:
        return Playlist.visibility == "public"
    return or_(Playlist.visibility == "public", Playlist.owner_id == viewer_id)


def playlist_view(
    playlist: Playlist, channel: Channel, viewer_id: UUID | None, count: int
) -> PlaylistView:
    return PlaylistView(
        id=playlist.id,
        title=playlist.title,
        description=playlist.description,
        visibility=playlist.visibility,
        owner_id=playlist.owner_id,
        channel_id=channel.id,
        channel_name=channel.title,
        created_at=playlist.created_at,
        can_manage=playlist.owner_id == viewer_id,
        videos_count=count,
    )


def visible_item_counts(viewer_id: UUID | None, owner_id: UUID) -> Subquery:
    # One grouped query for the whole page. Never count inaccessible items.
    return (
        select(PlaylistItem.playlist_id, func.count().label("videos_count"))
        .join(Playlist, Playlist.id == PlaylistItem.playlist_id)
        .join(Video, Video.id == PlaylistItem.video_id)
        .where(Playlist.owner_id == owner_id, readable(viewer_id))
        .group_by(PlaylistItem.playlist_id)
        .subquery()
    )


def playlists(
    engine: Engine,
    owner_id: UUID,
    viewer_id: UUID | None,
    page: int = 1,
    page_size: int = 20,
) -> PlaylistPage:
    page, page_size = pagination(page, page_size)
    predicate = and_(Playlist.owner_id == owner_id, playlist_readable(viewer_id))
    counts = visible_item_counts(viewer_id, owner_id)
    visible_count: ColumnElement[int] = func.coalesce(counts.c.videos_count, 0)
    with Session(engine) as session:
        total = (
            session.scalar(select(func.count()).select_from(Playlist).where(predicate))
            or 0
        )
        rows = session.execute(
            select(Playlist, Channel, visible_count)
            .join(Channel, Channel.user_id == Playlist.owner_id)
            .outerjoin(counts, counts.c.playlist_id == Playlist.id)
            .where(predicate)
            .order_by(Playlist.created_at.desc(), Playlist.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return PlaylistPage(
            items=[playlist_view(p, c, viewer_id, count) for p, c, count in rows],
            total=total,
            page=page,
            page_size=page_size,
        )


def channel_playlists(
    engine: Engine,
    channel_id: UUID,
    viewer_id: UUID | None,
    page: int = 1,
    page_size: int = 20,
) -> PlaylistPage:
    with Session(engine) as session:
        owner_id = session.scalar(
            select(Channel.user_id).where(Channel.id == channel_id)
        )
        if owner_id is None:
            raise HTTPException(404, "Канал не найден.")
    return playlists(engine, owner_id, viewer_id, page, page_size)


def playlist_detail(
    engine: Engine,
    playlist_id: UUID,
    viewer_id: UUID | None,
    page: int = 1,
    page_size: int = 20,
) -> PlaylistDetail:
    page, page_size = pagination(page, page_size)
    with Session(engine) as session:
        row = session.execute(
            select(Playlist, Channel)
            .join(Channel, Channel.user_id == Playlist.owner_id)
            .where(Playlist.id == playlist_id, playlist_readable(viewer_id))
        ).first()
        if row is None:
            raise HTTPException(404, "Плейлист недоступен.")
        predicate = and_(PlaylistItem.playlist_id == playlist_id, readable(viewer_id))
        total = (
            session.scalar(
                select(func.count())
                .select_from(PlaylistItem)
                .join(Video, Video.id == PlaylistItem.video_id)
                .where(predicate)
            )
            or 0
        )
        rows = session.execute(
            select(Video, Channel)
            .join(PlaylistItem, PlaylistItem.video_id == Video.id)
            .join(Channel, Channel.user_id == Video.owner_id)
            .where(predicate)
            .order_by(PlaylistItem.created_at, PlaylistItem.video_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return PlaylistDetail(
            playlist=playlist_view(row[0], row[1], viewer_id, total),
            videos=VideoPage(
                items=[view(video, channel, viewer_id) for video, channel in rows],
                total=total,
                page=page,
                page_size=page_size,
            ),
        )


def owned_playlist(session: Session, playlist_id: UUID, user_id: UUID) -> Playlist:
    playlist = session.scalar(
        select(Playlist)
        .where(Playlist.id == playlist_id, Playlist.owner_id == user_id)
        .with_for_update()
    )
    if playlist is None:
        raise HTTPException(404, "Плейлист недоступен.")
    return playlist


def create_playlist(engine: Engine, user_id: UUID, data: PlaylistInput) -> PlaylistView:
    with Session(engine) as session, session.begin():
        session.scalar(select(User.id).where(User.id == user_id).with_for_update())
        count = (
            session.scalar(select(func.count()).where(Playlist.owner_id == user_id))
            or 0
        )
        if count >= MAX_PLAYLISTS:
            raise HTTPException(422, "Можно создать максимум 100 плейлистов.")
        playlist = Playlist(owner_id=user_id, **data.model_dump())
        session.add(playlist)
        session.flush()
        channel = session.scalars(
            select(Channel).where(Channel.user_id == user_id)
        ).one()
        return playlist_view(playlist, channel, user_id, 0)


def update_playlist(
    engine: Engine, playlist_id: UUID, user_id: UUID, data: PlaylistPatch
) -> PlaylistView:
    with Session(engine) as session, session.begin():
        playlist = owned_playlist(session, playlist_id, user_id)
        for key, value in data.model_dump(exclude_none=True).items():
            setattr(playlist, key, value)
        playlist.updated_at = datetime.now(UTC)
    return playlist_detail(engine, playlist_id, user_id).playlist


def delete_playlist(engine: Engine, playlist_id: UUID, user_id: UUID) -> None:
    with Session(engine) as session, session.begin():
        playlist = owned_playlist(session, playlist_id, user_id)
        session.delete(playlist)


def set_playlist_item(
    engine: Engine, playlist_id: UUID, video_id: UUID, user_id: UUID, enabled: bool
) -> None:
    with Session(engine) as session, session.begin():
        playlist = owned_playlist(session, playlist_id, user_id)
        if enabled:
            accessible_video(session, video_id, user_id, lock=True)
            if session.get(PlaylistItem, (playlist_id, video_id)) is not None:
                return
            count = (
                session.scalar(
                    select(func.count()).where(PlaylistItem.playlist_id == playlist_id)
                )
                or 0
            )
            if count >= MAX_PLAYLIST_ITEMS:
                raise HTTPException(422, "В плейлисте может быть максимум 500 видео.")
            session.execute(
                insert(PlaylistItem)
                .values(playlist_id=playlist_id, video_id=video_id)
                .on_conflict_do_nothing()
            )
        else:
            session.execute(
                delete(PlaylistItem).where(
                    PlaylistItem.playlist_id == playlist_id,
                    PlaylistItem.video_id == video_id,
                )
            )
        playlist.updated_at = datetime.now(UTC)
