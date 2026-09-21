from __future__ import annotations

import errno
import os
import stat

from .contracts import FsReadErrorCode, FsReadItem
from .errors import RuntimeValidationError
from .executor import _validated_cwd_with_identity, _workspace_root
from .tool_contract import (
    Authority,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
    ToolContract,
)

ITEM_OUTPUT_LIMIT_BYTES = 128 * 1024
BATCH_OUTPUT_LIMIT_BYTES = 256 * 1024
ITEM_SCAN_LIMIT_BYTES = 1024 * 1024
BATCH_SCAN_LIMIT_BYTES = 4 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024

FS_READ_BATCH_CONTRACT = ToolContract(
    name="fs_read_batch",
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
            "kind": "cwd-relative-descendant",
            "disallowed_components": ["", ".", ".."],
            "symlink_traversal": False,
            "regular_files_only": True,
        },
    },
    bounds={
        "max_items": 20,
        "item_output_bytes": ITEM_OUTPUT_LIMIT_BYTES,
        "batch_output_bytes": BATCH_OUTPUT_LIMIT_BYTES,
        "item_scan_bytes": ITEM_SCAN_LIMIT_BYTES,
        "batch_scan_bytes": BATCH_SCAN_LIMIT_BYTES,
    },
    postconditions={
        "content_encoding": "utf-8-strict",
        "result_order": "request-order",
        "filesystem_failures": "per-item",
    },
)

_ERROR_MESSAGES: dict[str, str] = {
    "NOT_FOUND": "File not found",
    "ACCESS_DENIED": "Access denied",
    "SYMLINK_DISALLOWED": "Symlink components are not allowed",
    "NOT_REGULAR_FILE": "Path does not identify a regular file",
    "INVALID_UTF8": "File content is not valid UTF-8",
    "ITEM_OUTPUT_LIMIT_EXCEEDED": "Selected text exceeds the per-item output limit",
    "BATCH_OUTPUT_LIMIT_EXCEEDED": "Selected text exceeds the remaining batch output limit",
    "ITEM_SCAN_LIMIT_EXCEEDED": "File scan exceeds the per-item scan limit",
    "BATCH_SCAN_LIMIT_EXCEEDED": "File scan exceeds the remaining batch scan limit",
    "READ_FAILED": "File could not be read",
}


class _ItemFailure(Exception):
    def __init__(self, code: FsReadErrorCode, scan_bytes: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.scan_bytes = scan_bytes


def _request_item(item: FsReadItem) -> tuple[str, tuple[str, ...], int, int | None]:
    path = item.path
    if path.startswith("/"):
        raise RuntimeValidationError("fs_read_batch item path must be cwd-relative")
    if "\x00" in path:
        raise RuntimeValidationError("fs_read_batch item path must not contain NUL bytes")
    components = tuple(path.split("/"))
    if any(component in {"", ".", ".."} for component in components):
        raise RuntimeValidationError(
            "fs_read_batch item path must be a non-empty descendant path without empty, dot, or dot-dot components"
        )
    start_line = item.start_line if item.start_line is not None else 1
    end_line = item.end_line
    if end_line is not None and end_line < start_line:
        raise RuntimeValidationError("fs_read_batch end_line must be greater than or equal to start_line")
    return path, components, start_line, end_line


def _nofollow_stat(parent_fd: int, component: str) -> os.stat_result | None:
    try:
        return os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return None


def _classify_open_error(exc: OSError, parent_fd: int, component: str, *, final: bool) -> _ItemFailure:
    if exc.errno in {errno.EACCES, errno.EPERM}:
        return _ItemFailure("ACCESS_DENIED")
    if exc.errno == errno.ELOOP:
        return _ItemFailure("SYMLINK_DISALLOWED")
    observed = _nofollow_stat(parent_fd, component)
    if observed is not None:
        if stat.S_ISLNK(observed.st_mode):
            return _ItemFailure("SYMLINK_DISALLOWED")
        if final and not stat.S_ISREG(observed.st_mode):
            return _ItemFailure("NOT_REGULAR_FILE")
        if not final and not stat.S_ISDIR(observed.st_mode):
            return _ItemFailure("NOT_REGULAR_FILE")
    if exc.errno in {errno.ENOENT, errno.ESTALE}:
        return _ItemFailure("NOT_FOUND")
    return _ItemFailure("READ_FAILED")


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _cwd_directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW_ANY | getattr(os, "O_CLOEXEC", 0)


def _file_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _open_regular_at(cwd_fd: int, components: tuple[str, ...]) -> int:
    parent_fd = os.dup(cwd_fd)
    try:
        for component in components[:-1]:
            observed = _nofollow_stat(parent_fd, component)
            if observed is not None:
                if stat.S_ISLNK(observed.st_mode):
                    raise _ItemFailure("SYMLINK_DISALLOWED")
                if not stat.S_ISDIR(observed.st_mode):
                    raise _ItemFailure("NOT_REGULAR_FILE")
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=parent_fd)
            except OSError as exc:
                raise _classify_open_error(exc, parent_fd, component, final=False) from None
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_fd)
                raise _ItemFailure("NOT_REGULAR_FILE")
            os.close(parent_fd)
            parent_fd = next_fd

        final = components[-1]
        observed = _nofollow_stat(parent_fd, final)
        if observed is not None:
            if stat.S_ISLNK(observed.st_mode):
                raise _ItemFailure("SYMLINK_DISALLOWED")
            if not stat.S_ISREG(observed.st_mode):
                raise _ItemFailure("NOT_REGULAR_FILE")
        try:
            file_fd = os.open(final, _file_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise _classify_open_error(exc, parent_fd, final, final=True) from None
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            os.close(file_fd)
            raise _ItemFailure("NOT_REGULAR_FILE")
        return file_fd
    finally:
        os.close(parent_fd)


def _read_selected_text(
    file_fd: int,
    start_line: int,
    end_line: int | None,
    batch_scan_remaining: int,
) -> tuple[str, int]:
    output = bytearray()
    line_number = 1
    pending_cr = False
    pending_cr_selected = False
    scan_bytes = 0

    def selected() -> bool:
        return line_number >= start_line and (end_line is None or line_number <= end_line)

    def fail_incomplete(code: FsReadErrorCode) -> None:
        if scan_bytes >= batch_scan_remaining:
            raise _ItemFailure("BATCH_SCAN_LIMIT_EXCEEDED", scan_bytes)
        if scan_bytes >= ITEM_SCAN_LIMIT_BYTES:
            raise _ItemFailure("ITEM_SCAN_LIMIT_EXCEEDED", scan_bytes)
        raise _ItemFailure(code, scan_bytes)

    def write_selected(raw: bytes) -> None:
        if len(output) + len(raw) > ITEM_OUTPUT_LIMIT_BYTES:
            fail_incomplete("ITEM_OUTPUT_LIMIT_EXCEEDED")
        output.extend(raw)

    def finish_line() -> None:
        nonlocal line_number
        line_number += 1

    done = False
    while not done:
        item_remaining = ITEM_SCAN_LIMIT_BYTES - scan_bytes
        batch_remaining = batch_scan_remaining - scan_bytes
        if batch_remaining <= 0:
            raise _ItemFailure("BATCH_SCAN_LIMIT_EXCEEDED", scan_bytes)
        if item_remaining <= 0:
            raise _ItemFailure("ITEM_SCAN_LIMIT_EXCEEDED", scan_bytes)
        read_size = min(_READ_CHUNK_BYTES, item_remaining, batch_remaining)
        try:
            raw = os.read(file_fd, read_size)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM}:
                fail_incomplete("ACCESS_DENIED")
            fail_incomplete("READ_FAILED")
        scan_bytes += len(raw)
        if not raw:
            if pending_cr and pending_cr_selected:
                write_selected(b"\r")
            break

        for value in raw:
            if pending_cr:
                if value == 0x0A:
                    if pending_cr_selected:
                        write_selected(b"\r\n")
                    pending_cr = False
                    finish_line()
                    if end_line is not None and line_number > end_line:
                        done = True
                        break
                    continue
                if pending_cr_selected:
                    write_selected(b"\r")
                pending_cr = False
                finish_line()
                if end_line is not None and line_number > end_line:
                    done = True
                    break

            if value == 0x0D:
                pending_cr = True
                pending_cr_selected = selected()
            elif value == 0x0A:
                if selected():
                    write_selected(b"\n")
                finish_line()
                if end_line is not None and line_number > end_line:
                    done = True
                    break
            elif selected():
                write_selected(bytes((value,)))

        if not done:
            if scan_bytes >= batch_scan_remaining:
                raise _ItemFailure("BATCH_SCAN_LIMIT_EXCEEDED", scan_bytes)
            if scan_bytes >= ITEM_SCAN_LIMIT_BYTES:
                raise _ItemFailure("ITEM_SCAN_LIMIT_EXCEEDED", scan_bytes)

    try:
        return bytes(output).decode("utf-8", errors="strict"), scan_bytes
    except UnicodeDecodeError:
        raise _ItemFailure("INVALID_UTF8", scan_bytes) from None

def _error_result(
    path: str, start_line: int, end_line: int | None, failure: _ItemFailure
) -> dict[str, object]:
    return {
        "status": "error",
        "path": path,
        "start_line": start_line,
        "end_line": end_line,
        "error_code": failure.code,
        "message": _ERROR_MESSAGES[failure.code],
    }


def read_files_batch(cwd: str, items: list[FsReadItem]) -> dict[str, object]:
    """Read ordered bounded UTF-8 text ranges beneath one validated cwd."""

    requests = [_request_item(item) for item in items]
    root = _workspace_root()
    checked_cwd, validated_identity = _validated_cwd_with_identity(cwd, root)
    try:
        cwd_fd = os.open(str(checked_cwd), _cwd_directory_flags())
    except OSError:
        raise RuntimeValidationError("cwd could not be opened safely") from None

    try:
        opened_cwd = os.fstat(cwd_fd)
    except OSError:
        os.close(cwd_fd)
        raise RuntimeValidationError("cwd could not be opened safely") from None
    if validated_identity != (opened_cwd.st_dev, opened_cwd.st_ino):
        os.close(cwd_fd)
        raise RuntimeValidationError("cwd could not be opened safely")

    successful_bytes = 0
    batch_scan_bytes = 0
    results: list[dict[str, object]] = []
    try:
        for path, components, start_line, end_line in requests:
            if batch_scan_bytes >= BATCH_SCAN_LIMIT_BYTES:
                results.append(
                    _error_result(
                        path,
                        start_line,
                        end_line,
                        _ItemFailure("BATCH_SCAN_LIMIT_EXCEEDED"),
                    )
                )
                continue

            try:
                file_fd = _open_regular_at(cwd_fd, components)
                try:
                    text, item_scan_bytes = _read_selected_text(
                        file_fd,
                        start_line,
                        end_line,
                        BATCH_SCAN_LIMIT_BYTES - batch_scan_bytes,
                    )
                finally:
                    os.close(file_fd)
            except _ItemFailure as failure:
                batch_scan_bytes += failure.scan_bytes
                results.append(_error_result(path, start_line, end_line, failure))
                continue

            batch_scan_bytes += item_scan_bytes
            text_bytes = len(text.encode("utf-8"))
            if successful_bytes + text_bytes > BATCH_OUTPUT_LIMIT_BYTES:
                results.append(
                    _error_result(
                        path,
                        start_line,
                        end_line,
                        _ItemFailure("BATCH_OUTPUT_LIMIT_EXCEEDED"),
                    )
                )
                continue
            successful_bytes += text_bytes
            results.append(
                {
                    "status": "ok",
                    "path": path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "text": text,
                }
            )
    finally:
        os.close(cwd_fd)

    return {"items": results}
