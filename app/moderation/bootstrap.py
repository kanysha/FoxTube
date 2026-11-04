"""Offline, privileged, one-time first-administrator setup; never an HTTP route."""

import argparse
import sys
from uuid import UUID, uuid4

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.db import make_engine
from app.moderation.service import ROLE_LOCK


class BootstrapError(Exception):
    pass


def bootstrap_admin(engine: Engine, email: str) -> UUID:
    """Promote one existing exact account, only if no administrator exists."""
    with engine.begin() as connection:
        privileged = connection.scalar(
            text("""
            SELECT current_user NOT IN ('foxtube_api', 'foxtube_worker')
                AND has_column_privilege(current_user, 'public.users', 'role', 'UPDATE')
                AND has_table_privilege(current_user, 'public.audit_events', 'INSERT')
        """)
        )
        if not privileged:
            raise BootstrapError(
                "Нужна учётная запись БД для миграций или администрирования."
            )
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": ROLE_LOCK}
        )
        if connection.scalar(
            text("SELECT count(*) FROM public.users WHERE role = 'admin'")
        ):
            raise BootstrapError(
                "Администратор уже существует. Используйте управление ролями."
            )
        user_id = connection.scalar(
            text("SELECT id FROM public.users WHERE email = :email FOR UPDATE"),
            {"email": email},
        )
        if user_id is None:
            raise BootstrapError(
                "Пользователь с этим точным email не найден. Сначала зарегистрируйтесь."
            )
        previous = connection.scalar(
            text("SELECT role FROM public.users WHERE id = :id"), {"id": user_id}
        )
        connection.execute(
            text("UPDATE public.users SET role = 'admin' WHERE id = :id"),
            {"id": user_id},
        )
        connection.execute(
            text("""
                INSERT INTO public.audit_events
                    (id, actor_id, action, target_user_id, reason,
                     previous_value, new_value)
                VALUES (:audit, NULL, 'role.bootstrap', :target,
                        'Первичная настройка администратора через CLI.',
                        :previous, 'admin')
            """),
            {"audit": uuid4(), "target": user_id, "previous": previous},
        )
        return UUID(str(user_id))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Назначить первого администратора существующему аккаунту."
    )
    parser.add_argument(
        "--email", required=True, help="Точный email зарегистрированного аккаунта"
    )
    arguments = parser.parse_args()
    try:
        email = str(
            TypeAdapter(EmailStr).validate_python(arguments.email.strip().casefold())
        )
    except ValidationError:
        print("Некорректный email.", file=sys.stderr)
        return 2
    engine = make_engine(get_settings())
    try:
        user_id = bootstrap_admin(engine, email)
    except BootstrapError as error:
        print(str(error), file=sys.stderr)
        return 2
    except SQLAlchemyError:
        print(
            "Не удалось настроить БД. Проверьте миграции и привилегии подключения.",
            file=sys.stderr,
        )
        return 2
    finally:
        engine.dispose()
    print(f"Первый администратор назначен: {user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
