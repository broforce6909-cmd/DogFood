"""audit_action archival enum values

Revision ID: c2e6f9b3d58a
Revises: b1d5e8a2c479

Same gap as every other enum-value addition in this project (see
`b4c1d8e7f302`): `--autogenerate` does not detect a new value on an existing
native `ENUM`, and it lives in its own revision so it commits before anything
can use it.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c2e6f9b3d58a'
down_revision: Union[str, Sequence[str], None] = 'b1d5e8a2c479'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'event_archived'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'event_unarchived'")


def downgrade() -> None:
    pass
