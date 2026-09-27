"""audit_action result override enum values

Revision ID: a3f0e6b19d27
Revises: 8c17b848ef2e

Same gap as every other enum-value addition in this project (see
`4bef2bf4c0a8`): `AuditAction.RESULT_OVERRIDDEN`/`RESULT_OVERRIDE_CLEARED`
were added to the Python enum for Part 4's admin override feature, and
`--autogenerate` does not detect a new value on an existing native `ENUM`
column.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a3f0e6b19d27'
down_revision: Union[str, Sequence[str], None] = '8c17b848ef2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'result_overridden'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'result_override_cleared'")


def downgrade() -> None:
    pass
