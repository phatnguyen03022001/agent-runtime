from __future__ import annotations

import hashlib
import os
import secrets
import stat

from .contracts import CapabilityFailure, FsPatchEdit, FsPatchResult
from .errors import RuntimeValidationError
from .fs_safety import (
    FsSafetyError,
    file_identity,
    open_parent_at,
    open_regular_from_parent,
    open_validated_cwd,
    split_descendant,
    unlink_at_best_effort,
)
from .tool_contract import (
    Authority,
    ContractErrorCode,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
    ToolContract,
)

MAX_INPUT_FILE_BYTES = 1024 * 1024
MAX_OUTPUT_FILE_BYTES = 1024 * 1024
MAX_EDITS = 20
MAX_EDIT_BYTES = 256 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_TEMP_ATTEMPTS = 32

FS_PATCH_CONTRACT = ToolContract(
    name="fs_patch",
    tool_class=ToolClass.WRITE,
    authority=Authority(
        workspace_bound=True,
        network=NetworkAuthority.NONE,
        mutation=MutationAuthority.BOUNDED,
    ),
    annotations=ToolAnnotations(
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=False,
    ),
    preconditions={
        "cwd": "validated-workspace-descendant",
        "path": "normalized-cwd-relative-regular-file",
        "expected_sha256": "exact-lowercase-sha256",
        "edits": {
            "minimum": 1,
            "maximum": MAX_EDITS,
            "old_text_occurrences": "exactly-one-in-evolving-text",
        },
        "symlink_traversal": False,
    },
    bounds={
        "input_file_bytes": MAX_INPUT_FILE_BYTES,
        "output_file_bytes": MAX_OUTPUT_FILE_BYTES,
        "max_edits": MAX_EDITS,
        "aggregate_edit_utf8_bytes": MAX_EDIT_BYTES,
    },
    postconditions={
        "write": "same-directory-temp-fsync-atomic-replace-parent-fsync",
        "mode_bits": "preserved",
        "pre_replace_revalidation": "identity-and-original-sha256",
        "temp_cleanup": "best-effort",
    },
)


def _runtime_failure(exc: RuntimeValidationError) -> CapabilityFailure:
    if "outside AGENT_RUNTIME_WORKSPACE_ROOT" in str(exc):
        return CapabilityFailure(
            ContractErrorCode.OUTSIDE_WORKSPACE,
            "CWD_OUTSIDE_WORKSPACE",
            "cwd resolves outside the configured workspace",
        )
    return CapabilityFailure(
        ContractErrorCode.INVALID_ARGUMENT,
        "INVALID_CWD",
        "cwd must identify an existing absolute workspace directory",
    )


def _safety_failure(exc: FsSafetyError) -> CapabilityFailure:
    mapping = {
        "NOT_FOUND": (ContractErrorCode.NOT_FOUND, "PATH_NOT_FOUND", "target file was not found"),
        "ACCESS_DENIED": (
            ContractErrorCode.PERMISSION_DENIED,
            "ACCESS_DENIED",
            "target file could not be accessed",
        ),
        "SYMLINK_DISALLOWED": (
            ContractErrorCode.PRECONDITION_FAILED,
            "SYMLINK_DISALLOWED",
            "symlink traversal is not allowed",
        ),
        "NOT_DIRECTORY": (
            ContractErrorCode.PRECONDITION_FAILED,
            "PARENT_NOT_DIRECTORY",
            "target parent path is not a directory",
        ),
        "NOT_REGULAR_FILE": (
            ContractErrorCode.PRECONDITION_FAILED,
            "NOT_REGULAR_FILE",
            "target path does not identify a regular file",
        ),
        "CWD_STATE_CHANGED": (
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "cwd identity changed during validation",
        ),
    }
    code, reason, message = mapping.get(
        exc.reason,
        (ContractErrorCode.INTERNAL_ERROR, "FILESYSTEM_ERROR", "filesystem operation failed"),
    )
    return CapabilityFailure(code, reason, message)


def _read_bounded(file_fd: int, limit: int) -> bytes:
    observed = os.fstat(file_fd)
    if observed.st_size > limit:
        raise CapabilityFailure(
            ContractErrorCode.LIMIT_EXCEEDED,
            "INPUT_FILE_LIMIT",
            "input file exceeds 1 MiB",
        )
    output = bytearray()
    while True:
        remaining = limit - len(output)
        if remaining <= 0:
            current = os.fstat(file_fd)
            if current.st_size > len(output):
                raise CapabilityFailure(
                    ContractErrorCode.LIMIT_EXCEEDED,
                    "INPUT_FILE_LIMIT",
                    "input file exceeds 1 MiB",
                )
            return bytes(output)
        try:
            chunk = os.read(file_fd, min(_READ_CHUNK_BYTES, remaining))
        except OSError as exc:
            raise CapabilityFailure(
                ContractErrorCode.INTERNAL_ERROR,
                "READ_FAILED",
                "target file could not be read",
            ) from exc
        if not chunk:
            return bytes(output)
        output.extend(chunk)


def _write_all(file_fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(file_fd, payload[offset:])
        except OSError as exc:
            raise CapabilityFailure(
                ContractErrorCode.INTERNAL_ERROR,
                "WRITE_FAILED",
                "temporary file could not be written",
            ) from exc
        if written <= 0:
            raise CapabilityFailure(
                ContractErrorCode.INTERNAL_ERROR,
                "WRITE_FAILED",
                "temporary file write made no progress",
            )
        offset += written


def _create_temp(parent_fd: int, mode_bits: int) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _ in range(_TEMP_ATTEMPTS):
        name = f".agent-runtime-patch-{secrets.token_hex(12)}.tmp"
        try:
            fd = os.open(name, flags, mode_bits, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except PermissionError as exc:
            raise CapabilityFailure(
                ContractErrorCode.PERMISSION_DENIED,
                "TEMP_CREATE_DENIED",
                "same-directory temporary file could not be created",
            ) from exc
        except OSError as exc:
            raise CapabilityFailure(
                ContractErrorCode.INTERNAL_ERROR,
                "TEMP_CREATE_FAILED",
                "same-directory temporary file could not be created",
            ) from exc
        try:
            os.fchmod(fd, mode_bits)
        except OSError:
            os.close(fd)
            unlink_at_best_effort(parent_fd, name)
            raise CapabilityFailure(
                ContractErrorCode.INTERNAL_ERROR,
                "MODE_PRESERVE_FAILED",
                "temporary file mode could not be set",
            ) from None
        return fd, name
    raise CapabilityFailure(
        ContractErrorCode.INTERNAL_ERROR,
        "TEMP_NAME_EXHAUSTED",
        "same-directory temporary file name allocation failed",
    )


def _revalidate_target(
    parent_fd: int,
    final: str,
    expected_identity: tuple[int, int],
    expected_sha256: str,
) -> None:
    try:
        check_fd, observed = open_regular_from_parent(parent_fd, final)
    except FsSafetyError as exc:
        raise CapabilityFailure(
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "target state changed before replacement",
        ) from exc
    try:
        if file_identity(observed) != expected_identity:
            raise CapabilityFailure(
                ContractErrorCode.STATE_CHANGED,
                "LOCAL_STATE_CHANGED",
                "target identity changed before replacement",
            )
        raw = _read_bounded(check_fd, MAX_INPUT_FILE_BYTES)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_sha256:
            raise CapabilityFailure(
                ContractErrorCode.STATE_CHANGED,
                "LOCAL_STATE_CHANGED",
                "target content changed before replacement",
            )
    finally:
        os.close(check_fd)


def patch_file(
    cwd: str,
    path: str,
    expected_sha256: str,
    edits: list[FsPatchEdit],
) -> FsPatchResult:
    """Apply exact text replacements under expected-state and atomic-write guards."""

    try:
        components = split_descendant(path, allow_dot=False)
    except ValueError as exc:
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_PATH",
            "path must be a normalized cwd-relative descendant file",
        ) from exc
    if (
        type(expected_sha256) is not str
        or len(expected_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in expected_sha256)
    ):
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_EXPECTED_SHA256",
            "expected_sha256 must be exact lowercase SHA-256 hex",
        )
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_EDITS",
            "edits must contain between 1 and 20 items",
        )

    aggregate_edit_bytes = 0
    for edit in edits:
        if not isinstance(edit, FsPatchEdit):
            raise CapabilityFailure(
                ContractErrorCode.INVALID_ARGUMENT,
                "INVALID_EDIT",
                "each edit must contain strict old_text and new_text strings",
            )
        try:
            old_raw = edit.old_text.encode("utf-8", errors="strict")
            new_raw = edit.new_text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise CapabilityFailure(
                ContractErrorCode.INVALID_ARGUMENT,
                "INVALID_EDIT_UTF8",
                "edit text must be valid UTF-8",
            ) from exc
        if not old_raw:
            raise CapabilityFailure(
                ContractErrorCode.INVALID_ARGUMENT,
                "EMPTY_OLD_TEXT",
                "old_text must be non-empty",
            )
        aggregate_edit_bytes += len(old_raw) + len(new_raw)
        if aggregate_edit_bytes > MAX_EDIT_BYTES:
            raise CapabilityFailure(
                ContractErrorCode.LIMIT_EXCEEDED,
                "EDIT_BYTES_LIMIT",
                "aggregate edit text exceeds 256 KiB",
            )

    try:
        _checked_cwd, cwd_fd = open_validated_cwd(cwd)
    except RuntimeValidationError as exc:
        raise _runtime_failure(exc) from None
    except FsSafetyError as exc:
        raise _safety_failure(exc) from None

    parent_fd = -1
    target_fd = -1
    temp_fd = -1
    temp_name: str | None = None
    try:
        try:
            parent_fd, final = open_parent_at(cwd_fd, components)
            target_fd, observed = open_regular_from_parent(parent_fd, final)
        except FsSafetyError as exc:
            raise _safety_failure(exc) from None

        original_identity = file_identity(observed)
        mode_bits = stat.S_IMODE(observed.st_mode)
        original = _read_bounded(target_fd, MAX_INPUT_FILE_BYTES)
        os.close(target_fd)
        target_fd = -1

        if b"\x00" in original:
            raise CapabilityFailure(
                ContractErrorCode.PRECONDITION_FAILED,
                "BINARY_CONTENT",
                "target file contains NUL bytes",
            )
        try:
            text = original.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CapabilityFailure(
                ContractErrorCode.PRECONDITION_FAILED,
                "INVALID_UTF8",
                "target file is not valid UTF-8",
            ) from exc

        sha_before = hashlib.sha256(original).hexdigest()
        if sha_before != expected_sha256:
            raise CapabilityFailure(
                ContractErrorCode.PRECONDITION_FAILED,
                "EXPECTED_SHA256_MISMATCH",
                "target SHA-256 does not match expected_sha256",
            )

        evolving = text
        for edit in edits:
            occurrences = evolving.count(edit.old_text)
            if occurrences == 0:
                raise CapabilityFailure(
                    ContractErrorCode.PRECONDITION_FAILED,
                    "OLD_TEXT_NOT_FOUND",
                    "old_text was not found in the evolving target text",
                )
            if occurrences != 1:
                raise CapabilityFailure(
                    ContractErrorCode.CONFLICT,
                    "OLD_TEXT_NOT_UNIQUE",
                    "old_text is not unique in the evolving target text",
                )
            evolving = evolving.replace(edit.old_text, edit.new_text, 1)

        try:
            replacement = evolving.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise CapabilityFailure(
                ContractErrorCode.INVALID_ARGUMENT,
                "INVALID_OUTPUT_UTF8",
                "edited output is not valid UTF-8",
            ) from exc
        if len(replacement) > MAX_OUTPUT_FILE_BYTES:
            raise CapabilityFailure(
                ContractErrorCode.LIMIT_EXCEEDED,
                "OUTPUT_FILE_LIMIT",
                "edited output exceeds 1 MiB",
            )

        temp_fd, temp_name = _create_temp(parent_fd, mode_bits)
        _write_all(temp_fd, replacement)
        try:
            os.fsync(temp_fd)
        except OSError as exc:
            raise CapabilityFailure(
                ContractErrorCode.INTERNAL_ERROR,
                "TEMP_FSYNC_FAILED",
                "temporary file could not be fsynced",
            ) from exc
        os.close(temp_fd)
        temp_fd = -1

        _revalidate_target(parent_fd, final, original_identity, sha_before)
        try:
            os.replace(
                temp_name,
                final,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except OSError as exc:
            raise CapabilityFailure(
                ContractErrorCode.INTERNAL_ERROR,
                "ATOMIC_REPLACE_FAILED",
                "atomic target replacement failed",
            ) from exc
        temp_name = None
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise CapabilityFailure(
                ContractErrorCode.INTERNAL_ERROR,
                "PARENT_FSYNC_FAILED",
                "parent directory could not be fsynced after replacement",
            ) from exc

        sha_after = hashlib.sha256(replacement).hexdigest()
        return FsPatchResult(
            schema_version=1,
            path="/".join(components),
            sha256_before=sha_before,
            sha256_after=sha_after,
            bytes_before=len(original),
            bytes_after=len(replacement),
            edits_applied=len(edits),
        )
    finally:
        if target_fd >= 0:
            os.close(target_fd)
        if temp_fd >= 0:
            os.close(temp_fd)
        if parent_fd >= 0 and temp_name is not None:
            unlink_at_best_effort(parent_fd, temp_name)
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(cwd_fd)
