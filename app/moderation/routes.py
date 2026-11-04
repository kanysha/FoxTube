from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import EmailStr

from app.auth.dependencies import required_user
from app.auth.schemas import UserView
from app.moderation import service
from app.moderation.schemas import (
    AuditPage,
    ModerationInput,
    ModerationVideoView,
    ReportInput,
    ReportPage,
    ReportReceipt,
    ReportStatus,
    ReportView,
    ResolutionInput,
    RoleInput,
    RoleUserView,
)
from app.videos.routes import Mutator

router = APIRouter(prefix="/api/v1", tags=["Модерация и роли"])
Reader = Annotated[UserView, Depends(required_user)]


@router.post(
    "/videos/{video_id}/reports", response_model=ReportReceipt, status_code=201
)
def report_video(
    request: Request, video_id: UUID, data: ReportInput, user: Mutator
) -> ReportReceipt:
    return service.report_video(
        request.app.state.engine, user.id, video_id, data.reason
    )


@router.get("/moderation/reports", response_model=ReportPage)
def reports(
    request: Request,
    user: Reader,
    status: ReportStatus | None = "open",
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
) -> ReportPage:
    return service.list_reports(
        request.app.state.engine, user.id, status=status, page=page
    )


@router.get("/moderation/reports/{report_id}", response_model=ReportView)
def report(request: Request, report_id: UUID, user: Reader) -> ReportView:
    return service.get_report(request.app.state.engine, user.id, report_id)


@router.post("/moderation/reports/{report_id}/resolve", response_model=ReportView)
def resolve_report(
    request: Request, report_id: UUID, data: ResolutionInput, user: Mutator
) -> ReportView:
    return service.resolve_report(
        request.app.state.engine, user.id, report_id, data.resolution
    )


@router.patch("/moderation/videos/{video_id}", response_model=ModerationVideoView)
def moderate_video(
    request: Request, video_id: UUID, data: ModerationInput, user: Mutator
) -> ModerationVideoView:
    return service.moderate_video(request.app.state.engine, user.id, video_id, data)


@router.get("/admin/users", response_model=RoleUserView)
def admin_user(
    request: Request,
    user: Reader,
    email: Annotated[EmailStr | None, Query(max_length=254)] = None,
    user_id: UUID | None = None,
) -> RoleUserView:
    return service.find_user(
        request.app.state.engine,
        user.id,
        email=str(email).strip().casefold() if email else None,
        user_id=user_id,
    )


@router.patch("/admin/roles", response_model=RoleUserView)
def change_role(request: Request, data: RoleInput, user: Mutator) -> RoleUserView:
    return service.change_role(request.app.state.engine, user.id, data)


@router.get("/admin/audit", response_model=AuditPage)
def audit(
    request: Request,
    user: Reader,
    page: Annotated[int, Query(ge=1, le=10000)] = 1,
) -> AuditPage:
    return service.list_audit(request.app.state.engine, user.id, page=page)
