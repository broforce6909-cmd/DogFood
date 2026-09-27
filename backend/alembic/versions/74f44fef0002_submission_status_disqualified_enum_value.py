"""submission status disqualified enum value

Revision ID: 74f44fef0002
Revises: 84c3bf9d47e2

Adds `SubmissionStatus.DISQUALIFIED`. As with `4bef2bf4c0a8`, `--autogenerate`
does not detect a new value on an existing native `ENUM` column, so the type
alteration here is hand-written.

This is split into its own migration, separate from the CHECK constraint
update that follows it (`c1a9f7b3e4d2`), because Postgres refuses to use a
freshly added enum value inside the same transaction that added it --
`ALTER TYPE ... ADD VALUE` must commit first. `env.py` now runs each
migration script in its own transaction (`transaction_per_migration=True`,
added alongside this pair) specifically so a single `alembic upgrade head`
-- including a from-scratch deploy that is behind by both revisions at
once -- applies them correctly. See ARCHITECTURE.md's "Migrations" section.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '74f44fef0002'
down_revision: Union[str, Sequence[str], None] = '84c3bf9d47e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE submission_status ADD VALUE IF NOT EXISTS 'disqualified'")


def downgrade() -> None:
    # Postgres has no `DROP VALUE` for an enum type -- see 4bef2bf4c0a8 for
    # why removing 'disqualified' from `submission_status` is not attempted
    # here.
    pass
