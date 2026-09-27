"""Alembic environment: wired to the app's own settings and ORM metadata.

Two things this deviates from the generated template for, deliberately:

1. **The database URL comes from `app.config.settings`, not `alembic.ini`.**
   The app already has exactly one source of truth for `DATABASE_URL` -- the
   environment, read once in `app/config.py` -- and `alembic.ini` having its own
   copy would be a second place for the two to drift apart. `alembic.ini`'s own
   `sqlalchemy.url` is left blank; nothing reads it.
2. **`target_metadata` is `app.db.Base.metadata`, after importing `app.models`.**
   Importing the module is what registers every table on `Base.metadata` --
   without it, `target_metadata` would be empty and `alembic revision
   --autogenerate` would (silently, which is worse) propose dropping every table
   that exists.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Importing registers every model's table on Base.metadata; the import itself
# is the side effect this file needs, `models` is never referenced by name.
from app import models  # noqa: F401
from app.config import settings
from app.db import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live database connection.

    Kept from the generated template -- useful for reviewing what a migration
    would do (`alembic upgrade head --sql`) without touching anything.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Catches a column's type changing (e.g. widening `signature` from
            # 64 to 128 chars, which Phase 5 actually did) in autogenerate diffs
            # that would otherwise only compare column *presence*, not shape.
            compare_type=True,
            # One commit per migration script, not one commit for the whole
            # `upgrade head` run. Without this, a `ALTER TYPE ... ADD VALUE`
            # migration followed immediately (in the same invocation) by a
            # migration that uses the new value fails outright -- Postgres
            # refuses to use an enum value added earlier in the same still-open
            # transaction, which is exactly what a from-scratch deploy running
            # `alembic upgrade head` once would hit. See ARCHITECTURE.md's
            # "Migrations" section.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
