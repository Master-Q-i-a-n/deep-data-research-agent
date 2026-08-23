from __future__ import annotations

import asyncio

import pytest

from deep_data_research_agent.infrastructure.redis import lock as redis_lock


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key, value, *, nx=False, px=None):
        del px
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, _count, key, token, *args):
        if "PEXPIRE" in script:
            del args
            return int(self.values.get(key) == token)
        if self.values.get(key) == token:
            self.values.pop(key, None)
            return 1
        return 0


@pytest.mark.asyncio
async def test_distributed_lock_releases_only_its_owner(monkeypatch) -> None:
    client = FakeRedis()
    monkeypatch.setattr(redis_lock, "get_redis", lambda: client)

    async with redis_lock.distributed_lock(
        "lock", wait_seconds=0.1, lease_seconds=3, renew_seconds=1
    ) as lease:
        assert client.values["lock"] == lease.token
        client.values["lock"] = "new-owner"

    assert client.values["lock"] == "new-owner"


@pytest.mark.asyncio
async def test_distributed_lock_times_out_when_held(monkeypatch) -> None:
    client = FakeRedis()
    client.values["lock"] = "other"
    monkeypatch.setattr(redis_lock, "get_redis", lambda: client)
    monkeypatch.setattr(redis_lock.random, "random", lambda: 0)

    with pytest.raises(redis_lock.DistributedLockUnavailable, match="超时"):
        async with redis_lock.distributed_lock(
            "lock", wait_seconds=0.01, lease_seconds=3, renew_seconds=1
        ):
            await asyncio.sleep(0)
