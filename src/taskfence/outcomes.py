"""Outcome types and denial codes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Code(StrEnum):
    BAD_TOKEN = "BAD_TOKEN"                    # malformed token
    BAD_SIGNATURE = "BAD_SIGNATURE"
    UNKNOWN_KEY = "UNKNOWN_KEY"                # kid not in the key ring (e.g. removed after compromise)
    EXPIRED = "EXPIRED"
    NO_GRANT = "NO_GRANT"                      # no fence row for this grant
    STALE_EPOCH = "STALE_EPOCH"                # token epoch below the fence: revoked or superseded
    FUTURE_EPOCH = "FUTURE_EPOCH"              # token epoch above any epoch issued here
    PRINCIPAL_MISMATCH = "PRINCIPAL_MISMATCH"
    OPERATION_MISMATCH = "OPERATION_MISMATCH"
    RESOURCE_NOT_IN_SCOPE = "RESOURCE_NOT_IN_SCOPE"
    TASK_CANCELLED = "TASK_CANCELLED"
    OPERATION_CANCELLED = "OPERATION_CANCELLED"
    PREDICATE_FAILED = "PREDICATE_FAILED"      # application predicate returned False


class TaskFenceError(Exception):
    """Base class for all TaskFence errors."""


class Denied(TaskFenceError):
    """The effect must not commit. Roll back the transaction."""

    def __init__(self, code: Code, detail: str = ""):
        self.code = Code(code)
        self.detail = detail
        super().__init__(f"{self.code}{': ' + detail if detail else ''}")


class Duplicate(TaskFenceError):
    """This operation_id already committed. Roll back; the earlier effect stands."""

    def __init__(self, operation_id: str):
        self.operation_id = operation_id
        super().__init__(f"operation already committed: {operation_id}")


class Retry(TaskFenceError):
    """Transient database conflict (lock timeout, serialization failure). Safe to retry."""


class UsageError(TaskFenceError):
    """The library was called in a way that would void its guarantee."""


@dataclass(frozen=True)
class Admission:
    """Returned by `guard()`. Provisional until the caller's transaction commits."""

    operation_id: str
    task_id: str
    grant_id: str
    epoch: int
    resource: str | None
    seq: int


@dataclass(frozen=True)
class Grant:
    grant_id: str
    principal: str
    operation: str
    epoch: int
    resources: tuple[str, ...]


@dataclass(frozen=True)
class Revocation:
    grant_id: str
    effective: bool        # embedded mode: True once this call returns
    min_epoch: int
    seq: int


@dataclass(frozen=True)
class Receipt:
    """Owner acknowledgment of a federated INSTALL or FENCE message."""

    msg_id: str
    kind: str
    grant_id: str
    epoch: int
    applied: bool
    min_epoch: int
    duplicate: bool
