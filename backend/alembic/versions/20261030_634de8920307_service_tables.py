"""서비스 테이블: 프로젝트·버전·산출물·작업

Revision ID: 634de8920307
Revises: 
Create Date: 2026-10-30 12:48:13.989090
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '634de8920307'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('jobs',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('project', sa.String(length=200), nullable=False),
    sa.Column('base', sa.String(length=50), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('log', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('new_version', sa.String(length=50), nullable=True),
    sa.Column('warnings', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('started_at', sa.String(length=40), nullable=False),
    sa.Column('finished_at', sa.String(length=40), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_jobs_project'), 'jobs', ['project'], unique=False)
    op.create_index(op.f('ix_jobs_status'), 'jobs', ['status'], unique=False)
    op.create_table('project_files',
    sa.Column('project_id', sa.String(length=200), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('project_id', 'name')
    )
    op.create_table('projects',
    sa.Column('id', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('design_versions',
    sa.Column('project_id', sa.String(length=200), nullable=False),
    sa.Column('label', sa.String(length=50), nullable=False),
    sa.Column('record', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('project_id', 'label')
    )
    op.create_table('artifacts',
    sa.Column('project_id', sa.String(length=200), nullable=False),
    sa.Column('version_label', sa.String(length=50), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['project_id', 'version_label'], ['design_versions.project_id', 'design_versions.label'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('project_id', 'version_label', 'name')
    )


def downgrade() -> None:
    op.drop_table('artifacts')
    op.drop_table('design_versions')
    op.drop_table('projects')
    op.drop_table('project_files')
    op.drop_index(op.f('ix_jobs_status'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_project'), table_name='jobs')
    op.drop_table('jobs')
