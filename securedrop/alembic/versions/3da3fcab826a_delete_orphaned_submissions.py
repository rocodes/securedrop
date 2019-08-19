"""delete orphaned submissions and replies

Ref: https://github.com/freedomofpress/securedrop/issues/1189

Revision ID: 3da3fcab826a
Revises: a9fe328b053a
Create Date: 2018-11-25 19:40:25.873292

"""
import os
from alembic import op
import sqlalchemy as sa
from journalist_app import create_app
from rm import srm
from store import NoFileFoundException, TooManyFilesException
from worker import rq_worker_queue

# raise the errors if we're not in production
raise_errors = os.environ.get("SECUREDROP_ENV", "prod") != "prod"

try:
    from sdconfig import config
except ImportError:
    # This is a fresh install, and config.py has not been created yet.
    if raise_errors:
        raise

# revision identifiers, used by Alembic.
revision = '3da3fcab826a'
down_revision = 'a9fe328b053a'
branch_labels = None
depends_on = None


def raw_sql_grab_orphaned_objects(table_name):
    """Objects that have a source ID that doesn't exist in the
    sources table OR a NULL source ID should be deleted."""
    return ('SELECT id, filename, source_id FROM {table} '  # nosec
            'WHERE source_id NOT IN (SELECT id FROM sources) '
            'UNION SELECT id, filename, source_id FROM {table} '  # nosec
            'WHERE source_id IS NULL').format(table=table_name)


def upgrade():
    conn = op.get_bind()
    submissions = conn.execute(
        sa.text(raw_sql_grab_orphaned_objects('submissions'))
    ).fetchall()

    replies = conn.execute(
        sa.text(raw_sql_grab_orphaned_objects('replies'))
    ).fetchall()

    try:
        app = create_app(config)
        with app.app_context():
            for submission in submissions:
                try:
                    conn.execute(
                    sa.text("""
                        DELETE FROM submissions
                        WHERE id=:id
                    """).bindparams(id=submission.id)
                    )

                    file_path = app.storage.path_without_filesystem_id(submission.filename)
                    rq_worker_queue.enqueue(srm, file_path)
                except NoFileFoundException:
                    # The file must have been deleted by the admin, remove the row
                    conn.execute(
                    sa.text("""
                        DELETE FROM submissions
                        WHERE id=:id
                    """).bindparams(id=submission.id)
                    )
                except TooManyFilesException:
                    pass

            for reply in replies:
                try:
                    conn.execute(
                        sa.text("""
                            DELETE FROM replies
                            WHERE id=:id
                        """).bindparams(id=reply.id)
                    )

                    file_path = app.storage.path_without_filesystem_id(reply.filename)
                    rq_worker_queue.enqueue(srm, file_path)
                except NoFileFoundException:
                    # The file must have been deleted by the admin, remove the row
                    conn.execute(
                        sa.text("""
                            DELETE FROM replies
                            WHERE id=:id
                        """).bindparams(id=reply.id)
                    )
                except TooManyFilesException:
                    pass
    except:  # noqa
        if raise_errors:
            raise


def downgrade():
    # This is a destructive alembic migration, it cannot be downgraded
    pass
