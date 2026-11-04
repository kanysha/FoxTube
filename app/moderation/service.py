from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import Engine, Row, Select, case, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.auth.models import User
from app.auth.rate_limits import check_limits
from app.moderation.models import AuditEvent, Report
from app.moderation.schemas import (
    AuditPage,
    AuditView,
    ModerationInput,
    ModerationVideoView,
    ReportPage,
    ReportReceipt,
    ReportStatus,
    ReportView,
    RoleInput,
    RoleUserView,
)
from app.videos.models import Video
from app.videos.service import readable

PAGE_SIZE = 20
# All privileged writes and the offline first-admin command use this same lock.
ROLE_LOCK = 742920450412


def require_role(session: Session, actor_id: UUID, *, admin: bool = False) -> None:
    role = session.scalar(select(User.role).where(User.id == actor_id))
    if role not in ({"admin"} if admin else {"admin", "moderator"}):
        raise HTTPException(403, "Недостаточно прав.")


def database_action(
    session: Session, statement: str, parameters: dict[str, Any]
) -> None:
    try:
        session.execute(text(statement), parameters)
    except DBAPIError as error:
        code = getattr(error.orig, "sqlstate", None)
        if code == "P0403":
            raise HTTPException(403, "Недостаточно прав.") from None
        if code == "P0404":
            raise HTTPException(404, "Объект не найден.") from None
        if code == "P0409":
            raise HTTPException(
                409,
                "Действие недоступно: жалоба закрыта или это последний администратор.",
            ) from None
        constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        if code == "23505" and constraint == "uq_reports_open_video_reporter":
            raise HTTPException(
                409, "Ваша открытая жалоба на это видео уже существует."
            ) from None
        raise


def report_video(
    engine: Engine, actor_id: UUID, video_id: UUID, reason: str
) -> ReportReceipt:
    check_limits(engine, [("reports:user", str(actor_id), 20)])
    report_id = uuid4()
    with Session(engine) as session, session.begin():
        database_action(
            session,
            "SELECT public.foxtube_report_video("
            ":actor, :video, :reason, :report, :audit)",
            {
                "actor": actor_id,
                "video": video_id,
                "reason": reason,
                "report": report_id,
                "audit": uuid4(),
            },
        )
        report = session.get(Report, report_id)
        assert report is not None
        return ReportReceipt(
            id=report.id,
            video_id=report.video_id,
            status=report.status,
            created_at=report.created_at,
        )


def _video_columns(actor_id: UUID) -> tuple[Any, ...]:
    can_read = readable(actor_id)
    return (
        Video.id.label("video_id"),
        case((can_read, Video.title), else_="Недоступное видео").label("video_title"),
        Video.visibility,
        Video.status.label("video_status"),
        Video.moderation_status,
        Video.deleted_at,
        can_read.label("can_read"),
    )


def _video_from_row(row: Row[Any]) -> ModerationVideoView:
    values = row._mapping
    return ModerationVideoView(
        id=values["video_id"],
        title=values["video_title"],
        visibility=values["visibility"],
        status=values["video_status"],
        moderation_status=values["moderation_status"],
        deleted=values["deleted_at"] is not None,
        watch_url=f"/watch/{values['video_id']}" if values["can_read"] else None,
    )


def _report_query(actor_id: UUID) -> Select[Any]:
    return select(Report, *_video_columns(actor_id)).join(
        Video, Video.id == Report.video_id
    )


def _report_from_row(row: Row[Any]) -> ReportView:
    report = cast(Report, row[0])
    return ReportView(
        id=report.id,
        video_id=report.video_id,
        status=report.status,
        created_at=report.created_at,
        reason=report.reason,
        resolution=report.resolution,
        resolved_at=report.resolved_at,
        video=_video_from_row(row),
    )


def list_reports(
    engine: Engine,
    actor_id: UUID,
    *,
    status: ReportStatus | None = "open",
    page: int = 1,
) -> ReportPage:
    with Session(engine) as session:
        require_role(session, actor_id)
        filters = [Report.status == status] if status is not None else []
        total = (
            session.scalar(select(func.count()).select_from(Report).where(*filters))
            or 0
        )
        rows = session.execute(
            _report_query(actor_id)
            .where(*filters)
            .order_by(Report.created_at.desc(), Report.id.desc())
            .limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)
        ).all()
        return ReportPage(
            items=[_report_from_row(row) for row in rows],
            page=page,
            page_size=PAGE_SIZE,
            total=total,
        )


def get_report(engine: Engine, actor_id: UUID, report_id: UUID) -> ReportView:
    with Session(engine) as session:
        require_role(session, actor_id)
        row = session.execute(
            _report_query(actor_id).where(Report.id == report_id)
        ).one_or_none()
        if row is None:
            raise HTTPException(404, "Жалоба не найдена.")
        return _report_from_row(row)


def moderate_video(
    engine: Engine, actor_id: UUID, video_id: UUID, data: ModerationInput
) -> ModerationVideoView:
    with Session(engine) as session, session.begin():
        database_action(
            session,
            "SELECT public.foxtube_moderate_video("
            ":actor, :video, :status, :reason, :audit)",
            {
                "actor": actor_id,
                "video": video_id,
                "status": data.status,
                "reason": data.reason,
                "audit": uuid4(),
            },
        )
        row = session.execute(
            select(*_video_columns(actor_id)).where(Video.id == video_id)
        ).one()
        return _video_from_row(row)


def resolve_report(
    engine: Engine, actor_id: UUID, report_id: UUID, resolution: str
) -> ReportView:
    with Session(engine) as session, session.begin():
        database_action(
            session,
            "SELECT public.foxtube_resolve_report("
            ":actor, :report, :resolution, :audit)",
            {
                "actor": actor_id,
                "report": report_id,
                "resolution": resolution,
                "audit": uuid4(),
            },
        )
        row = session.execute(
            _report_query(actor_id).where(Report.id == report_id)
        ).one()
        return _report_from_row(row)


def _find_user(session: Session, email: str | None, user_id: UUID | None) -> User:
    if (email is None) == (user_id is None):
        raise HTTPException(422, "Укажите точный email или ID пользователя.")
    user = session.scalar(
        select(User).where(User.id == user_id if user_id else User.email == email)
    )
    if user is None:
        raise HTTPException(404, "Пользователь не найден.")
    return user


def _user_view(user: User) -> RoleUserView:
    return RoleUserView(
        id=user.id, email=user.email, display_name=user.display_name, role=user.role
    )


def find_user(
    engine: Engine,
    actor_id: UUID,
    *,
    email: str | None = None,
    user_id: UUID | None = None,
) -> RoleUserView:
    with Session(engine) as session:
        require_role(session, actor_id, admin=True)
        return _user_view(_find_user(session, email, user_id))


def change_role(engine: Engine, actor_id: UUID, data: RoleInput) -> RoleUserView:
    with Session(engine) as session, session.begin():
        require_role(session, actor_id, admin=True)
        target = _find_user(
            session, str(data.email) if data.email else None, data.user_id
        )
        database_action(
            session,
            "SELECT public.foxtube_set_role(:actor, :target, :role, :reason, :audit)",
            {
                "actor": actor_id,
                "target": target.id,
                "role": data.role,
                "reason": data.reason,
                "audit": uuid4(),
            },
        )
        session.refresh(target)
        return _user_view(target)


def list_audit(engine: Engine, actor_id: UUID, *, page: int = 1) -> AuditPage:
    with Session(engine) as session:
        require_role(session, actor_id, admin=True)
        total = session.scalar(select(func.count()).select_from(AuditEvent)) or 0
        events = session.scalars(
            select(AuditEvent)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)
        ).all()
        return AuditPage(
            items=[
                AuditView.model_validate(event, from_attributes=True)
                for event in events
            ],
            page=page,
            page_size=PAGE_SIZE,
            total=total,
        )
