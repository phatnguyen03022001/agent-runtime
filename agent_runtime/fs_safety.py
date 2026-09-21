from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from .executor import _validated_cwd_with_identity, _workspace_root


class FsSafetyError(Exception):
    """Low-level descriptor-relative filesystem safety failure."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _directory_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    return flags


def _cwd_directory_flags() -> int:
    nofollow_any = getattr(os, "O_NOFOLLOW_ANY", os.O_NOFOLLOW)
    return os.O_RDONLY | os.O_DIRECTORY | nofollow_any | getattr(os, "O_CLOEXEC", 0)


def _file_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def split_descendant(path: str, *, allow_dot: bool = False) -> tuple[str, ...]:
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    if "\x00" in path:
        raise ValueError("path must not contain NUL")
    if path.startswith("/"):
        raise ValueError("path must be cwd-relative")
    if allow_dot and path == ".":
        return ()
    components = tuple(path.split("/"))
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("path must be a normalized descendant without empty, dot, or dot-dot components")
    return components


def normalized_path(components: tuple[str, ...]) -> str:
    return "." if not components else "/".join(components)


def _stat_nofollow(parent_fd: int, component: str) -> os.stat_result | None:
    try:
        return os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return None


def _classify_open_error(
    exc: OSError,
    parent_fd: int,
    component: str,
    *,
    expected: str,
) -> FsSafetyError:
    if exc.errno in {errno.EACCES, errno.EPERM}:
        return FsSafetyError("ACCESS_DENIED")
    if exc.errno == errno.ELOOP:
        return FsSafetyError("SYMLINK_DISALLOWED")
    observed = _stat_nofollow(parent_fd, component)
    if observed is not None:
        if stat.S_ISLNK(observed.st_mode):
            return FsSafetyError("SYMLINK_DISALLOWED")
        if expected == "directory" and not stat.S_ISDIR(observed.st_mode):
            return FsSafetyError("NOT_DIRECTORY")
        if expected == "file" and not stat.S_ISREG(observed.st_mode):
            return FsSafetyError("NOT_REGULAR_FILE")
    if exc.errno in {errno.ENOENT, errno.ESTALE}:
        return FsSafetyError("NOT_FOUND")
    return FsSafetyError("IO_ERROR")


def open_validated_cwd(raw_cwd: str) -> tuple[Path, int]:
    root = _workspace_root()
    checked_cwd, validated_identity = _validated_cwd_with_identity(raw_cwd, root)
    try:
        cwd_fd = os.open(str(checked_cwd), _cwd_directory_flags())
        opened = os.fstat(cwd_fd)
    except OSError as exc:
        try:
            os.close(cwd_fd)  # type: ignore[possibly-undefined]
        except (OSError, UnboundLocalError):
            pass
        raise FsSafetyError("CWD_OPEN_FAILED") from exc
    if not stat.S_ISDIR(opened.st_mode) or validated_identity != (opened.st_dev, opened.st_ino):
        os.close(cwd_fd)
        raise FsSafetyError("CWD_STATE_CHANGED")
    return checked_cwd, cwd_fd


def open_directory_at(cwd_fd: int, components: tuple[str, ...]) -> int:
    current_fd = os.dup(cwd_fd)
    try:
        for component in components:
            observed = _stat_nofollow(current_fd, component)
            if observed is not None:
                if stat.S_ISLNK(observed.st_mode):
                    raise FsSafetyError("SYMLINK_DISALLOWED")
                if not stat.S_ISDIR(observed.st_mode):
                    raise FsSafetyError("NOT_DIRECTORY")
            try:
                next_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
            except OSError as exc:
                raise _classify_open_error(
                    exc, current_fd, component, expected="directory"
                ) from None
            opened = os.fstat(next_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_fd)
                raise FsSafetyError("NOT_DIRECTORY")
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def open_parent_at(cwd_fd: int, components: tuple[str, ...]) -> tuple[int, str]:
    if not components:
        raise ValueError("file path requires at least one descendant component")
    parent_fd = open_directory_at(cwd_fd, components[:-1])
    return parent_fd, components[-1]


def open_regular_at(cwd_fd: int, components: tuple[str, ...]) -> tuple[int, os.stat_result]:
    parent_fd, final = open_parent_at(cwd_fd, components)
    try:
        return open_regular_from_parent(parent_fd, final)
    finally:
        os.close(parent_fd)


def open_regular_from_parent(parent_fd: int, final: str) -> tuple[int, os.stat_result]:
    observed = _stat_nofollow(parent_fd, final)
    if observed is not None:
        if stat.S_ISLNK(observed.st_mode):
            raise FsSafetyError("SYMLINK_DISALLOWED")
        if not stat.S_ISREG(observed.st_mode):
            raise FsSafetyError("NOT_REGULAR_FILE")
    try:
        file_fd = os.open(final, _file_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise _classify_open_error(exc, parent_fd, final, expected="file") from None
    opened = os.fstat(file_fd)
    if not stat.S_ISREG(opened.st_mode):
        os.close(file_fd)
        raise FsSafetyError("NOT_REGULAR_FILE")
    return file_fd, opened


def file_identity(observed: os.stat_result) -> tuple[int, int]:
    return observed.st_dev, observed.st_ino


def unlink_at_best_effort(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        pass
