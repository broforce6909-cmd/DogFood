"""submission disqualified check constraint

Revision ID: c1a9f7b3e4d2
Revises: 74f44fef0002

`--autogenerate` does not detect a change to an existing `CheckConstraint`'s
condition text (only additions/removals of a constraint by that name), so
`ck_submissions_submitted_at_matches_status` is dropped and recreated here by
hand, to allow a disqualified row to keep whatever `submitted_at` it already
had rather than forcing it to null or non-null.

Kept as its own migration, after `74f44fef0002`, because the 'disqualified'
enum value that constraint`s new clause references must already be committed
-- see that migration's docstring.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c1a9f7b3e4d2'
down_revision: Union[str, Sequence[str], None] = '74f44fef0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_CONDITION = (
    "(status = 'draft' AND submitted_at IS NULL)"
    " OR (status = 'submitted' AND submitted_at IS NOT NULL)"
)
_NEW_CONDITION = f"{_OLD_CONDITION} OR (status = 'disqualified')"


def upgrade() -> None:
    op.drop_constraint(
        'ck_submissions_submitted_at_matches_status', 'submissions', type_='check'
    )
    op.create_check_constraint(
        'ck_submissions_submitted_at_matches_status', 'submissions', _NEW_CONDITION
    )


def downgrade() -> None:
    op.drop_constraint(
        'ck_submissions_submitted_at_matches_status', 'submissions', type_='check'
    )
    op.create_check_constraint(
        'ck_submissions_submitted_at_matches_status', 'submissions', _OLD_CONDITION
    )
