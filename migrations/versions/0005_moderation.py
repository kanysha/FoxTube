"""Reports, moderation, audited role changes and runtime privilege boundaries.

Revision ID: 0005_moderation
Revises: 0004_social
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_moderation"
down_revision = "0004_social"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "video_id",
            sa.Uuid(),
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reporter_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column(
            "resolved_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="reports_status"),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 1000", name="reports_reason"
        ),
        sa.CheckConstraint(
            "(status = 'open' AND resolution IS NULL AND resolved_at IS NULL "
            "AND resolved_by IS NULL) OR (status = 'resolved' "
            "AND resolution IS NOT NULL "
            "AND length(trim(resolution)) BETWEEN 1 AND 1000 "
            "AND resolved_at IS NOT NULL)",
            name="reports_resolution",
        ),
    )
    op.create_index(
        "ix_reports_status_created", "reports", ["status", "created_at", "id"]
    )
    op.create_index("ix_reports_video_id", "reports", ["video_id"])
    op.create_index(
        "uq_reports_open_video_reporter",
        "reports",
        ["video_id", "reporter_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "actor_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("video_id", sa.Uuid(), nullable=True),
        sa.Column("report_id", sa.Uuid(), nullable=True),
        sa.Column("target_user_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("previous_value", sa.String(32), nullable=True),
        sa.Column("new_value", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "action IN ('report.created', 'report.resolved', 'video.hidden', "
            "'video.restored', 'role.changed', 'role.bootstrap')",
            name="audit_events_action",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 1000", name="audit_events_reason"
        ),
    )
    op.create_index("ix_audit_events_created", "audit_events", ["created_at", "id"])
    op.execute("GRANT SELECT ON reports, audit_events TO foxtube_api")
    # No direct runtime UPDATE of users.role / videos.moderation_status, and no
    # INSERT, UPDATE or DELETE of the audit trail. Every write below is atomic.
    op.execute("""
        CREATE FUNCTION public.foxtube_report_video(
            p_actor uuid, p_video uuid, p_reason text, p_report uuid, p_audit uuid
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        BEGIN
            IF p_reason IS NULL OR length(btrim(p_reason)) NOT BETWEEN 1 AND 1000 THEN
                RAISE EXCEPTION 'Invalid reason' USING ERRCODE = '22023';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.users WHERE id = p_actor) THEN
                RAISE EXCEPTION 'Unauthorized' USING ERRCODE = 'P0403';
            END IF;
            -- Lock first, then evaluate ACL in a separate statement. A concurrent
            -- grant revocation may leave Video unchanged: reusing a pre-wait
            -- snapshot of video_grants would otherwise accept the revoked grant.
            PERFORM v.id FROM public.videos v WHERE v.id = p_video FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Video not found' USING ERRCODE = 'P0404';
            END IF;
            PERFORM v.id FROM public.videos v
            WHERE v.id = p_video AND v.deleted_at IS NULL AND v.status = 'READY'
              AND v.moderation_status = 'visible'
              AND (v.visibility = 'public' OR v.owner_id = p_actor OR EXISTS (
                  SELECT 1 FROM public.video_grants g
                  WHERE g.video_id = v.id AND g.user_id = p_actor
              ));
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Video not found' USING ERRCODE = 'P0404';
            END IF;
            INSERT INTO public.reports (id, video_id, reporter_id, reason)
            VALUES (p_report, p_video, p_actor, btrim(p_reason));
            INSERT INTO public.audit_events
                (id, actor_id, action, video_id, report_id, reason, new_value)
            VALUES (p_audit, p_actor, 'report.created', p_video, p_report,
                    btrim(p_reason), 'open');
            RETURN p_report;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.foxtube_moderate_video(
            p_actor uuid, p_video uuid, p_status text, p_reason text, p_audit uuid
        ) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE previous text;
        BEGIN
            -- Also used by role changes/bootstrap. Recheck role after acquiring it.
            PERFORM pg_advisory_xact_lock(742920450412);
            IF NOT EXISTS (SELECT 1 FROM public.users
                           WHERE id = p_actor AND role IN ('moderator', 'admin')) THEN
                RAISE EXCEPTION 'Forbidden' USING ERRCODE = 'P0403';
            END IF;
            IF p_status IS NULL OR p_status NOT IN ('visible', 'hidden')
               OR p_reason IS NULL
               OR length(btrim(p_reason)) NOT BETWEEN 1 AND 1000 THEN
                RAISE EXCEPTION 'Invalid moderation action' USING ERRCODE = '22023';
            END IF;
            SELECT moderation_status INTO previous FROM public.videos
                WHERE id = p_video AND deleted_at IS NULL FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Video not found' USING ERRCODE = 'P0404';
            END IF;
            IF previous = p_status THEN RETURN false; END IF;
            UPDATE public.videos SET moderation_status = p_status, updated_at = now()
                WHERE id = p_video;
            INSERT INTO public.audit_events
                (id, actor_id, action, video_id, reason, previous_value, new_value)
            VALUES (p_audit, p_actor,
                    CASE WHEN p_status = 'hidden'
                         THEN 'video.hidden' ELSE 'video.restored' END,
                    p_video, btrim(p_reason), previous, p_status);
            RETURN true;
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.foxtube_resolve_report(
            p_actor uuid, p_report uuid, p_resolution text, p_audit uuid
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE target_video uuid; previous text;
        BEGIN
            PERFORM pg_advisory_xact_lock(742920450412);
            IF NOT EXISTS (SELECT 1 FROM public.users
                           WHERE id = p_actor AND role IN ('moderator', 'admin')) THEN
                RAISE EXCEPTION 'Forbidden' USING ERRCODE = 'P0403';
            END IF;
            IF p_resolution IS NULL
               OR length(btrim(p_resolution)) NOT BETWEEN 1 AND 1000 THEN
                RAISE EXCEPTION 'Invalid resolution' USING ERRCODE = '22023';
            END IF;
            SELECT video_id, status INTO target_video, previous FROM public.reports
                WHERE id = p_report FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Report not found' USING ERRCODE = 'P0404';
            END IF;
            IF previous <> 'open' THEN
                RAISE EXCEPTION 'Report already resolved' USING ERRCODE = 'P0409';
            END IF;
            UPDATE public.reports
                SET status = 'resolved', resolution = btrim(p_resolution),
                resolved_by = p_actor, resolved_at = now() WHERE id = p_report;
            INSERT INTO public.audit_events
                (id, actor_id, action, video_id, report_id, reason,
                 previous_value, new_value)
            VALUES (p_audit, p_actor, 'report.resolved', target_video, p_report,
                    btrim(p_resolution), 'open', 'resolved');
        END $$;
    """)
    op.execute("""
        CREATE FUNCTION public.foxtube_set_role(
            p_actor uuid, p_target uuid, p_role text, p_reason text, p_audit uuid
        ) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE previous text;
        BEGIN
            PERFORM pg_advisory_xact_lock(742920450412);
            IF NOT EXISTS (SELECT 1 FROM public.users
                           WHERE id = p_actor AND role = 'admin') THEN
                RAISE EXCEPTION 'Forbidden' USING ERRCODE = 'P0403';
            END IF;
            IF p_role IS NULL OR p_role NOT IN ('user', 'moderator', 'admin')
               OR p_reason IS NULL
               OR length(btrim(p_reason)) NOT BETWEEN 1 AND 1000 THEN
                RAISE EXCEPTION 'Invalid role action' USING ERRCODE = '22023';
            END IF;
            SELECT role INTO previous FROM public.users WHERE id = p_target FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'User not found' USING ERRCODE = 'P0404';
            END IF;
            IF previous = p_role THEN RETURN false; END IF;
            IF previous = 'admin' AND p_role <> 'admin'
               AND (SELECT count(*) FROM public.users WHERE role = 'admin') <= 1 THEN
                RAISE EXCEPTION 'Last administrator' USING ERRCODE = 'P0409';
            END IF;
            UPDATE public.users SET role = p_role WHERE id = p_target;
            INSERT INTO public.audit_events
                (id, actor_id, action, target_user_id, reason,
                 previous_value, new_value)
            VALUES (p_audit, p_actor, 'role.changed', p_target,
                    btrim(p_reason), previous, p_role);
            RETURN true;
        END $$;
    """)
    for signature in (
        "foxtube_report_video(uuid, uuid, text, uuid, uuid)",
        "foxtube_moderate_video(uuid, uuid, text, text, uuid)",
        "foxtube_resolve_report(uuid, uuid, text, uuid)",
        "foxtube_set_role(uuid, uuid, text, text, uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{signature} TO foxtube_api")


def downgrade() -> None:
    for signature in (
        "foxtube_report_video(uuid, uuid, text, uuid, uuid)",
        "foxtube_moderate_video(uuid, uuid, text, text, uuid)",
        "foxtube_resolve_report(uuid, uuid, text, uuid)",
        "foxtube_set_role(uuid, uuid, text, text, uuid)",
    ):
        op.execute(f"DROP FUNCTION public.{signature}")
    op.drop_table("audit_events")
    op.drop_table("reports")
