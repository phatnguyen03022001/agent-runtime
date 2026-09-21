from __future__ import annotations

import os
import stat

from .contracts import CapabilityFailure, FsListEntry, FsListResult
from .errors import RuntimeValidationError
from .fs_safety import FsSafetyError, normalized_path, open_directory_at, open_validated_cwd, split_descendant
from .tool_contract import (
    Authority,
    ContractErrorCode,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
    ToolContract,
)

DEFAULT_MAX_ENTRIES = 200
MAX_ENTRIES = 1000
DIRECTORY_SCAN_LIMIT = 10000

FS_LIST_CONTRACT = ToolContract(
    name="fs_list",
    tool_class=ToolClass.READ,
    authority=Authority(
        workspace_bound=True,
        network=NetworkAuthority.NONE,
        mutation=MutationAuthority.NONE,
    ),
    annotations=ToolAnnotations(
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
    ),
    preconditions={
        "cwd": "validated-workspace-descendant",
        "path": {
            "kind": "cwd-relative-directory-or-dot",
            "symlink_traversal": False,
        },
    },
    bounds={
        "max_entries": MAX_ENTRIES,
        "directory_scan_entries": DIRECTORY_SCAN_LIMIT,
    },
    postconditions={
        "recursive": False,
        "ordering": "ascending-utf8-name",
        "entry_metadata": "nofollow",
    },
)


def _from_runtime_validation(exc: RuntimeValidationError) -> CapabilityFailure:
    message = str(exc)
    if "outside AGENT_RUNTIME_WORKSPACE_ROOT" in message:
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


def _from_safety(exc: FsSafetyError) -> CapabilityFailure:
    mapping = {
        "NOT_FOUND": (ContractErrorCode.NOT_FOUND, "PATH_NOT_FOUND", "directory path was not found"),
        "ACCESS_DENIED": (
            ContractErrorCode.PERMISSION_DENIED,
            "ACCESS_DENIED",
            "directory path could not be accessed",
        ),
        "SYMLINK_DISALLOWED": (
            ContractErrorCode.PRECONDITION_FAILED,
            "SYMLINK_DISALLOWED",
            "symlink traversal is not allowed",
        ),
        "NOT_DIRECTORY": (
            ContractErrorCode.PRECONDITION_FAILED,
            "PATH_NOT_DIRECTORY",
            "path does not identify a directory",
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


def _kind(observed: os.stat_result) -> tuple[str, int | None]:
    mode = observed.st_mode
    if stat.S_ISREG(mode):
        return "file", observed.st_size
    if stat.S_ISDIR(mode):
        return "directory", None
    if stat.S_ISLNK(mode):
        return "symlink", None
    return "other", None


def list_directory(cwd: str, path: str = ".", max_entries: int = DEFAULT_MAX_ENTRIES) -> FsListResult:
    """List one directory without recursion or symlink traversal."""

    try:
        components = split_descendant(path, allow_dot=True)
    except ValueError as exc:
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_PATH",
            "path must be '.' or a normalized cwd-relative descendant directory",
        ) from exc
    if type(max_entries) is not int or not 1 <= max_entries <= MAX_ENTRIES:
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_MAX_ENTRIES",
            "max_entries must be an integer from 1 through 1000",
        )

    try:
        _checked_cwd, cwd_fd = open_validated_cwd(cwd)
    except RuntimeValidationError as exc:
        raise _from_runtime_validation(exc) from None
    except FsSafetyError as exc:
        raise _from_safety(exc) from None

    try:
        try:
            directory_fd = open_directory_at(cwd_fd, components)
        except FsSafetyError as exc:
            raise _from_safety(exc) from None

        scanned = 0
        skipped_invalid_names = 0
        retained: list[FsListEntry] = []
        try:
            with os.scandir(directory_fd) as iterator:
                for entry in iterator:
                    scanned += 1
                    if scanned > DIRECTORY_SCAN_LIMIT:
                        raise CapabilityFailure(
                            ContractErrorCode.LIMIT_EXCEEDED,
                            "DIRECTORY_SCAN_LIMIT",
                            "directory contains more than 10000 entries",
                        )
                    name = entry.name
                    try:
                        name.encode("utf-8", errors="strict")
                    except UnicodeEncodeError:
                        skipped_invalid_names += 1
                        continue
                    try:
                        observed = entry.stat(follow_symlinks=False)
                    except PermissionError:
                        raise CapabilityFailure(
                            ContractErrorCode.PERMISSION_DENIED,
                            "ENTRY_STAT_DENIED",
                            "directory entry metadata could not be accessed",
                        ) from None
                    except OSError:
                        raise CapabilityFailure(
                            ContractErrorCode.STATE_CHANGED,
                            "LOCAL_STATE_CHANGED",
                            "directory state changed during listing",
                        ) from None
                    kind, size = _kind(observed)
                    child_components = components + (name,)
                    retained.append(
                        FsListEntry(
                            name=name,
                            path="/".join(child_components),
                            kind=kind,
                            size_bytes=size,
                        )
                    )
        finally:
            os.close(directory_fd)

        retained.sort(key=lambda item: item.name)
        truncated = len(retained) > max_entries
        return FsListResult(
            schema_version=1,
            path=normalized_path(components),
            entries=retained[:max_entries],
            truncated=truncated,
            scanned_entries=scanned,
            skipped_invalid_names=skipped_invalid_names,
        )
    finally:
        os.close(cwd_fd)
