"""assign_skill 与 _read_skill_files 的路径映射回归测试。

覆盖根因：VFS /skills/main/{name} 经 CompositeBackend 路由映射到沙箱物理 /{name}，
assign_skill 必须按物理路径读取与清理；aglob 返回相对路径、adownload_files 需要
绝对物理路径；aglob 空结果时回退到单个 JSON 数组输出。
"""

from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from deep_data_research_agent import identity, sandbox_manager, skill_tools


def _runtime(store=None):
    return SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-a"),
        store=store,
        server_info=SimpleNamespace(
            user=SimpleNamespace(identity="test-user"),
        ),
    )


def _glob_result(matches=None, error=None):
    return SimpleNamespace(error=error, matches=matches or [])


def _download(path, content, error=None):
    return SimpleNamespace(path=path, content=content, error=error)


def _execute(output, exit_code=0):
    return SimpleNamespace(output=output, exit_code=exit_code)


class FakeBackend:
    """最小沙箱后端：记录调用，按预设返回 glob / download / execute 结果。"""

    def __init__(self):
        self.glob_calls: list[tuple[str, str | None]] = []
        self.download_calls: list[list[str]] = []
        self.execute_calls: list[tuple[str, int | None]] = []
        self.glob_matches: list[dict] = []
        self.glob_error: str | None = None
        self.file_contents: dict[str, bytes] = {}
        self.find_output = ""
        self.find_exit_code = 0

    async def aglob(self, pattern, path=None):
        self.glob_calls.append((pattern, path))
        return _glob_result(self.glob_matches, self.glob_error)

    async def adownload_files(self, paths):
        self.download_calls.append(list(paths))
        return [
            _download(path, self.file_contents.get(path, b""))
            for path in paths
        ]

    async def aexecute(self, command, timeout=None):
        self.execute_calls.append((command, timeout))
        return _execute(self.find_output, self.find_exit_code)


SKILL_MD = "---\nname: demo\ndescription: 演示 Skill\n---\n".encode()


def _filled_backend(glob_matches, contents):
    backend = FakeBackend()
    backend.glob_matches = glob_matches
    backend.file_contents = contents
    return backend


@pytest.mark.asyncio
async def test_read_skill_files_resolves_physical_paths() -> None:
    backend = _filled_backend(
        glob_matches=[
            {"path": "SKILL.md", "is_dir": False},
            {"path": "scripts", "is_dir": True},
            {"path": "scripts/run.py", "is_dir": False},
        ],
        contents={
            "/demo/SKILL.md": SKILL_MD,
            "/demo/scripts/run.py": b"print('hi')\n",
        },
    )

    files = await skill_tools._read_skill_files(
        backend, "/demo", "/skills/main/demo"
    )

    assert files == [
        ("SKILL.md", SKILL_MD),
        ("scripts/run.py", b"print('hi')\n"),
    ]
    # aglob 收到的是物理路径，下载用绝对物理路径（相对结果被转成绝对路径）。
    assert backend.glob_calls == [("**/*", "/demo")]
    assert backend.download_calls == [["/demo/SKILL.md", "/demo/scripts/run.py"]]


@pytest.mark.asyncio
async def test_read_skill_files_requires_root_skill_md() -> None:
    backend = _filled_backend(
        glob_matches=[{"path": "run.py", "is_dir": False}],
        contents={"/demo/run.py": b"print('hi')\n"},
    )

    with pytest.raises(RuntimeError, match="缺少根级 SKILL.md"):
        await skill_tools._read_skill_files(
            backend, "/demo", "/skills/main/demo"
        )


@pytest.mark.asyncio
async def test_read_skill_files_falls_back_to_json_on_empty_glob() -> None:
    """JSON 数组不依赖换行，可规避 OpenSandbox 拼接 stdout 日志块。"""
    backend = _filled_backend(glob_matches=[], contents={})
    backend.find_output = '["/demo/SKILL.md", "/demo/run.py"]'
    backend.file_contents = {
        "/demo/SKILL.md": SKILL_MD,
        "/demo/run.py": b"print('hi')\n",
    }

    files = await skill_tools._read_skill_files(
        backend, "/demo", "/skills/main/demo"
    )

    assert files == [("SKILL.md", SKILL_MD), ("run.py", b"print('hi')\n")]
    assert backend.glob_calls == [("**/*", "/demo")]
    assert backend.execute_calls[0][0].startswith("python3 -c ")
    assert "json.dumps" in backend.execute_calls[0][0]
    assert backend.download_calls == [["/demo/SKILL.md", "/demo/run.py"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "message"),
    [
        (1, "Skill 目录不存在：VFS /skills/main/demo/（物理 /demo/）"),
        (0, "Skill 目录 /skills/main/demo/ 不存在或为空（物理 /demo/）"),
    ],
)
async def test_read_skill_files_reports_both_paths(exit_code, message) -> None:
    backend = _filled_backend(glob_matches=[], contents={})
    backend.find_output = ""
    backend.find_exit_code = exit_code

    with pytest.raises(RuntimeError, match=message):
        await skill_tools._read_skill_files(
            backend, "/demo", "/skills/main/demo"
        )


def _assign_backend():
    backend = _filled_backend(
        glob_matches=[{"path": "SKILL.md", "is_dir": False}],
        contents={"/demo-skill/SKILL.md": SKILL_MD},
    )
    return backend


@pytest.mark.asyncio
async def test_assign_skill_reads_and_cleans_physical_dir(monkeypatch) -> None:
    """回归根因：assign_skill 读/清理物理 /{name}，store 写入 /active/。"""
    backend = _assign_backend()
    monkeypatch.setattr(
        sandbox_manager,
        "SANDBOX_MANAGER",
        SimpleNamespace(get_backend=lambda _t, *, component: backend),
    )
    store = InMemoryStore()
    runtime = _runtime(store)

    result = await skill_tools.assign_skill.coroutine(
        skill_name="demo-skill",
        targets=["crawl-worker"],
        runtime=runtime,
    )

    assert '"status": "assigned"' in result
    assert '"file_count": 1' in result
    # 物理路径：不是字面 /skills/main/...
    assert backend.glob_calls == [("**/*", "/demo-skill")]
    assert backend.download_calls == [["/demo-skill/SKILL.md"]]
    assert backend.execute_calls == [("rm -rf /demo-skill", 30)]

    namespace = (identity.user_hash(runtime), "skills", "assigned", "crawl-worker")
    items = store.search(namespace)
    keys = {item.key for item in items}
    assert "/active/demo-skill/SKILL.md" in keys
    assert "/manifests/demo-skill.json" in keys


@pytest.mark.asyncio
async def test_assign_skill_error_reports_both_paths(monkeypatch) -> None:
    backend = _filled_backend(glob_matches=[], contents={})
    backend.find_output = ""
    backend.find_exit_code = 0
    monkeypatch.setattr(
        sandbox_manager,
        "SANDBOX_MANAGER",
        SimpleNamespace(get_backend=lambda _t, *, component: backend),
    )
    runtime = _runtime(InMemoryStore())

    with pytest.raises(
        RuntimeError,
        match=r"/skills/main/demo-skill/.*（物理 /demo-skill/）",
    ):
        await skill_tools.assign_skill.coroutine(
            skill_name="demo-skill",
            targets=["crawl-worker"],
            runtime=runtime,
        )
