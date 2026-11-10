"""Alembic běží nad enginem aplikace. Connection string se neloguje a není v alembic.ini."""
from alembic import context

from lovceny.db import Base, engine
from lovceny import models  # noqa: F401

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url="sqlite://", target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
