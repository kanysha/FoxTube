import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import Engine, and_, exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.auth.models import Channel, User
from app.videos import limits
from app.videos.models import Video, VideoGrant, VideoJob
from app.videos.schemas import (
    GrantPage,
    GrantView,
    VideoInput,
    VideoPage,
    VideoPatch,
    VideoView,
)


def capacity_lock(session: Session) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": limits.CAPACITY_LOCK}
    )


def readable(viewer_id: UUID | None) -> ColumnElement[bool]:
    permitted = Video.visibility == "public"
    if viewer_id is not None:
        permitted = or_(
            permitted,
            Video.owner_id == viewer_id,
            exists(
                select(1).where(
                    VideoGrant.video_id == Video.id, VideoGrant.user_id == viewer_id
                )
            ),
        )
    return and_(
        Video.deleted_at.is_(None),
        Video.moderation_status == "visible",
        Video.status == "READY",
        permitted,
    )


def view(video: Video, channel: Channel, viewer_id: UUID | None) -> VideoView:
    ready = (
        video.status == "READY"
        and video.moderation_status == "visible"
        and video.deleted_at is None
    )
    return VideoView(
        id=video.id,
        title=video.title,
        description=video.description,
        visibility=video.visibility,
        status=video.status,
        error=video.error,
        duration=video.duration,
        width=video.width,
        height=video.height,
        created_at=video.created_at,
        owner_id=video.owner_id,
        channel_id=channel.id,
        channel_name=channel.title,
        can_manage=video.owner_id == viewer_id,
        playback_url=f"/media/videos/{video.id}/playback" if ready else None,
        poster_url=f"/media/videos/{video.id}/poster" if ready else None,
    )


def list_videos(
    engine: Engine,
    viewer_id: UUID | None,
    query: str = "",
    page: int = 1,
    mine: bool = False,
    page_size: int = 20,
) -> VideoPage:
    predicate = readable(viewer_id)
    if mine:
        if viewer_id is None:
            raise HTTPException(401, "Необходим вход.")
        predicate = and_(Video.owner_id == viewer_id, Video.deleted_at.is_(None))
    if query:
        # Literal substring search: user '%' and '_' must not expand the query.
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        predicate = and_(predicate, Video.title.ilike(f"%{escaped}%", escape="\\"))
    page_size = max(1, min(page_size, 50))
    page = max(1, min(page, 10000))
    with Session(engine) as session:
        total = (
            session.scalar(select(func.count()).select_from(Video).where(predicate))
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
            items=[view(v, c, viewer_id) for v, c in rows],
            page=page,
            page_size=page_size,
            total=total,
        )


def get_video(engine: Engine, video_id: UUID, viewer_id: UUID | None) -> VideoView:
    predicate = readable(viewer_id)
    if viewer_id is not None:
        predicate = or_(
            predicate, and_(Video.owner_id == viewer_id, Video.deleted_at.is_(None))
        )
    with Session(engine) as session:
        row = session.execute(
            select(Video, Channel)
            .join(Channel, Channel.user_id == Video.owner_id)
            .where(Video.id == video_id, predicate)
        ).first()
        if row is None:
            raise HTTPException(404, "Видео недоступно.")
        return view(row[0], row[1], viewer_id)


def media_key(
    engine: Engine, video_id: UUID, viewer_id: UUID | None, poster: bool
) -> str:
    with Session(engine) as session:
        key = session.scalar(
            select(Video.poster_key if poster else Video.playback_key).where(
                Video.id == video_id, readable(viewer_id)
            )
        )
        if key is None:
            raise HTTPException(404, "Видео недоступно.")
        return key


def owned(session: Session, video_id: UUID, user_id: UUID, lock: bool = False) -> Video:
    statement = select(Video).where(
        Video.id == video_id, Video.owner_id == user_id, Video.deleted_at.is_(None)
    )
    video = session.scalar(statement.with_for_update() if lock else statement)
    if video is None:
        raise HTTPException(404, "Видео недоступно.")
    return video


def reserve_capacity(session: Session, root: Path, user_id: UUID, reserve: int) -> None:
    pending = (
        session.scalar(
            select(func.count())
            .select_from(Video)
            .where(
                Video.deleted_at.is_(None),
                or_(
                    Video.status.in_(("QUEUED", "PROCESSING")),
                    and_(Video.status == "UPLOADING", Video.reserved_bytes > 0),
                ),
            )
        )
        or 0
    )
    if pending >= limits.MAX_QUEUE:
        raise HTTPException(
            429,
            "Очередь заполнена. Повторите позже.",
            headers={"Retry-After": "60"},
        )
    usage = (
        session.scalar(
            select(
                func.coalesce(func.sum(Video.storage_bytes + Video.reserved_bytes), 0)
            ).where(Video.owner_id == user_id)
        )
        or 0
    )
    if usage + reserve > limits.USER_QUOTA:
        raise HTTPException(413, "Недостаточно квоты: на аккаунт доступно 5 ГиБ.")
    reserved = (
        session.scalar(select(func.coalesce(func.sum(Video.reserved_bytes), 0))) or 0
    )
    if shutil.disk_usage(root).free - reserved - reserve < limits.DISK_FLOOR:
        raise HTTPException(507, "В хранилище недостаточно свободного места.")


def create_video(
    engine: Engine, root: Path, user_id: UUID, data: VideoInput
) -> VideoView:
    now = datetime.now(UTC)
    reserve = data.expected_size + limits.OUTPUT_RESERVATION
    with Session(engine) as session, session.begin():
        capacity_lock(session)
        draft = session.scalar(
            select(Video.id)
            .where(
                Video.owner_id == user_id,
                Video.deleted_at.is_(None),
                Video.status == "UPLOADING",
                Video.reservation_expires_at > now,
            )
            .limit(1)
        )
        if draft is not None:
            raise HTTPException(
                409, "Завершите или отмените предыдущую загрузку в разделе «Мои видео»."
            )
        reserve_capacity(session, root, user_id, reserve)
        video = Video(
            owner_id=user_id,
            title=data.title,
            description=data.description,
            visibility=data.visibility,
            expected_size=data.expected_size,
            reserved_bytes=reserve,
            reservation_expires_at=now + timedelta(seconds=limits.RESERVATION_SECONDS),
        )
        session.add(video)
        session.flush()
        video_id = video.id
    return get_video(engine, video_id, user_id)


def patch_video(
    engine: Engine, video_id: UUID, user_id: UUID, data: VideoPatch
) -> VideoView:
    with Session(engine) as session, session.begin():
        video = owned(session, video_id, user_id, lock=True)
        for key, value in data.model_dump(exclude_none=True).items():
            setattr(video, key, value)
        video.updated_at = datetime.now(UTC)
    return get_video(engine, video_id, user_id)


def delete_video(engine: Engine, video_id: UUID, user_id: UUID) -> None:
    with Session(engine) as session, session.begin():
        video = owned(session, video_id, user_id, lock=True)
        video.deleted_at = datetime.now(UTC)
        video.updated_at = datetime.now(UTC)


def list_grants(engine: Engine, video_id: UUID, user_id: UUID) -> GrantPage:
    with Session(engine) as session:
        owned(session, video_id, user_id)
        rows = session.execute(
            select(User.id, User.email, User.display_name)
            .join(VideoGrant, VideoGrant.user_id == User.id)
            .where(VideoGrant.video_id == video_id)
            .order_by(User.email)
            .limit(100)
        ).all()
        return GrantPage(
            items=[GrantView(user_id=i, email=e, display_name=n) for i, e, n in rows]
        )


def add_grant(engine: Engine, video_id: UUID, user_id: UUID, email: str) -> None:
    with Session(engine) as session, session.begin():
        owned(session, video_id, user_id, lock=True)
        recipient = session.scalar(select(User.id).where(User.email == email))
        if recipient is None:
            raise HTTPException(422, "Пользователь с этим email не зарегистрирован.")
        if recipient == user_id:
            return
        count = (
            session.scalar(
                select(func.count())
                .select_from(VideoGrant)
                .where(VideoGrant.video_id == video_id)
            )
            or 0
        )
        present = session.get(VideoGrant, (video_id, recipient))
        if count >= 100 and present is None:
            raise HTTPException(422, "Доступ можно выдать не более 100 пользователям.")
        session.execute(
            insert(VideoGrant)
            .values(video_id=video_id, user_id=recipient)
            .on_conflict_do_nothing()
        )


def revoke_grant(
    engine: Engine, video_id: UUID, user_id: UUID, recipient: UUID
) -> None:
    with Session(engine) as session, session.begin():
        owned(session, video_id, user_id, lock=True)
        grant = session.get(VideoGrant, (video_id, recipient))
        if grant:
            session.delete(grant)


@dataclass(frozen=True)
class UploadTicket:
    video_id: UUID
    token: UUID
    expected_size: int

    @property
    def part_key(self) -> str:
        return f"incoming/{self.video_id}/{self.token}.part"

    @property
    def source_key(self) -> str:
        return f"sources/{self.video_id}/{self.token}.source"


def begin_upload(
    engine: Engine, root: Path, video_id: UUID, user_id: UUID, length: int | None
) -> UploadTicket | None:
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        capacity_lock(session)
        video = owned(session, video_id, user_id, lock=True)
        if video.status in ("QUEUED", "PROCESSING", "READY"):
            return None  # Already accepted: don't consume or persist the body again.
        if video.status != "UPLOADING" or video.reservation_expires_at <= now:
            raise HTTPException(409, "Срок загрузки истёк. Создайте новую загрузку.")
        if length is not None and length != video.expected_size:
            raise HTTPException(
                413 if length > video.expected_size else 400,
                "Размер файла не совпадает с заявленным.",
            )
        active = and_(Video.upload_token.is_not(None), Video.upload_expires_at > now)
        if session.scalar(
            select(func.count())
            .select_from(Video)
            .where(active, Video.owner_id == user_id)
        ):
            raise HTTPException(
                409, "У вас уже идёт загрузка. Дождитесь её завершения."
            )
        count = (
            session.scalar(select(func.count()).select_from(Video).where(active)) or 0
        )
        if count >= limits.MAX_UPLOADS:
            raise HTTPException(
                429,
                "Сервер занят загрузками. Повторите позже.",
                headers={"Retry-After": "10"},
            )
        if video.reserved_bytes == 0:
            reserve = video.expected_size + limits.OUTPUT_RESERVATION
            reserve_capacity(session, root, user_id, reserve)
            video.reserved_bytes = reserve
        ticket = UploadTicket(video.id, uuid4(), video.expected_size)
        video.reservation_expires_at = now + timedelta(seconds=1800)
        video.upload_token = ticket.token
        video.upload_expires_at = now + timedelta(seconds=limits.UPLOAD_LEASE_SECONDS)
        return ticket


def renew_upload(engine: Engine, ticket: UploadTicket) -> None:
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        video = session.scalar(
            select(Video).where(Video.id == ticket.video_id).with_for_update()
        )
        if (
            video is None
            or video.status != "UPLOADING"
            or video.deleted_at is not None
            or video.upload_token != ticket.token
            or video.upload_expires_at is None
            or video.upload_expires_at <= now
            or video.reservation_expires_at <= now
        ):
            raise HTTPException(409, "Загрузка отменена или срок передачи истёк.")
        video.upload_expires_at = now + timedelta(seconds=limits.UPLOAD_LEASE_SECONDS)


def finish_upload(engine: Engine, ticket: UploadTicket) -> None:
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        capacity_lock(session)
        video = session.scalar(
            select(Video).where(Video.id == ticket.video_id).with_for_update()
        )
        if (
            video is None
            or video.deleted_at is not None
            or video.status != "UPLOADING"
            or video.upload_token != ticket.token
            or video.upload_expires_at is None
            or video.upload_expires_at <= now
            or video.reservation_expires_at <= now
        ):
            raise HTTPException(409, "Загрузка отменена или срок передачи истёк.")
        video.status = "QUEUED"
        video.source_key = ticket.source_key
        video.storage_bytes = ticket.expected_size
        video.reserved_bytes = limits.OUTPUT_RESERVATION
        video.upload_token = None
        video.upload_expires_at = None
        video.updated_at = now
        session.add(VideoJob(video_id=video.id))


def abort_upload(engine: Engine, ticket: UploadTicket) -> bool:
    with Session(engine) as session, session.begin():
        capacity_lock(session)
        video = session.scalar(
            select(Video).where(Video.id == ticket.video_id).with_for_update()
        )
        if video and video.status == "UPLOADING" and video.upload_token == ticket.token:
            video.upload_token = None
            video.upload_expires_at = None
            video.reserved_bytes = 0
        return video is None or video.source_key != ticket.source_key
