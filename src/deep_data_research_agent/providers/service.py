"""Secure persistence and network validation for user model Providers."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

from deep_data_research_agent.core.config import get_settings
from deep_data_research_agent.database import repository as database

_STORED_PROVIDER_TYPE = "openai_compatible"


class ProviderConfigurationError(ValueError):
    """Raised when Provider configuration is missing, unsafe, or unreadable."""


class ProviderNotConfiguredError(ProviderConfigurationError):
    """Raised when an online model call has no per-user Provider."""


@dataclass(frozen=True, slots=True)
class ResolvedProvider:
    user_id: str
    base_url: str
    model_name: str
    api_key: str
    api_key_hint: str
    version: int


def _fernet(path: Path | None = None) -> Fernet:
    key_path = path or get_settings().model_provider_encryption_key_file
    try:
        key = key_path.read_bytes().strip()
    except OSError as exc:
        raise ProviderConfigurationError("模型 Provider 加密密钥不可用") from exc
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:
        raise ProviderConfigurationError("模型 Provider 加密密钥格式无效") from exc


def check_encryption_ready() -> None:
    """Validate the deployment key in production and any configured dev key."""

    settings = get_settings()
    path = settings.model_provider_encryption_key_file
    if settings.app_env != "production" and not path.exists():
        return
    _fernet(path)


def encrypt_api_key(api_key: str) -> tuple[str, str]:
    normalized = api_key.strip()
    if not normalized:
        raise ProviderConfigurationError("API Key 不能为空")
    if len(normalized) > 4096:
        raise ProviderConfigurationError("API Key 过长")
    ciphertext = _fernet().encrypt(normalized.encode("utf-8")).decode("ascii")
    return ciphertext, normalized[-4:]


def decrypt_api_key(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise ProviderConfigurationError("模型 Provider API Key 无法解密") from exc


def _allowlist_entries() -> tuple[set[str], list[ipaddress.IPv4Network | ipaddress.IPv6Network]]:
    hosts: set[str] = set()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in get_settings().model_provider_host_allowlist.split(","):
        entry = raw.strip().lower().rstrip(".")
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            hosts.add(entry)
    return hosts, networks


def normalize_provider_url(value: str) -> str:
    """Normalize a Provider base URL without inventing a `/v1` path."""

    raw = value.strip()
    if not raw or len(raw) > 2048:
        raise ProviderConfigurationError("API Base URL 不能为空或过长")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ProviderConfigurationError("API Base URL 必须是 HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ProviderConfigurationError("API Base URL 不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ProviderConfigurationError("API Base URL 不能包含查询参数或片段")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderConfigurationError("API Base URL 端口无效") from exc
    hostname = parsed.hostname.lower().rstrip(".")
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{display_host}:{port}" if port is not None else display_host
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def _resolve_addresses(hostname: str, port: int) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ProviderConfigurationError("API Base URL 域名无法解析") from exc
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for result in results:
        raw = result[4][0]
        try:
            addresses.add(ipaddress.ip_address(raw))
        except ValueError:
            continue
    if not addresses:
        raise ProviderConfigurationError("API Base URL 未解析到有效地址")
    return addresses


async def validate_provider_url(value: str) -> str:
    """Enforce public HTTPS unless the deployment explicitly allows the target."""

    normalized = normalize_provider_url(value)
    parsed = urlsplit(normalized)
    hostname = parsed.hostname or ""
    hosts, networks = _allowlist_entries()
    host_allowed = hostname in hosts
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = await asyncio.to_thread(_resolve_addresses, hostname, port)
    addresses_public = all(address.is_global for address in addresses)
    addresses_allowlisted = bool(networks) and all(
        any(address in network for network in networks) for address in addresses
    )
    target_allowlisted = host_allowed or addresses_allowlisted
    if parsed.scheme != "https" and not target_allowlisted:
        raise ProviderConfigurationError("HTTP Provider 地址必须加入部署白名单")
    if not addresses_public and not target_allowlisted:
        raise ProviderConfigurationError("私网或保留地址必须加入部署白名单")
    return normalized


def _validate_model_name(model_name: str) -> str:
    normalized_model = model_name.strip()
    if not normalized_model or len(normalized_model) > 128:
        raise ProviderConfigurationError("模型名不能为空或过长")
    return normalized_model


async def save_provider(
    *,
    user_id: str,
    base_url: str,
    model_name: str,
    api_key: str | None,
) -> database.ModelProviderRecord:
    normalized_model = _validate_model_name(model_name)
    normalized_url = await validate_provider_url(base_url)
    ciphertext: str | None = None
    hint: str | None = None
    if api_key is not None:
        ciphertext, hint = encrypt_api_key(api_key)
    return await database.upsert_model_provider(
        user_id=user_id,
        # Keep the legacy column populated until a future schema cleanup. It is
        # no longer exposed or used for runtime routing.
        provider_type=_STORED_PROVIDER_TYPE,
        base_url=normalized_url,
        model_name=normalized_model,
        api_key_ciphertext=ciphertext,
        api_key_hint=hint,
    )


async def resolve_provider(user_id: str) -> ResolvedProvider:
    record = await database.get_model_provider(user_id)
    if record is None:
        raise ProviderNotConfiguredError("请先配置模型 Provider")
    # Revalidate at use time so changed DNS cannot silently bypass save-time checks.
    base_url = await validate_provider_url(record.base_url)
    return ResolvedProvider(
        user_id=record.user_id,
        base_url=base_url,
        model_name=record.model_name,
        api_key=decrypt_api_key(record.api_key_ciphertext),
        api_key_hint=record.api_key_hint,
        version=record.version,
    )


async def get_public_provider(user_id: str) -> dict[str, object] | None:
    record = await database.get_model_provider(user_id)
    if record is None:
        return None
    return {
        "base_url": record.base_url,
        "model_name": record.model_name,
        "has_api_key": True,
        "api_key_hint": record.api_key_hint,
        "version": record.version,
        "updated_at": record.updated_at.isoformat(),
    }


async def delete_provider(user_id: str) -> bool:
    return await database.delete_model_provider(user_id)


__all__ = [
    "ProviderConfigurationError",
    "ProviderNotConfiguredError",
    "ResolvedProvider",
    "check_encryption_ready",
    "delete_provider",
    "encrypt_api_key",
    "get_public_provider",
    "normalize_provider_url",
    "resolve_provider",
    "save_provider",
    "validate_provider_url",
]
