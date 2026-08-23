"""Privacy-preserving Redis key helpers."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from deep_data_research_agent.core.config import get_settings

logger = logging.getLogger(__name__)
KEY_PREFIX = "ddra:v1"
_DEVELOPMENT_SECRET = secrets.token_bytes(32)
_warning_emitted = False


def key_secret() -> bytes:
    """Return the stable production HMAC secret or a process-local dev secret."""

    global _warning_emitted
    configured = get_settings().rate_limit_key_secret.get_secret_value()
    if configured:
        return configured.encode("utf-8")
    if not _warning_emitted:
        logger.warning("RATE_LIMIT_KEY_SECRET 未配置；开发环境 Redis 键将在进程重启后变化")
        _warning_emitted = True
    return _DEVELOPMENT_SECRET


def digest_key(scope: str, raw_key: str) -> str:
    payload = f"{scope}\0{raw_key}".encode()
    return hmac.new(key_secret(), payload, hashlib.sha256).hexdigest()


__all__ = ["KEY_PREFIX", "digest_key", "key_secret"]
