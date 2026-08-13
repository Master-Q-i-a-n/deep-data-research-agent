import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from deep_data_research_agent import database


@pytest_asyncio.fixture
async def isolated_database(monkeypatch):
    """Use SQLite to verify delivery idempotency without external PostgreSQL."""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_session_factory", factory)
    monkeypatch.setattr(database, "_initialized", False)
    await database.ensure_schema()
    try:
        yield
    finally:
        await database.close_database()


@pytest.mark.asyncio
async def test_email_delivery_insert_is_idempotent(isolated_database) -> None:
    values = {
        "idempotency_key": "a" * 64,
        "thread_id": "thread-a",
        "user_id": database.DEFAULT_USER_ID,
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
    assert first.status == second.status == "sending"
    assert second.message_id == "<message@example.com>"


@pytest.mark.asyncio
async def test_email_delivery_terminal_state_is_persisted(isolated_database) -> None:
    key = "b" * 64
    await database.begin_email_delivery(
        idempotency_key=key,
        thread_id="thread-a",
        user_id=database.DEFAULT_USER_ID,
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
        user_id=database.DEFAULT_USER_ID,
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
