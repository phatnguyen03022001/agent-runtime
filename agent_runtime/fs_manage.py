from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
from pathlib import Path

from .contracts import CapabilityFailure, FsManagePathState, FsManageResult
from .errors import RuntimeValidationError
from .fs_safety import (
    FsSafetyError,
    file_identity,
    open_parent_at,
    open_regular_from_parent,
    open_validated_cwd,
    split_descendant,
)
from .protection import ProtectedRuntimeDenied, is_protected_runtime_path
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
)

MAX_HASH_BYTES = 64 * 1024 * 1024
MAX_PARENT_CREATES = 16
_READ_CHUNK_BYTES = 64 * 1024
_RENAME_EXCL = 0x00000004

_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEATX_NP = getattr(_LIBC, "renameatx_np", None)
if _RENAMEATX_NP is not None:
    _RENAMEATX_NP.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEATX_NP.restype = ctypes.c_int

FS_MANAGE_CONTRACT = ToolContract(
    name="fs_manage",
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
        "path": "normalized-cwd-relative-descendant",
        "symlink_traversal": False,
        "protected_runtime": "denied-before-mutation",
        "expected_state": "explicit-per-operation",
    },
    bounds={
        "operations": ["mkdir", "move", "delete", "chmod"],
        "max_parent_creates": MAX_PARENT_CREATES,
        "max_file_hash_bytes": MAX_HASH_BYTES,
        "move": "same-filesystem-atomic-no-overwrite",
        "delete": "single-file-or-empty-directory",
        "chmod": "regular-file-ordinary-bits-only",
    },
    postconditions={
        "result_state": "verified-after-effect",
        "recursive_delete": False,
        "copy_delete_move_fallback": False,
        "effect_semantics": "task-0144",
    },
)


def _failure(
    code: ContractErrorCode,
    reason_code: str,
    message: str,
    *,
    retryable: bool = False,
    effect_state: EffectState | None = None,
    reconciliation_required: bool | None = None,
    safe_next_action: SafeNextAction | None = None,
) -> CapabilityFailure:
    return CapabilityFailure(
        code,
        reason_code,
        message,
        retryable=retryable,
        effect_state=effect_state,
        reconciliation_required=reconciliation_required,
        safe_next_action=safe_next_action,
    )


def _post_effect_failure(reason_code: str, message: str) -> CapabilityFailure:
    return _failure(
        ContractErrorCode.INTERNAL_ERROR,
        reason_code,
        message,
        effect_state=EffectState.PRESENT,
        reconciliation_required=True,
        safe_next_action=SafeNextAction.RECONCILE,
    )


def _runtime_failure(exc: RuntimeValidationError) -> CapabilityFailure:
    if "outside AGENT_RUNTIME_WORKSPACE_ROOT" in str(exc):
        return _failure(
            ContractErrorCode.OUTSIDE_WORKSPACE,
            "CWD_OUTSIDE_WORKSPACE",
            "cwd resolves outside the configured workspace",
        )
    return _failure(
        ContractErrorCode.INVALID_ARGUMENT,
        "INVALID_CWD",
        "cwd must identify an existing absolute workspace directory",
    )


def _safety_failure(exc: FsSafetyError) -> CapabilityFailure:
    mapping = {
        "NOT_FOUND": (ContractErrorCode.NOT_FOUND, "PATH_NOT_FOUND", "path was not found"),
        "ACCESS_DENIED": (
            ContractErrorCode.PERMISSION_DENIED,
            "ACCESS_DENIED",
            "path could not be accessed",
        ),
        "SYMLINK_DISALLOWED": (
            ContractErrorCode.PRECONDITION_FAILED,
            "SYMLINK_DISALLOWED",
            "symlink traversal is not allowed",
        ),
        "NOT_DIRECTORY": (
            ContractErrorCode.PRECONDITION_FAILED,
            "PARENT_NOT_DIRECTORY",
            "parent path is not a directory",
        ),
        "NOT_REGULAR_FILE": (
            ContractErrorCode.PRECONDITION_FAILED,
            "NOT_REGULAR_FILE",
            "path does not identify a regular file",
        ),
        "CWD_STATE_CHANGED": (
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "cwd identity changed during validation",
        ),
    }
    code, reason, message = mapping.get(
        exc.reason,
        (ContractErrorCode.UNAVAILABLE, "FILESYSTEM_UNAVAILABLE", "filesystem operation failed"),
    )
    if code is ContractErrorCode.UNAVAILABLE:
        return _failure(
            code,
            reason,
            message,
            retryable=True,
            effect_state=EffectState.ABSENT,
            reconciliation_required=False,
            safe_next_action=SafeNextAction.RETRY,
        )
    return _failure(code, reason, message)


def _validate_sha256(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise _failure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_EXPECTED_SHA256",
            f"{field} must be exact lowercase SHA-256 hex",
        )
    return value


def _components(value: object, field: str) -> tuple[str, ...]:
    if type(value) is not str:
        raise _failure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_PATH",
            f"{field} must be a normalized cwd-relative descendant",
        )
    try:
        return split_descendant(value)
    except ValueError as exc:
        raise _failure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_PATH",
            f"{field} must be a normalized cwd-relative descendant",
        ) from exc


def _normalized(components: tuple[str, ...]) -> str:
    return "/".join(components)


def _absent_state(path: str) -> FsManagePathState:
    return FsManagePathState(
        path=path,
        kind="absent",
        device=None,
        inode=None,
        mode=None,
        size_bytes=None,
        sha256=None,
    )


def _state(
    path: str,
    kind: str,
    observed: os.stat_result,
    *,
    sha256: str | None = None,
) -> FsManagePathState:
    return FsManagePathState(
        path=path,
        kind=kind,
        device=observed.st_dev,
        inode=observed.st_ino,
        mode=stat.S_IMODE(observed.st_mode),
        size_bytes=observed.st_size if kind == "file" else None,
        sha256=sha256,
    )


def _lstat(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except PermissionError as exc:
        raise _failure(
            ContractErrorCode.PERMISSION_DENIED,
            "ACCESS_DENIED",
            "path metadata could not be accessed",
        ) from exc
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ESTALE}:
            return None
        raise _failure(
            ContractErrorCode.UNAVAILABLE,
            "FILESYSTEM_UNAVAILABLE",
            "path metadata could not be read",
            retryable=True,
            effect_state=EffectState.ABSENT,
            reconciliation_required=False,
            safe_next_action=SafeNextAction.RETRY,
        ) from exc


def _require_absent(parent_fd: int, name: str, path: str) -> FsManagePathState:
    observed = _lstat(parent_fd, name)
    if observed is None:
        return _absent_state(path)
    if stat.S_ISLNK(observed.st_mode):
        raise _failure(
            ContractErrorCode.PRECONDITION_FAILED,
            "SYMLINK_DISALLOWED",
            "symlink targets are not allowed",
        )
    raise _failure(
        ContractErrorCode.CONFLICT,
        "TARGET_ALREADY_EXISTS",
        "target must be absent",
    )


def _hash_regular_fd(file_fd: int) -> str:
    try:
        observed = os.fstat(file_fd)
    except OSError as exc:
        raise _failure(
            ContractErrorCode.UNAVAILABLE,
            "FILESYSTEM_UNAVAILABLE",
            "file metadata could not be read",
            retryable=True,
            effect_state=EffectState.ABSENT,
            reconciliation_required=False,
            safe_next_action=SafeNextAction.RETRY,
        ) from exc
    if observed.st_size > MAX_HASH_BYTES:
        raise _failure(
            ContractErrorCode.LIMIT_EXCEEDED,
            "FILE_HASH_LIMIT",
            "file exceeds the fs_manage hash bound",
        )
    try:
        os.lseek(file_fd, 0, os.SEEK_SET)
    except OSError as exc:
        raise _failure(
            ContractErrorCode.UNAVAILABLE,
            "FILESYSTEM_UNAVAILABLE",
            "file could not be positioned",
            retryable=True,
            effect_state=EffectState.ABSENT,
            reconciliation_required=False,
            safe_next_action=SafeNextAction.RETRY,
        ) from exc
    digest = hashlib.sha256()
    total = 0
    while True:
        remaining = MAX_HASH_BYTES - total
        if remaining <= 0:
            current = os.fstat(file_fd)
            if current.st_size > total:
                raise _failure(
                    ContractErrorCode.LIMIT_EXCEEDED,
                    "FILE_HASH_LIMIT",
                    "file exceeds the fs_manage hash bound",
                )
            break
        try:
            raw = os.read(file_fd, min(_READ_CHUNK_BYTES, remaining))
        except OSError as exc:
            raise _failure(
                ContractErrorCode.UNAVAILABLE,
                "FILESYSTEM_UNAVAILABLE",
                "file could not be read",
                retryable=True,
                effect_state=EffectState.ABSENT,
                reconciliation_required=False,
                safe_next_action=SafeNextAction.RETRY,
            ) from exc
        if not raw:
            break
        total += len(raw)
        digest.update(raw)
    try:
        final = os.fstat(file_fd)
    except OSError as exc:
        raise _failure(
            ContractErrorCode.UNAVAILABLE,
            "FILESYSTEM_UNAVAILABLE",
            "file metadata could not be revalidated",
            retryable=True,
            effect_state=EffectState.ABSENT,
            reconciliation_required=False,
            safe_next_action=SafeNextAction.RETRY,
        ) from exc
    if final.st_size != total:
        raise _failure(
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "file size changed while hashing",
        )
    return digest.hexdigest()


def _open_file(
    parent_fd: int,
    name: str,
    path: str,
    expected_sha256: str,
    *,
    mismatch_reason: str,
) -> tuple[int, os.stat_result, str]:
    try:
        file_fd, observed = open_regular_from_parent(parent_fd, name)
    except FsSafetyError as exc:
        raise _safety_failure(exc) from None
    try:
        digest = _hash_regular_fd(file_fd)
    except Exception:
        os.close(file_fd)
        raise
    if digest != expected_sha256:
        os.close(file_fd)
        raise _failure(
            ContractErrorCode.PRECONDITION_FAILED,
            mismatch_reason,
            "file SHA-256 does not match the expected state",
        )
    return file_fd, observed, digest


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory_from_parent(parent_fd: int, name: str) -> tuple[int, os.stat_result]:
    observed = _lstat(parent_fd, name)
    if observed is None:
        raise _failure(ContractErrorCode.NOT_FOUND, "PATH_NOT_FOUND", "directory was not found")
    if stat.S_ISLNK(observed.st_mode):
        raise _failure(
            ContractErrorCode.PRECONDITION_FAILED,
            "SYMLINK_DISALLOWED",
            "symlink targets are not allowed",
        )
    if not stat.S_ISDIR(observed.st_mode):
        raise _failure(
            ContractErrorCode.PRECONDITION_FAILED,
            "KIND_MISMATCH",
            "path does not identify a directory",
        )
    try:
        directory_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except PermissionError as exc:
        raise _failure(
            ContractErrorCode.PERMISSION_DENIED,
            "ACCESS_DENIED",
            "directory could not be opened",
        ) from exc
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ESTALE, errno.ELOOP}:
            raise _failure(
                ContractErrorCode.STATE_CHANGED,
                "LOCAL_STATE_CHANGED",
                "directory state changed during validation",
            ) from exc
        raise _failure(
            ContractErrorCode.UNAVAILABLE,
            "FILESYSTEM_UNAVAILABLE",
            "directory could not be opened",
            retryable=True,
            effect_state=EffectState.ABSENT,
            reconciliation_required=False,
            safe_next_action=SafeNextAction.RETRY,
        ) from exc
    opened = os.fstat(directory_fd)
    if not stat.S_ISDIR(opened.st_mode):
        os.close(directory_fd)
        raise _failure(
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "directory state changed during validation",
        )
    return directory_fd, opened


def _protect(checked_cwd: Path, components: tuple[str, ...]) -> None:
    candidate = checked_cwd.joinpath(*components)
    if is_protected_runtime_path(candidate):
        raise ProtectedRuntimeDenied("filesystem_path")


def _revalidate_file(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    expected_sha256: str,
) -> tuple[int, os.stat_result, str]:
    try:
        file_fd, observed = open_regular_from_parent(parent_fd, name)
    except FsSafetyError as exc:
        raise _failure(
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "file state changed before mutation",
        ) from exc
    try:
        digest = _hash_regular_fd(file_fd)
    except CapabilityFailure as exc:
        os.close(file_fd)
        raise _failure(
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "file content changed before mutation",
        ) from exc
    if file_identity(observed) != expected_identity or digest != expected_sha256:
        os.close(file_fd)
        raise _failure(
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "file identity or content changed before mutation",
        )
    return file_fd, observed, digest


def _revalidate_directory(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
) -> tuple[int, os.stat_result]:
    try:
        directory_fd, observed = _open_directory_from_parent(parent_fd, name)
    except CapabilityFailure as exc:
        raise _failure(
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "directory state changed before mutation",
        ) from exc
    if file_identity(observed) != expected_identity:
        os.close(directory_fd)
        raise _failure(
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "directory identity changed before mutation",
        )
    return directory_fd, observed


def _rename_no_replace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    if _RENAMEATX_NP is None:
        raise OSError(errno.ENOTSUP, "atomic no-overwrite rename is unavailable")
    ctypes.set_errno(0)
    result = _RENAMEATX_NP(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        _RENAME_EXCL,
    )
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(error_number, os.strerror(error_number))


def _move_failure(exc: OSError) -> CapabilityFailure:
    if exc.errno == errno.EEXIST:
        return _failure(
            ContractErrorCode.CONFLICT,
            "DESTINATION_ALREADY_EXISTS",
            "move destination must remain absent",
        )
    if exc.errno == errno.EXDEV:
        return _failure(
            ContractErrorCode.PRECONDITION_FAILED,
            "CROSS_DEVICE_MOVE",
            "cross-filesystem move is not allowed",
        )
    if exc.errno in {errno.EACCES, errno.EPERM}:
        return _failure(
            ContractErrorCode.PERMISSION_DENIED,
            "ACCESS_DENIED",
            "atomic move was denied",
        )
    if exc.errno in {errno.ENOENT, errno.ESTALE}:
        return _failure(
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "move source or parent state changed before mutation",
        )
    if exc.errno == errno.EINVAL:
        return _failure(
            ContractErrorCode.PRECONDITION_FAILED,
            "INVALID_MOVE_TOPOLOGY",
            "move topology is not supported",
        )
    if exc.errno in {errno.ENOTSUP, errno.ENOSYS}:
        return _failure(
            ContractErrorCode.UNAVAILABLE,
            "ATOMIC_MOVE_UNAVAILABLE",
            "atomic no-overwrite move is unavailable",
            effect_state=EffectState.ABSENT,
            reconciliation_required=False,
            safe_next_action=SafeNextAction.REPORT_DEFECT,
        )
    return _failure(
        ContractErrorCode.UNAVAILABLE,
        "MOVE_EFFECT_UNKNOWN",
        "atomic move failed without a provable final filesystem state",
        effect_state=EffectState.UNKNOWN,
        reconciliation_required=True,
        safe_next_action=SafeNextAction.RECONCILE,
    )


def _validate_request(
    operation: object,
    path: object,
    source_path: object,
    destination_path: object,
    expected_kind: object,
    expected_sha256: object,
    expected_source_sha256: object,
    parents: object,
    mode: object,
) -> dict[str, object]:
    if operation not in {"mkdir", "move", "delete", "chmod"}:
        raise _failure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_OPERATION",
            "operation must be mkdir, move, delete, or chmod",
        )
    if type(parents) is not bool:
        raise _failure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_OPERATION_ARGUMENTS",
            "parents must be a boolean",
        )

    request: dict[str, object] = {"operation": operation, "parents": parents}
    if operation == "mkdir":
        if (
            path is None
            or source_path is not None
            or destination_path is not None
            or expected_kind is not None
            or expected_sha256 is not None
            or expected_source_sha256 is not None
            or mode is not None
        ):
            raise _failure(
                ContractErrorCode.INVALID_ARGUMENT,
                "INVALID_OPERATION_ARGUMENTS",
                "mkdir accepts only path and optional parents",
            )
        request["path_components"] = _components(path, "path")
        return request

    if parents:
        raise _failure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_OPERATION_ARGUMENTS",
            "parents is valid only for mkdir",
        )

    if operation == "move":
        if (
            path is not None
            or source_path is None
            or destination_path is None
            or expected_kind not in {"file", "directory"}
            or expected_sha256 is not None
            or mode is not None
        ):
            raise _failure(
                ContractErrorCode.INVALID_ARGUMENT,
                "INVALID_OPERATION_ARGUMENTS",
                "move requires source_path, destination_path, and expected_kind",
            )
        request["source_components"] = _components(source_path, "source_path")
        request["destination_components"] = _components(destination_path, "destination_path")
        request["expected_kind"] = expected_kind
        if expected_kind == "file":
            request["expected_source_sha256"] = _validate_sha256(
                expected_source_sha256,
                "expected_source_sha256",
            )
        elif expected_source_sha256 is not None:
            raise _failure(
                ContractErrorCode.INVALID_ARGUMENT,
                "INVALID_OPERATION_ARGUMENTS",
                "directory move must not include expected_source_sha256",
            )
        return request

    if source_path is not None or destination_path is not None or expected_source_sha256 is not None:
        raise _failure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_OPERATION_ARGUMENTS",
            "source_path, destination_path, and expected_source_sha256 are move-only",
        )

    if operation == "delete":
        if path is None or expected_kind not in {"file", "directory"} or mode is not None:
            raise _failure(
                ContractErrorCode.INVALID_ARGUMENT,
                "INVALID_OPERATION_ARGUMENTS",
                "delete requires path and expected_kind",
            )
        request["path_components"] = _components(path, "path")
        request["expected_kind"] = expected_kind
        if expected_kind == "file":
            request["expected_sha256"] = _validate_sha256(expected_sha256, "expected_sha256")
        elif expected_sha256 is not None:
            raise _failure(
                ContractErrorCode.INVALID_ARGUMENT,
                "INVALID_OPERATION_ARGUMENTS",
                "directory delete must not include expected_sha256",
            )
        return request

    if (
        path is None
        or expected_kind is not None
        or expected_sha256 is None
        or type(mode) is not int
        or isinstance(mode, bool)
        or mode < 0
        or mode > 0o777
    ):
        raise _failure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_OPERATION_ARGUMENTS",
            "chmod requires path, expected_sha256, and mode in 0o000..0o777",
        )
    request["path_components"] = _components(path, "path")
    request["expected_sha256"] = _validate_sha256(expected_sha256, "expected_sha256")
    request["mode"] = mode
    return request


def _mkdir(cwd_fd: int, components: tuple[str, ...], *, parents: bool) -> FsManageResult:
    created_before: list[FsManagePathState] = []
    created_after: list[FsManagePathState] = []
    current_fd = os.dup(cwd_fd)
    try:
        for index, component in enumerate(components):
            normalized = _normalized(components[: index + 1])
            observed = _lstat(current_fd, component)
            final = index == len(components) - 1
            if observed is not None:
                if stat.S_ISLNK(observed.st_mode):
                    if created_after:
                        raise _post_effect_failure(
                            "MKDIR_PARTIAL_STATE_CHANGED",
                            "mkdir parents partially applied before a symlink race",
                        )
                    raise _failure(
                        ContractErrorCode.PRECONDITION_FAILED,
                        "SYMLINK_DISALLOWED",
                        "symlink traversal is not allowed",
                    )
                if not stat.S_ISDIR(observed.st_mode):
                    if created_after:
                        raise _post_effect_failure(
                            "MKDIR_PARTIAL_STATE_CHANGED",
                            "mkdir parents partially applied before a non-directory race",
                        )
                    raise _failure(
                        ContractErrorCode.PRECONDITION_FAILED,
                        "PARENT_NOT_DIRECTORY",
                        "mkdir path crosses a non-directory",
                    )
                if final:
                    if created_after:
                        raise _post_effect_failure(
                            "MKDIR_PARTIAL_STATE_CHANGED",
                            "mkdir parents partially applied before the target became occupied",
                        )
                    raise _failure(
                        ContractErrorCode.CONFLICT,
                        "TARGET_ALREADY_EXISTS",
                        "mkdir target must be absent",
                    )
                try:
                    next_fd, _ = _open_directory_from_parent(current_fd, component)
                except CapabilityFailure as exc:
                    if created_after:
                        raise _post_effect_failure(
                            "MKDIR_PARTIAL_STATE_CHANGED",
                            "mkdir parents partially applied before parent revalidation failed",
                        ) from exc
                    raise
                os.close(current_fd)
                current_fd = next_fd
                continue

            remaining = len(components) - index
            if not parents and not final:
                raise _failure(
                    ContractErrorCode.NOT_FOUND,
                    "PARENT_NOT_FOUND",
                    "mkdir parent path does not exist",
                )
            if parents and remaining > MAX_PARENT_CREATES:
                raise _failure(
                    ContractErrorCode.LIMIT_EXCEEDED,
                    "MKDIR_PARENT_LIMIT",
                    "mkdir parents would create more than 16 directories",
                )

            try:
                os.mkdir(component, 0o755, dir_fd=current_fd)
            except FileExistsError as exc:
                if created_after:
                    raise _post_effect_failure(
                        "MKDIR_PARTIAL_STATE_CHANGED",
                        "mkdir parents partially applied before a concurrent state change",
                    ) from exc
                raise _failure(
                    ContractErrorCode.CONFLICT,
                    "TARGET_ALREADY_EXISTS",
                    "mkdir target state changed before creation",
                ) from exc
            except PermissionError as exc:
                if created_after:
                    raise _post_effect_failure(
                        "MKDIR_PARTIAL_EFFECT",
                        "mkdir parents partially applied before permission failure",
                    ) from exc
                raise _failure(
                    ContractErrorCode.PERMISSION_DENIED,
                    "ACCESS_DENIED",
                    "directory creation was denied",
                ) from exc
            except OSError as exc:
                if created_after:
                    raise _post_effect_failure(
                        "MKDIR_PARTIAL_EFFECT",
                        "mkdir parents partially applied before filesystem failure",
                    ) from exc
                raise _failure(
                    ContractErrorCode.UNAVAILABLE,
                    "MKDIR_FAILED",
                    "directory creation failed before effect",
                    retryable=True,
                    effect_state=EffectState.ABSENT,
                    reconciliation_required=False,
                    safe_next_action=SafeNextAction.RETRY,
                ) from exc

            created_before.append(_absent_state(normalized))
            try:
                next_fd, created = _open_directory_from_parent(current_fd, component)
            except CapabilityFailure as exc:
                raise _post_effect_failure(
                    "MKDIR_POSTCONDITION_AMBIGUOUS",
                    "created directory could not be verified",
                ) from exc
            created_after.append(_state(normalized, "directory", created))
            os.close(current_fd)
            current_fd = next_fd

        if not created_after:
            raise AssertionError("mkdir success must create at least one directory")
        return FsManageResult(
            schema_version=1,
            operation="mkdir",
            before=created_before,
            after=created_after,
            effect_state="present",
        )
    finally:
        os.close(current_fd)


def _move(
    cwd_fd: int,
    source_components: tuple[str, ...],
    destination_components: tuple[str, ...],
    *,
    expected_kind: str,
    expected_source_sha256: str | None,
) -> FsManageResult:
    source_parent_fd = destination_parent_fd = -1
    source_check_fd = -1
    try:
        source_parent_fd, source_name = open_parent_at(cwd_fd, source_components)
        destination_parent_fd, destination_name = open_parent_at(cwd_fd, destination_components)
        source_path = _normalized(source_components)
        destination_path = _normalized(destination_components)

        if expected_kind == "file":
            assert expected_source_sha256 is not None
            source_fd, source_stat, source_sha = _open_file(
                source_parent_fd,
                source_name,
                source_path,
                expected_source_sha256,
                mismatch_reason="EXPECTED_SOURCE_SHA256_MISMATCH",
            )
            os.close(source_fd)
            source_before = _state(source_path, "file", source_stat, sha256=source_sha)
        else:
            source_fd, source_stat = _open_directory_from_parent(source_parent_fd, source_name)
            os.close(source_fd)
            source_sha = None
            source_before = _state(source_path, "directory", source_stat)

        destination_before = _require_absent(
            destination_parent_fd,
            destination_name,
            destination_path,
        )
        source_identity = file_identity(source_stat)

        if expected_kind == "file":
            source_check_fd, _, _ = _revalidate_file(
                source_parent_fd,
                source_name,
                source_identity,
                source_sha,
            )
        else:
            source_check_fd, _ = _revalidate_directory(
                source_parent_fd,
                source_name,
                source_identity,
            )

        try:
            _rename_no_replace(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
        except OSError as exc:
            raise _move_failure(exc) from exc

        try:
            source_after_observed = _lstat(source_parent_fd, source_name)
            if source_after_observed is not None:
                raise _post_effect_failure(
                    "MOVE_POSTCONDITION_AMBIGUOUS",
                    "move source path is not absent after rename",
                )
            if expected_kind == "file":
                destination_fd, destination_stat, destination_sha = _open_file(
                    destination_parent_fd,
                    destination_name,
                    destination_path,
                    source_sha,
                    mismatch_reason="MOVE_POSTCONDITION_AMBIGUOUS",
                )
                os.close(destination_fd)
                if file_identity(destination_stat) != source_identity:
                    raise _post_effect_failure(
                        "MOVE_POSTCONDITION_AMBIGUOUS",
                        "move destination identity does not match the source",
                    )
                destination_after = _state(
                    destination_path,
                    "file",
                    destination_stat,
                    sha256=destination_sha,
                )
            else:
                destination_fd, destination_stat = _open_directory_from_parent(
                    destination_parent_fd,
                    destination_name,
                )
                os.close(destination_fd)
                if file_identity(destination_stat) != source_identity:
                    raise _post_effect_failure(
                        "MOVE_POSTCONDITION_AMBIGUOUS",
                        "move destination identity does not match the source",
                    )
                destination_after = _state(destination_path, "directory", destination_stat)
        except CapabilityFailure as exc:
            if (
                exc.effect_state is EffectState.PRESENT
                and exc.reconciliation_required
            ):
                raise
            raise _post_effect_failure(
                "MOVE_POSTCONDITION_AMBIGUOUS",
                "move destination could not be verified after rename",
            ) from exc

        return FsManageResult(
            schema_version=1,
            operation="move",
            before=[source_before, destination_before],
            after=[_absent_state(source_path), destination_after],
            effect_state="present",
        )
    except FsSafetyError as exc:
        raise _safety_failure(exc) from None
    finally:
        if source_check_fd >= 0:
            os.close(source_check_fd)
        if source_parent_fd >= 0:
            os.close(source_parent_fd)
        if destination_parent_fd >= 0:
            os.close(destination_parent_fd)


def _delete(
    cwd_fd: int,
    components: tuple[str, ...],
    *,
    expected_kind: str,
    expected_sha256: str | None,
) -> FsManageResult:
    parent_fd = check_fd = -1
    try:
        parent_fd, name = open_parent_at(cwd_fd, components)
        path = _normalized(components)
        if expected_kind == "file":
            assert expected_sha256 is not None
            file_fd, observed, digest = _open_file(
                parent_fd,
                name,
                path,
                expected_sha256,
                mismatch_reason="EXPECTED_SHA256_MISMATCH",
            )
            os.close(file_fd)
            before = _state(path, "file", observed, sha256=digest)
            check_fd, _, _ = _revalidate_file(
                parent_fd,
                name,
                file_identity(observed),
                digest,
            )
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ESTALE}:
                    raise _failure(
                        ContractErrorCode.STATE_CHANGED,
                        "LOCAL_STATE_CHANGED",
                        "delete target changed before unlink",
                    ) from exc
                if exc.errno in {errno.EACCES, errno.EPERM}:
                    raise _failure(
                        ContractErrorCode.PERMISSION_DENIED,
                        "ACCESS_DENIED",
                        "file delete was denied",
                    ) from exc
                raise _failure(
                    ContractErrorCode.UNAVAILABLE,
                    "DELETE_FAILED",
                    "file delete failed before effect",
                    retryable=True,
                    effect_state=EffectState.ABSENT,
                    reconciliation_required=False,
                    safe_next_action=SafeNextAction.RETRY,
                ) from exc
        else:
            directory_fd, observed = _open_directory_from_parent(parent_fd, name)
            os.close(directory_fd)
            before = _state(path, "directory", observed)
            check_fd, _ = _revalidate_directory(
                parent_fd,
                name,
                file_identity(observed),
            )
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                    raise _failure(
                        ContractErrorCode.PRECONDITION_FAILED,
                        "DIRECTORY_NOT_EMPTY",
                        "delete supports empty directories only",
                    ) from exc
                if exc.errno in {errno.ENOENT, errno.ESTALE}:
                    raise _failure(
                        ContractErrorCode.STATE_CHANGED,
                        "LOCAL_STATE_CHANGED",
                        "delete target changed before rmdir",
                    ) from exc
                if exc.errno in {errno.EACCES, errno.EPERM}:
                    raise _failure(
                        ContractErrorCode.PERMISSION_DENIED,
                        "ACCESS_DENIED",
                        "directory delete was denied",
                    ) from exc
                raise _failure(
                    ContractErrorCode.UNAVAILABLE,
                    "DELETE_FAILED",
                    "directory delete failed before effect",
                    retryable=True,
                    effect_state=EffectState.ABSENT,
                    reconciliation_required=False,
                    safe_next_action=SafeNextAction.RETRY,
                ) from exc

        try:
            remaining = _lstat(parent_fd, name)
        except CapabilityFailure as exc:
            raise _post_effect_failure(
                "DELETE_POSTCONDITION_AMBIGUOUS",
                "delete target absence could not be verified after mutation",
            ) from exc
        if remaining is not None:
            raise _post_effect_failure(
                "DELETE_POSTCONDITION_AMBIGUOUS",
                "delete target is not absent after mutation",
            )
        return FsManageResult(
            schema_version=1,
            operation="delete",
            before=[before],
            after=[_absent_state(path)],
            effect_state="present",
        )
    except FsSafetyError as exc:
        raise _safety_failure(exc) from None
    finally:
        if check_fd >= 0:
            os.close(check_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _chmod(
    cwd_fd: int,
    components: tuple[str, ...],
    *,
    expected_sha256: str,
    mode: int,
) -> FsManageResult:
    parent_fd = check_fd = -1
    try:
        parent_fd, name = open_parent_at(cwd_fd, components)
        path = _normalized(components)
        file_fd, observed, digest = _open_file(
            parent_fd,
            name,
            path,
            expected_sha256,
            mismatch_reason="EXPECTED_SHA256_MISMATCH",
        )
        os.close(file_fd)
        if observed.st_nlink != 1:
            raise _failure(
                ContractErrorCode.PRECONDITION_FAILED,
                "HARDLINK_DISALLOWED",
                "chmod requires a regular file with exactly one link",
            )
        before = _state(path, "file", observed, sha256=digest)
        check_fd, revalidated, revalidated_sha = _revalidate_file(
            parent_fd,
            name,
            file_identity(observed),
            digest,
        )
        if revalidated.st_nlink != 1:
            raise _failure(
                ContractErrorCode.STATE_CHANGED,
                "LOCAL_STATE_CHANGED",
                "file link count changed before chmod",
            )
        if stat.S_IMODE(revalidated.st_mode) == mode:
            return FsManageResult(
                schema_version=1,
                operation="chmod",
                before=[before],
                after=[_state(path, "file", revalidated, sha256=revalidated_sha)],
                effect_state="absent",
            )
        try:
            os.fchmod(check_fd, mode)
        except PermissionError as exc:
            raise _failure(
                ContractErrorCode.PERMISSION_DENIED,
                "ACCESS_DENIED",
                "chmod was denied",
            ) from exc
        except OSError as exc:
            raise _failure(
                ContractErrorCode.UNAVAILABLE,
                "CHMOD_FAILED",
                "chmod failed before effect",
                retryable=True,
                effect_state=EffectState.ABSENT,
                reconciliation_required=False,
                safe_next_action=SafeNextAction.RETRY,
            ) from exc

        try:
            after_stat = os.fstat(check_fd)
            after_sha = _hash_regular_fd(check_fd)
            current_fd, current_stat = open_regular_from_parent(parent_fd, name)
            os.close(current_fd)
        except (OSError, FsSafetyError, CapabilityFailure) as exc:
            raise _post_effect_failure(
                "CHMOD_POSTCONDITION_AMBIGUOUS",
                "chmod target could not be verified after effect",
            ) from exc
        if (
            file_identity(after_stat) != file_identity(observed)
            or file_identity(current_stat) != file_identity(observed)
            or stat.S_IMODE(after_stat.st_mode) != mode
            or after_sha != digest
        ):
            raise _post_effect_failure(
                "CHMOD_POSTCONDITION_AMBIGUOUS",
                "chmod postcondition does not match the requested state",
            )
        return FsManageResult(
            schema_version=1,
            operation="chmod",
            before=[before],
            after=[_state(path, "file", after_stat, sha256=after_sha)],
            effect_state="present",
        )
    except FsSafetyError as exc:
        raise _safety_failure(exc) from None
    finally:
        if check_fd >= 0:
            os.close(check_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def manage_filesystem(
    cwd: str,
    operation: str,
    path: str | None = None,
    source_path: str | None = None,
    destination_path: str | None = None,
    expected_kind: str | None = None,
    expected_sha256: str | None = None,
    expected_source_sha256: str | None = None,
    parents: bool = False,
    mode: int | None = None,
) -> FsManageResult:
    """Apply one bounded guarded filesystem management operation."""

    request = _validate_request(
        operation,
        path,
        source_path,
        destination_path,
        expected_kind,
        expected_sha256,
        expected_source_sha256,
        parents,
        mode,
    )
    try:
        checked_cwd, cwd_fd = open_validated_cwd(cwd)
    except RuntimeValidationError as exc:
        raise _runtime_failure(exc) from None
    except FsSafetyError as exc:
        raise _safety_failure(exc) from None

    try:
        if operation == "move":
            source_components = request["source_components"]
            destination_components = request["destination_components"]
            assert isinstance(source_components, tuple)
            assert isinstance(destination_components, tuple)
            _protect(checked_cwd, source_components)
            _protect(checked_cwd, destination_components)
            return _move(
                cwd_fd,
                source_components,
                destination_components,
                expected_kind=str(request["expected_kind"]),
                expected_source_sha256=(
                    str(request["expected_source_sha256"])
                    if "expected_source_sha256" in request
                    else None
                ),
            )

        path_components = request["path_components"]
        assert isinstance(path_components, tuple)
        _protect(checked_cwd, path_components)
        if operation == "mkdir":
            return _mkdir(cwd_fd, path_components, parents=bool(request["parents"]))
        if operation == "delete":
            return _delete(
                cwd_fd,
                path_components,
                expected_kind=str(request["expected_kind"]),
                expected_sha256=(
                    str(request["expected_sha256"]) if "expected_sha256" in request else None
                ),
            )
        assert operation == "chmod"
        return _chmod(
            cwd_fd,
            path_components,
            expected_sha256=str(request["expected_sha256"]),
            mode=int(request["mode"]),
        )
    finally:
        os.close(cwd_fd)
