"""audit_action disqualify/reinstate enum values

Revision ID: a8e2c6f19b3d
Revises: c1a9f7b3e4d2

Same gap as `4bef2bf4c0a8`: `SUBMISSION_DISQUALIFIED`/`SUBMISSION_REINSTATED`
were added to the Python `AuditAction` enum for the new
`/submissions/{id}/disqualify` and `/reinstate` routes, and
`--autogenerate` does not detect new values on an existing native `ENUM`
column. Hand-written, same as every other enum-value addition since.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a8e2c6f19b3d'
down_revision: Union[str, Sequence[str], None] = 'c1a9f7b3e4d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_VALUES = ("submission_disqualified", "submission_reinstated")


def upgrade() -> None:
    for value in _NEW_VALUES:
        op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres has no `DROP VALUE` for an enum type -- see 4bef2bf4c0a8.
    pass
