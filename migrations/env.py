from alembic import context

from workforce import models  # noqa: F401  (registers tables)
from workforce.db import Base, engine

target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=str(engine().url), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    with engine().connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
