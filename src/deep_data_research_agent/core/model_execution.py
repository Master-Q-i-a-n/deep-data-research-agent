"""Provider-agnostic model execution requirements."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelExecutionProfile:
    """Describe how a caller wants to use a model without naming that caller."""

    name: str
    harness_provider: str = "openai"
    enable_streaming: bool = False
    enable_hosted_web_search: bool = False
    max_retries: int = 2

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("模型执行配置名称不能为空")
        if not self.harness_provider.strip():
            raise ValueError("模型 Harness Provider 不能为空")
        if self.max_retries < 0:
            raise ValueError("模型重试次数不能为负数")


__all__ = ["ModelExecutionProfile"]
