"""Replace the legacy Provider type with concrete wire protocols.

Revision ID: 0003_model_provider_protocols
Revises: 0002_user_model_provider
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_model_provider_protocols"
down_revision: str | None = "0002_user_model_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESPONSES_MODELS = (
    "gpt-5.6",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-v4-flash-vision-exp",
)


def upgrade() -> None:
    providers = sa.table(
        "user_model_providers",
        sa.column("provider_type", sa.String()),
        sa.column("model_name", sa.String()),
    )
    op.execute(
        providers.update()
        .where(providers.c.provider_type == "openai_compatible")
        .values(
            provider_type=sa.case(
                (
                    sa.func.lower(providers.c.model_name).in_(_RESPONSES_MODELS),
                    "responses",
                ),
                else_="chat_completions",
            )
        )
    )


def downgrade() -> None:
    providers = sa.table(
        "user_model_providers",
        sa.column("provider_type", sa.String()),
    )
    op.execute(
        providers.update()
        .where(
            providers.c.provider_type.in_(
                ("responses", "chat_completions", "anthropic")
            )
        )
        .values(provider_type="openai_compatible")
    )
