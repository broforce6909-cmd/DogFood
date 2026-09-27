"""judge calibration: practice projects, expected scores, judge scores

Revision ID: e7a2b5c9d146
Revises: d6e1f4a2b835

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7a2b5c9d146'
down_revision: Union[str, Sequence[str], None] = 'd6e1f4a2b835'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'calibration_projects',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('event_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=160), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('event_id', 'name', name='uq_calibration_projects_event_name'),
        sa.UniqueConstraint('id', 'event_id', name='uq_calibration_projects_id_event'),
    )
    op.create_table(
        'calibration_expected',
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('criterion_id', sa.Uuid(), nullable=False),
        sa.Column('event_id', sa.Uuid(), nullable=False),
        sa.Column('value', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.ForeignKeyConstraint(['project_id', 'event_id'], ['calibration_projects.id', 'calibration_projects.event_id'], name='fk_calibration_expected_project_same_event', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['criterion_id', 'event_id'], ['rubric_criteria.id', 'rubric_criteria.event_id'], name='fk_calibration_expected_criterion_same_event', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('project_id', 'criterion_id'),
    )
    op.create_table(
        'calibration_scores',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('event_id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('judge_id', sa.Uuid(), nullable=False),
        sa.Column('criterion_id', sa.Uuid(), nullable=False),
        sa.Column('value', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['project_id', 'event_id'], ['calibration_projects.id', 'calibration_projects.event_id'], name='fk_calibration_scores_project_same_event', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['judge_id', 'event_id'], ['judges.id', 'judges.event_id'], name='fk_calibration_scores_judge_same_event', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['criterion_id', 'event_id'], ['rubric_criteria.id', 'rubric_criteria.event_id'], name='fk_calibration_scores_criterion_same_event', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'judge_id', 'criterion_id', name='uq_calibration_scores_one_each'),
    )
    op.create_index('ix_calibration_scores_event', 'calibration_scores', ['event_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_calibration_scores_event', table_name='calibration_scores')
    op.drop_table('calibration_scores')
    op.drop_table('calibration_expected')
    op.drop_table('calibration_projects')
