"""audit_action calibration enum values

Revision ID: f8b3c6d0e257
Revises: e7a2b5c9d146

Same gap as every other enum-value addition in this project (see
`b4c1d8e7f302`): `--autogenerate` does not detect a new value on an existing
native `ENUM`, and it lives in its own revision so it commits before anything
can use it (`transaction_per_migration=True` in `alembic/env.py`).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f8b3c6d0e257'
down_revision: Union[str, Sequence[str], None] = 'e7a2b5c9d146'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'calibration_project_added'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'calibration_project_removed'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'calibration_scored'")


def downgrade() -> None:
    pass
