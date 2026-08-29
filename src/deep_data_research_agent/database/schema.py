"""Application schema revision constants shared by runtime and deployment tools."""

ALEMBIC_BASELINE_REVISION = "0001_current_schema"
ALEMBIC_HEAD_REVISION = "0002_user_model_provider"

CHECKPOINT_TABLES = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)

__all__ = [
    "ALEMBIC_BASELINE_REVISION",
    "ALEMBIC_HEAD_REVISION",
    "CHECKPOINT_TABLES",
]
