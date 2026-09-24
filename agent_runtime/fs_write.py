from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat

from .contracts import (
    CapabilityFailure,
    FsWriteReceiptResult,
    FsWriteResult,
)
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
    EffectState,
    MutationAuthority,
    NetworkAuthority,
    SafeNextAction,
    ToolAnnotations,
    ToolClass,
    ToolContract,
    canonical_structured_bytes,
    frame_bytes,
    make_receipt_v1,
)

MAX_PATH_CHARS = 4096
MAX_CONTENT_BYTES = 1024 * 1024
MAX_INPUT_FILE_BYTES = 1024 * 1024
MAX_OUTPUT_FILE_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_TEMP_ATTEMPTS = 32
_CREATE_MODE = 0o666

FS_WRITE_CONTRACT = ToolContract(
    name="fs_write",
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
        "path": {
            "kind": "normalized-cwd-relative-descendant",
            "max_chars": MAX_PATH_CHARS,
            "symlink_traversal": False,
        },
        "operation": ["create", "replace"],
        "content": {
            "encoding": "utf-8-strict",
            "nul": False,
            "max_utf8_bytes": MAX_CONTENT_BYTES,
        },
        "expected_state": {
            "create": "target-absent",
            "replace": "exact-lowercase-sha256",
        },
    },
    bounds={
        "path_chars": MAX_PATH_CHARS,
        "content_utf8_bytes": MAX_CONTENT_BYTES,
        "replace_input_file_bytes": MAX_INPUT_FILE_BYTES,
        "output_file_bytes": MAX_OUTPUT_FILE_BYTES,
    },
    postconditions={
        "create": "same-directory-temp-fsync-atomic-no-overwrite-link-temp-cleanup-parent-fsync",
        "replace": "same-directory-temp-fsync-mode-preserve-immediate-revalidation-atomic-replace-parent-fsync",
        "receipt_kind": "fs-write",
        "receipt_observed_state": "framed-final-mode-plus-framed-exact-final-file-bytes",
    },
)


def _failure(
    code: ContractErrorCode,
    reason: str,
    message: str,
    *,
    retryable: bool = False,
    effect_state: EffectState = EffectState.ABSENT,
    reconciliation_required: bool = False,
    safe_next_action: SafeNextAction | None = None,
) -> CapabilityFailure:
    if safe_next_action is None:
        if code is ContractErrorCode.LIMIT_EXCEEDED and retryable:
            safe_next_action = SafeNextAction.WAIT
        elif retryable and code in {ContractErrorCode.TIMEOUT, ContractErrorCode.UNAVAILABLE}:
            safe_next_action = SafeNextAction.RETRY
        elif code in {
            ContractErrorCode.INVALID_ARGUMENT,
            ContractErrorCode.OUTSIDE_WORKSPACE,
            ContractErrorCode.PRECONDITION_FAILED,
            ContractErrorCode.STATE_CHANGED,
            ContractErrorCode.CONFLICT,
            ContractErrorCode.PERMISSION_DENIED,
            ContractErrorCode.NOT_FOUND,
            ContractErrorCode.LIMIT_EXCEEDED,
        }:
            safe_next_action = SafeNextAction.FIX_REQUEST
        else:
            safe_next_action = SafeNextAction.REPORT_DEFECT
    return CapabilityFailure(
        code,
        reason,
        message,
        retryable=retryable,
        effect_state=effect_state,
        reconciliation_required=reconciliation_required,
        safe_next_action=safe_next_action,
    )


def _runtime_failure(exc: RuntimeValidationError) -> CapabilityFailure:
    if "outside AGENT_RUNTIME_WORKSPACE_ROOT" in str(exc):
        return _failure(
            ContractErrorCode.OUTSIDE_WORKSPACE,
            "PATH_OUTSIDE_WORKSPACE",
            "cwd resolves outside the configured workspace",
        )
    return _failure(
        ContractErrorCode.INVALID_ARGUMENT,
        "INVALID_PATH",
        "cwd must identify an existing absolute workspace directory",
    )


def _safety_failure(exc: FsSafetyError) -> CapabilityFailure:
    mapping = {
        "NOT_FOUND": (
            ContractErrorCode.NOT_FOUND,
            "TARGET_NOT_FOUND",
            "target or parent path was not found",
            False,
        ),
        "ACCESS_DENIED": (
            ContractErrorCode.PERMISSION_DENIED,
            "FILESYSTEM_PERMISSION_DENIED",
            "filesystem access was denied",
            False,
        ),
        "SYMLINK_DISALLOWED": (
            ContractErrorCode.PRECONDITION_FAILED,
            "SYMLINK_DISALLOWED",
            "symlink traversal is not allowed",
            False,
        ),
        "NOT_DIRECTORY": (
            ContractErrorCode.PRECONDITION_FAILED,
            "NOT_REGULAR_FILE",
            "target parent path is not a directory",
            False,
        ),
        "NOT_REGULAR_FILE": (
            ContractErrorCode.PRECONDITION_FAILED,
            "NOT_REGULAR_FILE",
            "target path is not a regular file",
            False,
        ),
        "CWD_STATE_CHANGED": (
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "cwd identity changed during validation",
            False,
        ),
    }
    code, reason, message, retryable = mapping.get(
        exc.reason,
        (
            ContractErrorCode.UNAVAILABLE,
            "FILESYSTEM_UNAVAILABLE",
            "filesystem operation is unavailable",
            True,
        ),
    )
    return _failure(code, reason, message, retryable=retryable)


def _validate_request(
    path: str,
    operation: str,
    content: str,
    expected_sha256: str | None,
) -> tuple[tuple[str, ...], bytes]:
    try:
        components = split_descendant(path, allow_dot=False)
    except ValueError as exc:
        raise _failure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_PATH",
            "path must be a normalized cwd-relative descendant file",
        ) from exc

    if operation not in {"create", "replace"}:
        raise _failure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_PATH",
            "operation must be 'create' or 'replace'",
        )
    if type(content) is not str:
        raise _failure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_UTF8_CONTENT",
            "content must be a strict UTF-8 string",
        )
    try:
        payload = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _failure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_UTF8_CONTENT",
            "content must be valid UTF-8",
        ) from exc
    if b"\x00" in payload:
        raise _failure(
            ContractErrorCode.INVALID_ARGUMENT,
            "NUL_CONTENT_DISALLOWED",
            "content must not contain U+0000",
        )
    if len(payload) > MAX_CONTENT_BYTES:
        raise _failure(
            ContractErrorCode.LIMIT_EXCEEDED,
            "OUTPUT_FILE_LIMIT",
            "content exceeds 1 MiB UTF-8 bytes",
        )

    if operation == "create":
        if expected_sha256 is not None:
            raise _failure(
                ContractErrorCode.INVALID_ARGUMENT,
                "EXPECTED_SHA_FORBIDDEN",
                "expected_sha256 must be null for create",
            )
    else:
        if expected_sha256 is None:
            raise _failure(
                ContractErrorCode.INVALID_ARGUMENT,
                "EXPECTED_SHA_REQUIRED",
                "expected_sha256 is required for replace",
            )
        if (
            type(expected_sha256) is not str
            or len(expected_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in expected_sha256)
        ):
            raise _failure(
                ContractErrorCode.INVALID_ARGUMENT,
                "EXPECTED_SHA_REQUIRED",
                "expected_sha256 must be lowercase SHA-256 hex for replace",
            )
    return components, payload


def _read_bounded(file_fd: int, limit: int) -> bytes:
    try:
        observed = os.fstat(file_fd)
    except OSError as exc:
        raise _failure(
            ContractErrorCode.UNAVAILABLE,
            "FILESYSTEM_UNAVAILABLE",
            "target metadata could not be read",
            retryable=True,
        ) from exc
    if observed.st_size > limit:
        raise _failure(
            ContractErrorCode.LIMIT_EXCEEDED,
            "INPUT_FILE_LIMIT",
            "input file exceeds 1 MiB",
        )
    try:
        os.lseek(file_fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise _failure(
            ContractErrorCode.UNAVAILABLE,
            "FILESYSTEM_UNAVAILABLE",
            "target file could not be positioned",
            retryable=True,
        ) from exc
    output = bytearray()
    while True:
        remaining = limit - len(output)
        if remaining <= 0:
            try:
                current = os.fstat(file_fd)
            except OSError as exc:
                raise _failure(
                    ContractErrorCode.UNAVAILABLE,
                    "FILESYSTEM_UNAVAILABLE",
                    "target metadata could not be read",
                    retryable=True,
                ) from exc
            if current.st_size > len(output):
                raise _failure(
                    ContractErrorCode.LIMIT_EXCEEDED,
                    "INPUT_FILE_LIMIT",
                    "input file exceeds 1 MiB",
                )
            return bytes(output)
        try:
            chunk = os.read(file_fd, min(_READ_CHUNK_BYTES, remaining))
        except OSError as exc:
            raise _failure(
                ContractErrorCode.UNAVAILABLE,
                "FILESYSTEM_UNAVAILABLE",
                "target file could not be read",
                retryable=True,
            ) from exc
        if not chunk:
            return bytes(output)
        output.extend(chunk)


def _require_text_target(raw: bytes) -> None:
    if b"\x00" in raw:
        raise _failure(
            ContractErrorCode.PRECONDITION_FAILED,
            "NUL_TARGET_DISALLOWED",
            "target file contains NUL bytes",
        )
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _failure(
            ContractErrorCode.PRECONDITION_FAILED,
            "INVALID_UTF8_TARGET",
            "target file is not valid UTF-8",
        ) from exc


def _write_all(file_fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(file_fd, payload[offset:])
        except PermissionError as exc:
            raise _failure(
                ContractErrorCode.PERMISSION_DENIED,
                "FILESYSTEM_PERMISSION_DENIED",
                "temporary file write was denied",
            ) from exc
        except OSError as exc:
            raise _failure(
                ContractErrorCode.INTERNAL_ERROR,
                "ATOMIC_WRITE_FAILED",
                "temporary file could not be written",
            ) from exc
        if written <= 0:
            raise _failure(
                ContractErrorCode.INTERNAL_ERROR,
                "ATOMIC_WRITE_FAILED",
                "temporary file write made no progress",
            )
        offset += written


def _create_temp(parent_fd: int, mode_bits: int, *, preserve_mode: bool) -> tuple[int, str]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _ in range(_TEMP_ATTEMPTS):
        name = f".agent-runtime-write-{secrets.token_hex(12)}.tmp"
        try:
            file_fd = os.open(name, flags, mode_bits, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except PermissionError as exc:
            raise _failure(
                ContractErrorCode.PERMISSION_DENIED,
                "FILESYSTEM_PERMISSION_DENIED",
                "same-directory temporary file creation was denied",
            ) from exc
        except OSError as exc:
            raise _failure(
                ContractErrorCode.UNAVAILABLE,
                "FILESYSTEM_UNAVAILABLE",
                "same-directory temporary file could not be created",
                retryable=True,
            ) from exc
        if preserve_mode:
            try:
                os.fchmod(file_fd, mode_bits)
            except PermissionError as exc:
                os.close(file_fd)
                unlink_at_best_effort(parent_fd, name)
                raise _failure(
                    ContractErrorCode.PERMISSION_DENIED,
                    "FILESYSTEM_PERMISSION_DENIED",
                    "replacement mode preservation was denied",
                ) from exc
            except OSError as exc:
                os.close(file_fd)
                unlink_at_best_effort(parent_fd, name)
                raise _failure(
                    ContractErrorCode.INTERNAL_ERROR,
                    "ATOMIC_WRITE_FAILED",
                    "replacement mode could not be preserved",
                ) from exc
        return file_fd, name
    raise _failure(
        ContractErrorCode.INTERNAL_ERROR,
        "ATOMIC_WRITE_FAILED",
        "same-directory temporary file name allocation failed",
    )


def _fsync_file(file_fd: int) -> None:
    try:
        os.fsync(file_fd)
    except OSError as exc:
        raise _failure(
            ContractErrorCode.INTERNAL_ERROR,
            "ATOMIC_WRITE_FAILED",
            "temporary file could not be fsynced",
        ) from exc


def _fsync_parent(parent_fd: int) -> None:
    try:
        os.fsync(parent_fd)
    except OSError as exc:
        raise _failure(
            ContractErrorCode.INTERNAL_ERROR,
            "ATOMIC_WRITE_FAILED",
            "parent directory could not be fsynced",
            effect_state=EffectState.PRESENT,
            reconciliation_required=True,
            safe_next_action=SafeNextAction.RECONCILE,
        ) from exc


def _revalidate_target(
    parent_fd: int,
    final: str,
    expected_identity: tuple[int, int],
    expected_sha256: str,
) -> tuple[os.stat_result, bytes]:
    try:
        check_fd, observed = open_regular_from_parent(parent_fd, final)
    except FsSafetyError as exc:
        raise _failure(
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "target state changed before replacement",
        ) from exc
    try:
        if file_identity(observed) != expected_identity:
            raise _failure(
                ContractErrorCode.STATE_CHANGED,
                "LOCAL_STATE_CHANGED",
                "target identity changed before replacement",
            )
        try:
            raw = _read_bounded(check_fd, MAX_INPUT_FILE_BYTES)
        except CapabilityFailure as exc:
            raise _failure(
                ContractErrorCode.STATE_CHANGED,
                "LOCAL_STATE_CHANGED",
                "target content changed before replacement",
            ) from exc
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise _failure(
                ContractErrorCode.STATE_CHANGED,
                "LOCAL_STATE_CHANGED",
                "target content changed before replacement",
            )
        return observed, raw
    finally:
        os.close(check_fd)


def _receipt(
    *,
    checked_cwd: str,
    normalized_path: str,
    operation: str,
    expected_sha256: str | None,
    mode_bits: int,
    final_bytes: bytes,
) -> FsWriteReceiptResult:
    semantic_parameters: dict[str, object]
    if operation == "create":
        semantic_parameters = {"operation": "create", "expected_absent": True}
    else:
        semantic_parameters = {
            "operation": "replace",
            "expected_sha256": expected_sha256,
        }
    observed_state = (
        frame_bytes(canonical_structured_bytes({"mode_bits": mode_bits}))
        + frame_bytes(final_bytes)
    )
    receipt = make_receipt_v1(
        kind="fs-write",
        subject={"cwd": checked_cwd, "path": normalized_path},
        semantic_parameters=semantic_parameters,
        observed_state_bytes=observed_state,
    )
    return FsWriteReceiptResult(
        schema_version=receipt.schema_version,
        kind="fs-write",
        digest=receipt.digest,
    )


def _create(
    *,
    checked_cwd: str,
    cwd_fd: int,
    components: tuple[str, ...],
    payload: bytes,
) -> FsWriteResult:
    parent_fd = -1
    temp_fd = -1
    temp_name: str | None = None
    try:
        try:
            parent_fd, final = open_parent_at(cwd_fd, components)
        except FsSafetyError as exc:
            raise _safety_failure(exc) from None

        temp_fd, temp_name = _create_temp(parent_fd, _CREATE_MODE, preserve_mode=False)
        _write_all(temp_fd, payload)
        _fsync_file(temp_fd)
        published_identity = file_identity(os.fstat(temp_fd))

        try:
            os.link(
                temp_name,
                final,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise _failure(
                ContractErrorCode.PRECONDITION_FAILED,
                "TARGET_ALREADY_EXISTS",
                "create target already exists",
            ) from exc
        except PermissionError as exc:
            raise _failure(
                ContractErrorCode.PERMISSION_DENIED,
                "FILESYSTEM_PERMISSION_DENIED",
                "atomic no-overwrite publication was denied",
            ) from exc
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise _failure(
                    ContractErrorCode.PRECONDITION_FAILED,
                    "TARGET_ALREADY_EXISTS",
                    "create target already exists",
                ) from exc
            raise _failure(
                ContractErrorCode.INTERNAL_ERROR,
                "ATOMIC_WRITE_FAILED",
                "atomic no-overwrite publication failed",
            ) from exc

        cleanup_error: OSError | None = None
        for _ in range(2):
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
                temp_name = None
                cleanup_error = None
                break
            except OSError as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            try:
                observed = os.stat(final, dir_fd=parent_fd, follow_symlinks=False)
                target_is_published = file_identity(observed) == published_identity
            except OSError:
                target_is_published = False
            try:
                _fsync_parent(parent_fd)
            except CapabilityFailure:
                pass
            temp_name = None
            if target_is_published:
                message = "create published target but temporary-name cleanup could not be proven complete"
            else:
                message = "create publication cleanup is ambiguous after target identity changed"
            raise _failure(
                ContractErrorCode.INTERNAL_ERROR,
                "CREATE_CLEANUP_AMBIGUOUS",
                message,
                effect_state=EffectState.PRESENT,
                reconciliation_required=True,
                safe_next_action=SafeNextAction.RECONCILE,
            )

        _fsync_parent(parent_fd)
        observed = os.fstat(temp_fd)
        mode_after = stat.S_IMODE(observed.st_mode)
        final_bytes = _read_bounded(temp_fd, MAX_OUTPUT_FILE_BYTES)
        normalized_path = "/".join(components)
        digest = hashlib.sha256(final_bytes).hexdigest()
        return FsWriteResult(
            schema_version=1,
            status="created",
            path=normalized_path,
            sha256_before=None,
            sha256_after=digest,
            bytes_before=None,
            bytes_after=len(final_bytes),
            mode_before=None,
            mode_after=mode_after,
            write_receipt=_receipt(
                checked_cwd=checked_cwd,
                normalized_path=normalized_path,
                operation="create",
                expected_sha256=None,
                mode_bits=mode_after,
                final_bytes=final_bytes,
            ),
        )
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        if parent_fd >= 0 and temp_name is not None:
            unlink_at_best_effort(parent_fd, temp_name)
        if parent_fd >= 0:
            os.close(parent_fd)


def _replace(
    *,
    checked_cwd: str,
    cwd_fd: int,
    components: tuple[str, ...],
    payload: bytes,
    expected_sha256: str,
) -> FsWriteResult:
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
        mode_before = stat.S_IMODE(observed.st_mode)
        original = _read_bounded(target_fd, MAX_INPUT_FILE_BYTES)
        os.close(target_fd)
        target_fd = -1
        _require_text_target(original)

        sha_before = hashlib.sha256(original).hexdigest()
        if sha_before != expected_sha256:
            raise _failure(
                ContractErrorCode.PRECONDITION_FAILED,
                "EXPECTED_SHA_MISMATCH",
                "target SHA-256 does not match expected_sha256",
            )

        normalized_path = "/".join(components)
        sha_after = hashlib.sha256(payload).hexdigest()
        if payload == original:
            revalidated, final_bytes = _revalidate_target(
                parent_fd,
                final,
                original_identity,
                sha_before,
            )
            mode_after = stat.S_IMODE(revalidated.st_mode)
            return FsWriteResult(
                schema_version=1,
                status="unchanged",
                path=normalized_path,
                sha256_before=sha_before,
                sha256_after=hashlib.sha256(final_bytes).hexdigest(),
                bytes_before=len(original),
                bytes_after=len(final_bytes),
                mode_before=mode_before,
                mode_after=mode_after,
                write_receipt=_receipt(
                    checked_cwd=checked_cwd,
                    normalized_path=normalized_path,
                    operation="replace",
                    expected_sha256=expected_sha256,
                    mode_bits=mode_after,
                    final_bytes=final_bytes,
                ),
            )

        temp_fd, temp_name = _create_temp(parent_fd, mode_before, preserve_mode=True)
        _write_all(temp_fd, payload)
        _fsync_file(temp_fd)

        _revalidate_target(parent_fd, final, original_identity, sha_before)
        try:
            os.replace(
                temp_name,
                final,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except PermissionError as exc:
            raise _failure(
                ContractErrorCode.PERMISSION_DENIED,
                "FILESYSTEM_PERMISSION_DENIED",
                "atomic replacement was denied",
            ) from exc
        except OSError as exc:
            raise _failure(
                ContractErrorCode.INTERNAL_ERROR,
                "ATOMIC_WRITE_FAILED",
                "atomic replacement failed",
            ) from exc
        temp_name = None
        _fsync_parent(parent_fd)

        replacement_stat = os.fstat(temp_fd)
        mode_after = stat.S_IMODE(replacement_stat.st_mode)
        final_bytes = _read_bounded(temp_fd, MAX_OUTPUT_FILE_BYTES)
        return FsWriteResult(
            schema_version=1,
            status="replaced",
            path=normalized_path,
            sha256_before=sha_before,
            sha256_after=hashlib.sha256(final_bytes).hexdigest(),
            bytes_before=len(original),
            bytes_after=len(final_bytes),
            mode_before=mode_before,
            mode_after=mode_after,
            write_receipt=_receipt(
                checked_cwd=checked_cwd,
                normalized_path=normalized_path,
                operation="replace",
                expected_sha256=expected_sha256,
                mode_bits=mode_after,
                final_bytes=final_bytes,
            ),
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


def write_file(
    cwd: str,
    path: str,
    operation: str,
    content: str,
    expected_sha256: str | None = None,
) -> FsWriteResult:
    """Create or replace one whole UTF-8 file under explicit expected-state guards."""

    components, payload = _validate_request(path, operation, content, expected_sha256)
    try:
        checked_cwd, cwd_fd = open_validated_cwd(cwd)
    except RuntimeValidationError as exc:
        raise _runtime_failure(exc) from None
    except FsSafetyError as exc:
        raise _safety_failure(exc) from None

    try:
        if operation == "create":
            return _create(
                checked_cwd=str(checked_cwd),
                cwd_fd=cwd_fd,
                components=components,
                payload=payload,
            )
        assert expected_sha256 is not None
        return _replace(
            checked_cwd=str(checked_cwd),
            cwd_fd=cwd_fd,
            components=components,
            payload=payload,
            expected_sha256=expected_sha256,
        )
    finally:
        os.close(cwd_fd)
