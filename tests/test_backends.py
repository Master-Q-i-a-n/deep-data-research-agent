from types import SimpleNamespace

import pytest
from blockbuster import blockbuster_ctx
from deepagents.backends import FilesystemBackend

from deep_data_research_agent import backends


def _runtime(thread_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        config={"configurable": {"thread_id": thread_id}},
        state={},
    )


def test_workspace_root_is_scoped_and_sanitized(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(backends, "ARTIFACT_ROOT", tmp_path.resolve())

    assert backends.workspace_root(_runtime("user/thread:1")) == (
        tmp_path.resolve() / "user_thread_1" / "workspace"
    )


def test_workspace_files_are_isolated_by_thread(tmp_path, monkeypatch) -> None:
    # FilesystemBackend 在测试进入异步路径前初始化，模拟生产模块加载阶段。
    filesystem = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    monkeypatch.setattr(backends, "_WORKSPACE_FILESYSTEM", filesystem)

    first = backends.create_backend(_runtime("thread-a"))
    second = backends.create_backend(_runtime("thread-b"))

    assert first.write("/workspace/report.md", "A").error is None
    assert second.write("/workspace/report.md", "B").error is None
    assert (tmp_path / "thread-a" / "workspace" / "report.md").read_text(encoding="utf-8") == "A"
    assert (tmp_path / "thread-b" / "workspace" / "report.md").read_text(encoding="utf-8") == "B"


@pytest.mark.asyncio
async def test_backend_factory_and_async_skill_read_do_not_block_event_loop() -> None:
    # 与 `langgraph dev` 相同，BlockBuster 会拦截事件循环中的同步文件调用。
    with blockbuster_ctx(scanned_modules=["deep_data_research_agent"]):
        backend = backends.create_backend(_runtime("thread-async"))
        result = await backend.als("/skills/worker/")

    assert result.error is None
    assert result.entries
    assert result.entries[0]["path"].endswith("/tavily-crawling/")


def test_backend_exposes_worker_and_supervisor_skills() -> None:
    backend = backends.create_backend(_runtime("thread-1"))

    assert backend.ls("/skills/worker/").entries[0]["path"].endswith(
        "/tavily-crawling/"
    )
    assert backend.ls("/skills/supervisor/").entries[0]["path"].endswith(
        "/evidence-reporting/"
    )
