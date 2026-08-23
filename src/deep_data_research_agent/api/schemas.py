"""Validated request payloads for the custom HTTP API."""

from uuid import UUID

from pydantic import BaseModel, Field


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


__all__ = [
    "AsyncTaskStatusRequest",
    "LoginRequest",
    "MemorySettingsRequest",
    "RegisterRequest",
    "RunAdmissionRequest",
]
