from types import SimpleNamespace

import pytest

from deep_data_research_agent import interaction_tools, sandbox_manager


def _runtime():
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-a"),
        server_info=SimpleNamespace(user=SimpleNamespace(identity="user-a")),
        tool_call_id="call-download",
    )


class FakeBackend:
    def __init__(self, content: bytes | None = b"report") -> None:
        self.content = content
        self.download_calls: list[list[str]] = []

    async def adownload_files(self, paths):
        self.download_calls.append(list(paths))
        return [
            SimpleNamespace(
                path=paths[0],
                content=self.content,
                error=None if self.content is not None else "文件不存在",
            )
        ]


def test_ask_user_has_defensive_result() -> None:
    result = interaction_tools.ask_user.func(
        question="采购数量是多少？",
        missing_fields=["quantity"],
        known_information="型号已确认",
    )

    assert "不能继续假设" in result


@pytest.mark.asyncio
async def test_request_report_download_exports_after_approval(monkeypatch) -> None:
    backend = FakeBackend(b"# report")
    export_calls: list[tuple[str, str]] = []

    async def ensure(*args, **kwargs):
        assert args == ("thread-a",)
        assert kwargs["user_id"] == "user-a"
        return backend

    async def export(thread_id: str, *, component: str):
        export_calls.append((thread_id, component))
        return [{"path": "/workspace/final_report.md", "size": 8}]

    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "ensure", ensure)
    monkeypatch.setattr(sandbox_manager.SANDBOX_MANAGER, "export_workspace", export)

    result = await interaction_tools.request_report_download.coroutine(
        file_path="/workspace/final_report.md",
        download_name="采购报告.md",
        runtime=_runtime(),
    )

    assert result.status == "success"
    assert result.artifact == {
        "type": "file_download",
        "path": "/workspace/final_report.md",
        "filename": "采购报告.md",
        "size": 8,
    }
    assert backend.download_calls == [["/workspace/final_report.md"]]
    assert export_calls == [("thread-a", "supervisor")]


@pytest.mark.asyncio
async def test_request_report_download_rejects_path_outside_workspace(monkeypatch) -> None:
    async def unexpected_ensure(*_args, **_kwargs):
        raise AssertionError("非法路径不应访问沙箱")

    monkeypatch.setattr(
        sandbox_manager.SANDBOX_MANAGER,
        "ensure",
        unexpected_ensure,
    )

    result = await interaction_tools.request_report_download.coroutine(
        file_path="/etc/passwd",
        download_name=None,
        runtime=_runtime(),
    )

    assert result.status == "error"
    assert "必须位于 /workspace" in str(result.content)
