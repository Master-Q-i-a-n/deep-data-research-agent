from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from deep_data_research_agent.database import repository as database
from deep_data_research_agent.database.models import Base


async def _noop_schema_check(**_kwargs) -> None:
    return None


@pytest_asyncio.fixture
async def isolated_database(monkeypatch):
    """Use SQLite to verify delivery idempotency without external PostgreSQL."""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_session_factory", factory)
    monkeypatch.setattr(database, "_initialized", False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(database, "_validate_deployed_schema", _noop_schema_check)
    await database.ensure_schema()
    user = await database.create_user("delivery-user", "hash")
    try:
        yield user.id
    finally:
        await database.close_database()


@pytest.mark.asyncio
async def test_email_delivery_insert_is_idempotent(isolated_database) -> None:
    values = {
        "idempotency_key": "a" * 64,
        "thread_id": "thread-a",
        "user_id": isolated_database,
        "recipient": "reader@example.com",
        "subject": "研究报告",
        "pdf_filename": "final_report.pdf",
        "zip_filename": "final_report-bundle.zip",
        "message_id": "<message@example.com>",
    }

    first, first_created = await database.begin_email_delivery(**values)
    second, second_created = await database.begin_email_delivery(**values)

    assert first_created is True
    assert second_created is False
    assert first.status == second.status == "queued"
    assert second.message_id == "<message@example.com>"


@pytest.mark.asyncio
async def test_email_delivery_terminal_state_is_persisted(isolated_database) -> None:
    key = "b" * 64
    await database.begin_email_delivery(
        idempotency_key=key,
        thread_id="thread-a",
        user_id=isolated_database,
        recipient="reader@example.com",
        subject="研究报告",
        pdf_filename="final_report.pdf",
        zip_filename="final_report-bundle.zip",
        message_id="<message@example.com>",
    )

    updated = await database.finish_email_delivery(
        key,
        status="uncertain",
        error_summary="投递状态不确定",
    )
    replay, created = await database.begin_email_delivery(
        idempotency_key=key,
        thread_id="thread-a",
        user_id=isolated_database,
        recipient="ignored@example.com",
        subject="ignored",
        pdf_filename="ignored.pdf",
        zip_filename="ignored.zip",
        message_id="<ignored@example.com>",
    )

    assert updated.status == "uncertain"
    assert replay.status == "uncertain"
    assert replay.recipient == "reader@example.com"
    assert created is False


@pytest.mark.asyncio
async def test_email_delivery_rejects_unknown_status(isolated_database) -> None:
    with pytest.raises(ValueError, match="状态无效"):
        await database.finish_email_delivery("missing", status="retrying")


@pytest.mark.asyncio
async def test_email_delivery_claim_is_atomic_and_replay_safe(isolated_database) -> None:
    key = "c" * 64
    await database.begin_email_delivery(
        idempotency_key=key,
        thread_id="thread-a",
        user_id=isolated_database,
        recipient="reader@example.com",
        subject="研究报告",
        pdf_filename="final_report.pdf",
        zip_filename="final_report-bundle.zip",
        message_id="<message@example.com>",
        pdf_path="/workspace/output/final_report.pdf",
        markdown_path="/workspace/output/final_report.md",
    )

    claimed = await database.claim_email_delivery(key)
    duplicate = await database.claim_email_delivery(key)

    assert claimed is not None
    assert claimed.status == "processing"
    assert claimed.attempts == 1
    assert duplicate is None


@pytest.mark.asyncio
async def test_recovery_never_resends_stale_submitting_delivery(isolated_database) -> None:
    key = "d" * 64
    await database.begin_email_delivery(
        idempotency_key=key,
        thread_id="thread-a",
        user_id=isolated_database,
        recipient="reader@example.com",
        subject="研究报告",
        pdf_filename="final_report.pdf",
        zip_filename="final_report-bundle.zip",
        message_id="<message@example.com>",
        pdf_path="/workspace/output/final_report.pdf",
        markdown_path="/workspace/output/final_report.md",
    )
    await database.claim_email_delivery(key)
    await database.mark_email_submitting(key)
    async with database.session_factory()() as session, session.begin():
        delivery = (
            await session.execute(
                select(database.EmailDelivery).where(
                    database.EmailDelivery.idempotency_key == key
                )
            )
        ).scalar_one()
        delivery.lease_until = database._utcnow() - timedelta(seconds=1)

    publish_ids = await database.recover_email_deliveries()
    recovered = await database.get_email_delivery(key)

    assert key not in publish_ids
    assert recovered is not None
    assert recovered.status == "uncertain"
