from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum


__all__ = [
    "ToolClass",
    "NetworkAuthority",
    "MutationAuthority",
    "Authority",
    "ContractErrorCode",
    "EffectState",
    "SafeNextAction",
    "ContractError",
    "ReceiptV1",
    "ToolAnnotations",
    "ToolContract",
    "canonical_structured_bytes",
    "frame_bytes",
    "make_receipt_v1",
]


_RECEIPT_NAMESPACE = b"agent-runtime.receipt.v1"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_UINT64_MAX = (1 << 64) - 1


class ToolClass(str, Enum):
    READ = "read"
    WRITE = "write"
    PROCESS = "process"
    REPO = "repo"
    HOST = "host"


class NetworkAuthority(str, Enum):
    NONE = "none"
    BOUNDED = "bounded"


class MutationAuthority(str, Enum):
    NONE = "none"
    BOUNDED = "bounded"
    DESTRUCTIVE = "destructive"


class ContractErrorCode(str, Enum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    OUTSIDE_WORKSPACE = "OUTSIDE_WORKSPACE"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    STATE_CHANGED = "STATE_CHANGED"
    CONFLICT = "CONFLICT"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class EffectState(str, Enum):
    ABSENT = "absent"
    PRESENT = "present"
    UNKNOWN = "unknown"


class SafeNextAction(str, Enum):
    FIX_REQUEST = "fix_request"
    RETRY = "retry"
    WAIT = "wait"
    RECONCILE = "reconcile"
    UNSUPPORTED = "unsupported"
    REPORT_DEFECT = "report_defect"


def _validate_utf8_string(value: object, field: str, *, nonempty: bool) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be a string")
    if nonempty and value == "":
        raise ValueError(f"{field} must be non-empty")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc
    return value


def _validate_structured(value: object, path: str = "$") -> None:
    value_type = type(value)
    if value is None or value_type is bool or value_type is int:
        return
    if value_type is str:
        _validate_utf8_string(value, path, nonempty=False)
        return
    if value_type is list:
        for index, item in enumerate(value):
            _validate_structured(item, f"{path}[{index}]")
        return
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"{path} object keys must be strings")
            _validate_utf8_string(key, f"{path} key", nonempty=False)
            _validate_structured(item, f"{path}.{key}")
        return
    raise TypeError(f"{path} contains unsupported structured value type: {value_type.__name__}")


def canonical_structured_bytes(value: object) -> bytes:
    """Return deterministic compact UTF-8 JSON for the supported structured subset."""

    _validate_structured(value)
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return rendered.encode("utf-8", errors="strict")


def frame_bytes(payload: bytes) -> bytes:
    """Frame exact bytes as uint64 big-endian byte length followed by the payload."""

    if type(payload) is not bytes:
        raise TypeError("payload must be exact bytes")
    size = len(payload)
    if size > _UINT64_MAX:
        raise ValueError("payload length exceeds uint64")
    return size.to_bytes(8, "big", signed=False) + payload


@dataclass(frozen=True, slots=True)
class Authority:
    workspace_bound: bool
    network: NetworkAuthority
    mutation: MutationAuthority

    def __post_init__(self) -> None:
        if type(self.workspace_bound) is not bool:
            raise TypeError("workspace_bound must be bool")
        if not isinstance(self.network, NetworkAuthority):
            raise TypeError("network must be NetworkAuthority")
        if not isinstance(self.mutation, MutationAuthority):
            raise TypeError("mutation must be MutationAuthority")


@dataclass(frozen=True, slots=True)
class ContractError:
    code: ContractErrorCode
    reason_code: str
    retryable: bool
    effect_state: EffectState
    reconciliation_required: bool
    safe_next_action: SafeNextAction

    def __post_init__(self) -> None:
        if not isinstance(self.code, ContractErrorCode):
            raise TypeError("code must be ContractErrorCode")
        _validate_utf8_string(self.reason_code, "reason_code", nonempty=True)
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be bool")
        if not isinstance(self.effect_state, EffectState):
            raise TypeError("effect_state must be EffectState")
        if type(self.reconciliation_required) is not bool:
            raise TypeError("reconciliation_required must be bool")
        if not isinstance(self.safe_next_action, SafeNextAction):
            raise TypeError("safe_next_action must be SafeNextAction")
        if self.effect_state is EffectState.UNKNOWN and not self.reconciliation_required:
            raise ValueError("unknown effect_state requires reconciliation")
        if self.reconciliation_required and self.retryable:
            raise ValueError("reconciliation-required failures are not directly retryable")
        if self.reconciliation_required != (self.safe_next_action is SafeNextAction.RECONCILE):
            raise ValueError("reconciliation_required must match safe_next_action=reconcile")


@dataclass(frozen=True, slots=True)
class ReceiptV1:
    schema_version: int
    kind: str
    digest: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be exactly 1")
        _validate_utf8_string(self.kind, "kind", nonempty=True)
        digest = _validate_utf8_string(self.digest, "digest", nonempty=True)
        if _SHA256_HEX.fullmatch(digest) is None:
            raise ValueError("digest must be exact lowercase SHA-256 hex")


@dataclass(frozen=True, slots=True)
class ToolAnnotations:
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool

    def __post_init__(self) -> None:
        for field in ("read_only", "destructive", "idempotent", "open_world"):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be bool")


@dataclass(frozen=True, slots=True)
class ToolContract:
    name: str
    tool_class: ToolClass
    authority: Authority
    annotations: ToolAnnotations
    preconditions: object
    bounds: object
    postconditions: object

    def __post_init__(self) -> None:
        _validate_utf8_string(self.name, "name", nonempty=True)
        if not isinstance(self.tool_class, ToolClass):
            raise TypeError("tool_class must be ToolClass")
        if not isinstance(self.authority, Authority):
            raise TypeError("authority must be Authority")
        if not isinstance(self.annotations, ToolAnnotations):
            raise TypeError("annotations must be ToolAnnotations")
        canonical_structured_bytes(self.preconditions)
        canonical_structured_bytes(self.bounds)
        canonical_structured_bytes(self.postconditions)


def make_receipt_v1(
    *,
    kind: str,
    subject: object,
    semantic_parameters: object,
    observed_state_bytes: bytes,
) -> ReceiptV1:
    """Create a deterministic receipt over explicit semantic inputs and complete observed bytes."""

    kind_value = _validate_utf8_string(kind, "kind", nonempty=True)
    if type(observed_state_bytes) is not bytes:
        raise TypeError("observed_state_bytes must be exact bytes")
    subject_bytes = canonical_structured_bytes(subject)
    parameter_bytes = canonical_structured_bytes(semantic_parameters)
    material = b"".join(
        (
            frame_bytes(_RECEIPT_NAMESPACE),
            frame_bytes(kind_value.encode("utf-8", errors="strict")),
            frame_bytes(subject_bytes),
            frame_bytes(parameter_bytes),
            frame_bytes(observed_state_bytes),
        )
    )
    digest = hashlib.sha256(material).hexdigest()
    return ReceiptV1(schema_version=1, kind=kind_value, digest=digest)
