"""scores.value becomes continuous (Numeric(5,2)) instead of a whole number

Revision ID: d6e1f4a2b835
Revises: c5d2e9f1a614
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "d6e1f4a2b835"
down_revision = "c5d2e9f1a614"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Widening, not narrowing: every existing integer score (0-100) converts
    losslessly to the same value as a Numeric(5,2), so already-seeded discrete
    scores stay exactly as they were -- they simply become one point among many
    on the same 0.1-step scale, not a special case."""
    op.execute("ALTER TABLE scores ALTER COLUMN value TYPE NUMERIC(5, 2) USING value::numeric(5, 2)")


def downgrade() -> None:
    """Rounds to the nearest whole number -- lossy by construction, since a
    continuous scale has no canonical way back to discrete steps."""
    op.execute("ALTER TABLE scores ALTER COLUMN value TYPE INTEGER USING round(value)::integer")
