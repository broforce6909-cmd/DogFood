"""backfill missing audit_action enum values

Revision ID: 4bef2bf4c0a8
Revises: 9070e8444b3a

Three `AuditAction` members were added to the Python enum across recent
migrations (`third_review_assigned`, `judge_recused` in b5eb765b806c;
`event_registered` in this same batch) but the Postgres-native `audit_action`
enum type was never actually altered to accept them -- `alembic revision
--autogenerate` does not detect enum-value additions to an existing
`ENUM` column the way it detects a new table or column, so this went
unnoticed until a real `record(action=AuditAction.EVENT_REGISTERED, ...)`
call hit a live (migrated-by-alembic, not `create_all`-built) database and
Postgres rejected the value outright. The test suite never caught this
because it builds its schema with `Base.metadata.create_all()`, which always
reflects the *current* Python enum, not what any migration actually did to a
real database -- see ARCHITECTURE.md's "Migrations" for why the test suite
uses `create_all` at all and why that is normally safe.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '4bef2bf4c0a8'
down_revision: Union[str, Sequence[str], None] = '9070e8444b3a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_VALUES = ("third_review_assigned", "judge_recused", "event_registered")


def upgrade() -> None:
    # `ADD VALUE IF NOT EXISTS` (Postgres 12+): idempotent, and safe inside
    # alembic's transactional DDL as long as the new value is never *used* in
    # the same transaction, which this migration does not do.
    for value in _NEW_VALUES:
        op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres has no `DROP VALUE` for an enum type -- removing one means
    # recreating the type and repointing every column that uses it. Not
    # attempted here; a downgrade that needs this back out is rare enough
    # (and destructive enough) to be a manual operation, not an automated one.
    pass
