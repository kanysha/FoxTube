from logging.config import fileConfig

from alembic import context

from app.auth import models as auth_models  # noqa: F401
from app.core.config import get_settings
from app.core.db import Base, make_engine
from app.core.models import ServiceHeartbeat  # noqa: F401
from app.social import models as social_models  # noqa: F401
from app.videos import models as video_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(get_settings(), pool_size=1)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
