"""Validated request payloads for the custom HTTP API."""

from uuid import UUID

from pydantic import BaseModel, Field, SecretStr


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class RunAdmissionRequest(BaseModel):
    submission_id: UUID
    thread_id: UUID | None = None


class MemorySettingsRequest(BaseModel):
    failure_lesson_saving_enabled: bool


class AsyncTaskStatusRequest(BaseModel):
    """Identify the owning Supervisor thread; task IDs come from its state."""

    thread_id: str = Field(min_length=1, max_length=64)


class AsyncTaskCancelRequest(BaseModel):
    """Identify the owning Supervisor thread for a direct child-run cancellation."""

    thread_id: str = Field(min_length=1, max_length=64)


class ModelProviderRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=2048)
    model_name: str = Field(min_length=1, max_length=128)
    # Omit the key while editing to retain the encrypted value already stored.
    api_key: SecretStr | None = None


__all__ = [
    "AsyncTaskCancelRequest",
    "AsyncTaskStatusRequest",
    "LoginRequest",
    "MemorySettingsRequest",
    "ModelProviderRequest",
    "RegisterRequest",
    "RunAdmissionRequest",
]
