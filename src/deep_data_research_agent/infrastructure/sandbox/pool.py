"""Shared clean-sandbox buffer; borrowed instances remain owned by their thread."""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
import threading
import time
from collections.abc import Callable
from datetime import timedelta

from opensandbox import PoolCreationSpec, SandboxPoolSync, SandboxSync
from opensandbox.config import ConnectionConfigSync
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import NetworkPolicy
from opensandbox.redis_pool_store import RedisPoolStateStore
from redis import Redis

from deep_data_research_agent.core.config import Settings
from deep_data_research_agent.infrastructure.redis.client import redis_password

logger = logging.getLogger(__name__)
# 后台预热在线程中运行，必须与请求冷创建共享线程安全的门限。
CREATE_GATE = threading.BoundedSemaphore(1)
BASELINE_PATH = "/tmp/.ddra-internal/base-distributions.json"
BASELINE_CODE = (
    "import importlib.metadata as m, json, re; "
    "normalize=lambda value: re.sub(r'[-_.]+', '-', value).lower(); "
    "print(json.dumps(sorted({normalize(d.metadata['Name']) "
    "for d in m.distributions() if d.metadata['Name']})))"
)


def create_with_retries(factory: Callable[[], SandboxSync]) -> SandboxSync:
    """Hold the creation gate throughout transient Docker startup retries."""

    with CREATE_GATE:
        for attempt, delay in enumerate((1.0, 3.0, None)):
            try:
                return factory()
            except Exception as exc:
                code = str(getattr(getattr(exc, "error", None), "code", ""))
                retryable = (
                    code.endswith("SANDBOX_START_FAILED")
                    or code == "READY_TIMEOUT"
                    or "Egress sidecar container failed to start" in str(exc)
                )
                if not retryable or delay is None:
                    raise
                logger.warning(
                    "沙箱冷启动失败，第 %d 次重试前等待 %.0f 秒", attempt + 1, delay
                )
                time.sleep(delay)
    raise AssertionError("unreachable")


class WarmSandboxPool:
    """Own SDK scheduler and its synchronous Redis transport in one process."""

    def __init__(self, settings: Settings, connection: ConnectionConfigSync) -> None:
        self._redis = Redis.from_url(
            settings.redis_url,
            username=settings.redis_username,
            password=redis_password(settings.redis_password_file),
            decode_responses=True,
            protocol=2,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_socket_timeout_seconds,
        )
        identity = json.dumps(
            [
                settings.open_sandbox_protocol,
                settings.open_sandbox_domain,
                settings.open_sandbox_image,
                "allow",
            ],
            separators=(",", ":"),
        )
        self.name = "networked-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        self._settings = settings
        self._metadata = {"ddra_pool": self.name}
        self.sdk = SandboxPoolSync(
            pool_name=self.name,
            max_idle=settings.sandbox_pool_max_idle,
            state_store=RedisPoolStateStore(self._redis, key_prefix="ddra:v1:pool"),
            connection_config=connection,
            creation_spec=PoolCreationSpec(
                image=settings.open_sandbox_image,
                metadata=self._metadata,
                network_policy=NetworkPolicy(defaultAction="allow", egress=[]),
            ),
            warmup_concurrency=1,
            reconcile_interval=timedelta(
                seconds=settings.sandbox_pool_reconcile_interval_seconds
            ),
            idle_timeout=timedelta(seconds=settings.sandbox_pool_idle_timeout_seconds),
            sandbox_creator=self._create,
            warmup_sandbox_preparer=self._prepare,
            max_acquire_retries=3,
        )

    def _create(self, context) -> SandboxSync:
        return create_with_retries(
            lambda: SandboxSync.create(
                self._settings.open_sandbox_image,
                timeout=context.idle_timeout,
                metadata=self._metadata,
                network_policy=NetworkPolicy(defaultAction="allow", egress=[]),
                connection_config=context.connection_config,
                ready_timeout=context.ready_timeout,
                skip_health_check=context.skip_health_check,
                health_check=context.health_check,
                health_check_polling_interval=context.health_check_polling_interval,
            )
        )

    @staticmethod
    def _prepare(sandbox: SandboxSync) -> None:
        # 基础依赖在任何用户代码运行前采集；不向空闲容器写入私有文件。
        command = (
            "mkdir -p /workspace /tmp/.ddra-internal && python -c "
            + shlex.quote(BASELINE_CODE)
            + " > "
            + shlex.quote(BASELINE_PATH)
        )
        result = sandbox.commands.run(command, opts=RunCommandOpts())
        if result.exit_code not in {None, 0}:
            raise RuntimeError("预热沙箱基础初始化失败")

    def start(self) -> None:
        self.sdk.start()

    def close(self) -> None:
        try:
            # 不清空共享池：其他进程可能还在领取，空闲实例由服务端 TTL 回收。
            self.sdk.shutdown()
        finally:
            self._redis.close()
