import os
from pathlib import PurePosixPath
from types import SimpleNamespace
from uuid import uuid4

import pytest

from deep_data_research_agent.core.config import Settings
from deep_data_research_agent.infrastructure import workspace as workspace_module
from deep_data_research_agent.infrastructure.workspace import (
    LocalWorkspaceStore,
    OSSWorkspaceStore,
    WorkspaceFileNotFound,
    WorkspaceScope,
    WorkspaceStorageError,
)


@pytest.mark.asyncio
async def test_local_workspace_store_contract_and_thread_isolation(tmp_path) -> None:
    store = LocalWorkspaceStore(tmp_path)
    target = WorkspaceScope("user-a", "thread-a", "supervisor")
    sibling = WorkspaceScope("user-a", "thread-b", "supervisor")

    await store.write_many(
        target,
        [
            ("output/report.md", "中文".encode()),
            ("input/orders.csv", b"id\n1\n"),
        ],
    )
    await store.write_many(sibling, [("output/report.md", b"sibling")])

    listed = await store.list(target)
    assert [item.path for item in listed] == [
        "/workspace/input/orders.csv",
        "/workspace/output/report.md",
    ]
    assert await store.read(target, "/workspace/output/report.md") == "中文".encode()
    metadata, chunks = await store.stream(target, "output/report.md")
    assert metadata.size == len("中文".encode())
    assert b"".join([chunk async for chunk in chunks]) == "中文".encode()

    await store.delete_file(target, "input/orders.csv")
    with pytest.raises(WorkspaceFileNotFound):
        await store.stat(target, "input/orders.csv")

    await store.delete_thread("user-a", "thread-a")
    assert await store.list(target) == []
    assert await store.read(sibling, "output/report.md") == b"sibling"


def test_workspace_scope_and_path_reject_cross_prefix_values(tmp_path) -> None:
    store = LocalWorkspaceStore(tmp_path)
    with pytest.raises(ValueError):
        WorkspaceScope("../user", "thread-a", "supervisor")
    with pytest.raises(ValueError):
        store.workspace_path(WorkspaceScope("user-a", "thread/a", "supervisor"))


def test_production_requires_complete_oss_workspace_configuration() -> None:
    with pytest.raises(ValueError, match="WORKSPACE_STORAGE_BACKEND"):
        Settings(
            _env_file=None,
            app_env="production",
            rate_limit_key_secret="x" * 32,
        )
    with pytest.raises(ValueError, match="OSS_BUCKET_NAME"):
        Settings(
            _env_file=None,
            app_env="production",
            rate_limit_key_secret="x" * 32,
            workspace_storage_backend="oss",
        )


class _AsyncBody:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        self.closed = True

    async def read(self) -> bytes:
        return self.content

    async def iter_bytes(self, **_kwargs):
        yield self.content


class _FakeOSSClient:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.put_keys: list[str] = []
        self.put_content_types: list[str | None] = []
        self.delete_batches: list[list[str]] = []
        self.closed = False

    async def list_objects_v2(self, request):
        keys = sorted(key for key in self.files if key.startswith(request.prefix or ""))
        start = int(request.continuation_token or 0)
        limit = int(request.max_keys or 1000)
        page = keys[start : start + limit]
        next_offset = start + len(page)
        return SimpleNamespace(
            contents=[
                SimpleNamespace(key=key, size=len(self.files[key]), etag=f"etag-{key}")
                for key in page
            ],
            is_truncated=next_offset < len(keys),
            next_continuation_token=(
                str(next_offset) if next_offset < len(keys) else None
            ),
        )

    async def put_object(self, request):
        self.files[request.key] = bytes(request.body)
        self.put_keys.append(request.key)
        self.put_content_types.append(request.content_type)
        return SimpleNamespace(etag="etag")

    async def get_object(self, request):
        if request.key not in self.files:
            raise WorkspaceFileNotFound(request.key)
        content = self.files[request.key]
        return SimpleNamespace(
            body=_AsyncBody(content),
            content_length=len(content),
            content_type="application/octet-stream",
            etag="etag",
        )

    async def head_object(self, request):
        if request.key not in self.files:
            raise WorkspaceFileNotFound(request.key)
        content = self.files[request.key]
        return SimpleNamespace(
            content_length=len(content),
            content_type=None,
            etag="etag",
        )

    async def delete_object(self, request):
        self.files.pop(request.key, None)

    async def delete_multiple_objects(self, request):
        keys = [item.key for item in request.delete.objects]
        self.delete_batches.append(keys)
        for key in keys:
            self.files.pop(key, None)

    async def close(self):
        self.closed = True


def _fake_oss_store() -> tuple[OSSWorkspaceStore, _FakeOSSClient]:
    store = object.__new__(OSSWorkspaceStore)
    store.region = "cn-beijing"
    store.endpoint = "https://oss-cn-beijing-internal.aliyuncs.com"
    store.bucket = "test-bucket"
    store.prefix = "users"
    client = _FakeOSSClient()
    store._client = client
    return store, client


def test_oss_store_uses_hardened_ecs_role_credentials(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCredentialsConfig:
        def __init__(self, **kwargs) -> None:
            captured["credentials"] = kwargs

    class FakeCredentialsClient:
        def __init__(self, config) -> None:
            captured["credentials_config"] = config

        def get_credential(self):
            return SimpleNamespace(
                access_key_id="sts-id",
                access_key_secret="sts-secret",
                security_token="sts-token",
            )

    class FakeAsyncClient:
        def __init__(self, config) -> None:
            captured["oss_config"] = config

    monkeypatch.setattr(workspace_module, "CredentialsConfig", FakeCredentialsConfig)
    monkeypatch.setattr(workspace_module, "CredentialsClient", FakeCredentialsClient)
    monkeypatch.setattr(workspace_module.oss_aio, "AsyncClient", FakeAsyncClient)

    store = OSSWorkspaceStore(
        region="cn-beijing",
        endpoint="https://oss-cn-beijing-internal.aliyuncs.com",
        bucket="test-bucket",
        prefix="users",
        role_name="DeepAgentsECSRole",
    )

    assert captured["credentials"] == {
        "type": "ecs_ram_role",
        "role_name": "DeepAgentsECSRole",
        "disable_imds_v1": True,
    }
    config = captured["oss_config"]
    assert config.region == "cn-beijing"
    assert config.endpoint == "https://oss-cn-beijing-internal.aliyuncs.com"
    assert store.bucket == "test-bucket"


@pytest.mark.asyncio
async def test_oss_store_maps_keys_pages_and_streams() -> None:
    store, client = _fake_oss_store()
    scope = WorkspaceScope("user-a", "thread-a", "supervisor")

    await store.write_many(scope, [("output/report.md", b"report")])
    expected_key = "users/user-a/jobs/thread-a/supervisor/workspace/output/report.md"
    assert client.put_keys == [expected_key]
    assert client.put_content_types == ["text/markdown"]
    assert [item.path for item in await store.list(scope, prefix="output")] == [
        "/workspace/output/report.md"
    ]
    assert await store.read(scope, "output/report.md") == b"report"
    metadata, chunks = await store.stream(scope, "output/report.md")
    assert metadata.size == 6
    assert b"".join([chunk async for chunk in chunks]) == b"report"


@pytest.mark.asyncio
async def test_oss_store_deletes_more_than_one_batch_without_touching_sibling() -> None:
    store, client = _fake_oss_store()
    target_prefix = "users/user-a/jobs/thread-a/supervisor/workspace/"
    for index in range(1001):
        client.files[f"{target_prefix}output/{index}.json"] = b"{}"
    sibling_key = "users/user-a/jobs/thread-b/supervisor/workspace/output/keep.json"
    client.files[sibling_key] = b"{}"

    await store.delete_thread("user-a", "thread-a")

    assert [len(batch) for batch in client.delete_batches] == [1000, 1]
    assert sibling_key in client.files
    assert not any(key.startswith(target_prefix) for key in client.files)


@pytest.mark.asyncio
async def test_oss_store_normalizes_transport_failures() -> None:
    store, client = _fake_oss_store()
    scope = WorkspaceScope("user-a", "thread-a", "supervisor")

    async def fail(_request):
        raise OSError("network detail must not escape")

    client.put_object = fail
    with pytest.raises(WorkspaceStorageError, match="OSS 请求失败"):
        await store.write_many(scope, [(PurePosixPath("output/report.md"), b"x")])


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_OSS_INTEGRATION") != "1",
    reason="需要在已绑定 ECS RAM Role 的实例上显式启用 OSS 集成测试",
)
async def test_real_oss_workspace_roundtrip() -> None:
    required = {
        "region": os.getenv("OSS_REGION", ""),
        "endpoint": os.getenv("OSS_ENDPOINT", ""),
        "bucket": os.getenv("OSS_BUCKET_NAME", ""),
        "role_name": os.getenv("OSS_ECS_RAM_ROLE", ""),
    }
    if not all(required.values()):
        pytest.fail("OSS 集成测试缺少环境变量")
    prefix = f"integration-tests/{uuid4().hex}"
    store = OSSWorkspaceStore(prefix=prefix, **required)
    scope = WorkspaceScope("test-user", "test-thread", "supervisor")
    try:
        await store.check_ready()
        await store.write_many(scope, [("output/hello.txt", b"oss-ok")])
        assert await store.read(scope, "output/hello.txt") == b"oss-ok"
        assert len(await store.list(scope)) == 1
        await store.delete_thread(scope.user_id, scope.thread_id)
        assert await store.list(scope) == []
    finally:
        await store.delete_thread(scope.user_id, scope.thread_id)
        await store.close()
