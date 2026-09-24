from __future__ import annotations

import hashlib
import heapq
import json
import os
import stat
import time

from .contracts import CapabilityFailure, ContinuationReceiptResult, FsSearchResult, FsSearchResultItem
from .errors import RuntimeValidationError
from .fs_safety import FsSafetyError, open_directory_at, open_regular_at, open_validated_cwd, split_descendant
from .tool_contract import (
    Authority,
    CONTINUATION_CURSOR_MAX_CHARS,
    CONTINUATION_TTL_SECONDS,
    ContinuationFailure,
    ContractErrorCode,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
    ToolContract,
    canonical_structured_bytes,
    frame_bytes,
    make_continuation_cursor,
    make_receipt_v1,
    parse_continuation_cursor,
)

MAX_FILES_SCANNED = 20000
MAX_CONTENT_BYTES_SCANNED = 64 * 1024 * 1024
MAX_RESULTS = 500
MAX_SERIALIZED_RESULT_BYTES = 256 * 1024
CALL_DEADLINE_SECONDS = 5.0
LINE_TEXT_MAX_BYTES = 4096
_READ_CHUNK_BYTES = 64 * 1024

FS_SEARCH_CONTRACT = ToolContract(
    name="fs_search",
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
        "query": "literal-nonempty-utf8",
        "mode": ["content", "path"],
        "root_path": "cwd-relative-directory-or-dot",
        "symlink_traversal": False,
        "git_directory_traversal": False,
    },
    bounds={
        "max_files_scanned": MAX_FILES_SCANNED,
        "max_content_bytes_scanned": MAX_CONTENT_BYTES_SCANNED,
        "max_results": MAX_RESULTS,
        "max_serialized_result_bytes": MAX_SERIALIZED_RESULT_BYTES,
        "deadline_milliseconds": 5000,
        "line_text_bytes": LINE_TEXT_MAX_BYTES,
        "continuation_cursor_chars": CONTINUATION_CURSOR_MAX_CHARS,
        "continuation_ttl_seconds": CONTINUATION_TTL_SECONDS,
    },
    postconditions={
        "matching": "literal-substring",
        "ordering": "normalized-relative-utf8-path-then-line",
        "content_hash": "complete-raw-file-sha256",
        "bounded_completion": True,
        "continuation_consistency": "revalidated",
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
        "NOT_FOUND": (ContractErrorCode.NOT_FOUND, "PATH_NOT_FOUND", "filesystem path was not found"),
        "ACCESS_DENIED": (
            ContractErrorCode.PERMISSION_DENIED,
            "ACCESS_DENIED",
            "filesystem path could not be accessed",
        ),
        "SYMLINK_DISALLOWED": (
            ContractErrorCode.PRECONDITION_FAILED,
            "SYMLINK_DISALLOWED",
            "symlink traversal is not allowed",
        ),
        "NOT_DIRECTORY": (
            ContractErrorCode.PRECONDITION_FAILED,
            "PATH_NOT_DIRECTORY",
            "search root does not identify a directory",
        ),
        "NOT_REGULAR_FILE": (
            ContractErrorCode.STATE_CHANGED,
            "LOCAL_STATE_CHANGED",
            "file type changed during search",
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


def _strict_name(name: str) -> bool:
    try:
        name.encode("utf-8", errors="strict")
        return True
    except UnicodeEncodeError:
        return False


def _truncate_line(line: str) -> tuple[str, bool]:
    raw = line.encode("utf-8", errors="strict")
    if len(raw) <= LINE_TEXT_MAX_BYTES:
        return line, False
    return raw[:LINE_TEXT_MAX_BYTES].decode("utf-8", errors="ignore"), True


def _serialized_size(payload: dict[str, object]) -> int:
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    )


def _payload(
    results: list[FsSearchResultItem],
    *,
    truncated: bool,
    limit_reason: str | None,
    files_scanned: int,
    bytes_scanned: int,
    skipped_invalid_utf8: int,
    skipped_nul: int,
    skipped_symlinks: int,
    next_cursor: str | None,
    continuation_receipt: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "results": [item.model_dump() for item in results],
        "truncated": truncated,
        "limit_reason": limit_reason,
        "files_scanned": files_scanned,
        "bytes_scanned": bytes_scanned,
        "skipped_invalid_utf8": skipped_invalid_utf8,
        "skipped_nul": skipped_nul,
        "skipped_symlinks": skipped_symlinks,
        "next_cursor": next_cursor,
        "continuation_receipt": continuation_receipt,
    }


def _read_complete(
    file_fd: int,
    remaining: int,
    deadline: float,
) -> tuple[bytes | None, int, str | None]:
    if time.monotonic() >= deadline:
        return None, 0, "deadline"
    observed = os.fstat(file_fd)
    if observed.st_size > remaining:
        return None, 0, "max_bytes"
    output = bytearray()
    while True:
        if time.monotonic() >= deadline:
            return None, len(output), "deadline"
        available = remaining - len(output)
        if available <= 0:
            current = os.fstat(file_fd)
            if current.st_size > len(output):
                return None, len(output), "max_bytes"
            return bytes(output), len(output), None
        try:
            chunk = os.read(file_fd, min(_READ_CHUNK_BYTES, available))
        except OSError as exc:
            raise FsSafetyError("IO_ERROR") from exc
        if not chunk:
            return bytes(output), len(output), None
        output.extend(chunk)


def search_files(
    cwd: str,
    query: str,
    mode: str,
    root_path: str = ".",
    case_sensitive: bool = True,
    max_results: int = 100,
    cursor: str | None = None,
    continuation_receipt: ContinuationReceiptResult | None = None,
) -> FsSearchResult:
    """Search regular files with literal matching and bounded deterministic traversal."""

    if type(query) is not str or not query:
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_QUERY",
            "query must be a non-empty string",
        )
    try:
        query.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_QUERY_UTF8",
            "query must be valid UTF-8",
        ) from exc
    if len(query) > 4096:
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "QUERY_TOO_LONG",
            "query must contain at most 4096 characters",
        )
    if mode not in {"content", "path"}:
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_MODE",
            "mode must be 'content' or 'path'",
        )
    if type(case_sensitive) is not bool:
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_CASE_SENSITIVE",
            "case_sensitive must be boolean",
        )
    if type(max_results) is not int or not 1 <= max_results <= MAX_RESULTS:
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_MAX_RESULTS",
            "max_results must be an integer from 1 through 500",
        )
    try:
        root_components = split_descendant(root_path, allow_dot=True)
    except ValueError as exc:
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "INVALID_ROOT_PATH",
            "root_path must be '.' or a normalized cwd-relative descendant directory",
        ) from exc
    if (cursor is None) != (continuation_receipt is None):
        raise CapabilityFailure(
            ContractErrorCode.INVALID_ARGUMENT,
            "CONTINUATION_PAIR_REQUIRED",
            "cursor and continuation_receipt must be provided together",
        )

    normalized_root = "/".join(root_components) if root_components else "."
    semantic_parameters = {
        "query": query,
        "mode": mode,
        "root_path": normalized_root,
        "case_sensitive": case_sensitive,
        "max_results": max_results,
    }
    offset = 0
    if continuation_receipt is not None:
        if continuation_receipt.kind != "fs-search":
            raise CapabilityFailure(
                ContractErrorCode.INVALID_ARGUMENT,
                "CONTINUATION_RECEIPT_KIND_MISMATCH",
                "continuation receipt belongs to another tool",
            )
        try:
            offset = parse_continuation_cursor(
                cursor or "",
                tool="fs_search",
                semantic_parameters=semantic_parameters,
                receipt_digest=continuation_receipt.digest,
            )
        except ContinuationFailure as exc:
            raise CapabilityFailure(
                ContractErrorCode.PRECONDITION_FAILED
                if exc.reason_code == "CONTINUATION_EXPIRED"
                else ContractErrorCode.INVALID_ARGUMENT,
                exc.reason_code,
                exc.message,
            ) from None

    started = time.monotonic()
    deadline = started + CALL_DEADLINE_SECONDS
    results: list[FsSearchResultItem] = []
    files_scanned = 0
    bytes_scanned = 0
    skipped_invalid_utf8 = 0
    skipped_nul = 0
    skipped_symlinks = 0
    truncated = False
    limit_reason: str | None = None
    match_count = 0
    resume_validated = offset == 0
    state_hasher = hashlib.sha256()
    boundary_digest: bytes | None = None
    last_result_digest: bytes | None = None
    more_results = False

    def stop(reason: str) -> None:
        nonlocal truncated, limit_reason
        truncated = True
        limit_reason = reason

    def observe_state(value: object) -> None:
        state_hasher.update(frame_bytes(canonical_structured_bytes(value)))

    try:
        _checked_cwd, cwd_fd = open_validated_cwd(cwd)
    except RuntimeValidationError as exc:
        raise _runtime_failure(exc) from None
    except FsSafetyError as exc:
        raise _safety_failure(exc) from None

    def receipt_for(observed_digest: bytes):
        return make_receipt_v1(
            kind="fs-search",
            subject={"cwd": str(_checked_cwd), "root_path": normalized_root},
            semantic_parameters=semantic_parameters,
            observed_state_bytes=observed_digest,
        )

    def validate_resume_boundary() -> None:
        nonlocal resume_validated
        if continuation_receipt is None or resume_validated or match_count != offset:
            return
        replayed = receipt_for(state_hasher.digest())
        if replayed.digest != continuation_receipt.digest:
            raise CapabilityFailure(
                ContractErrorCode.STATE_CHANGED,
                "CONTINUATION_STATE_CHANGED",
                "search traversal state changed before the continuation boundary",
            )
        resume_validated = True

    dummy_receipt = {
        "schema_version": 1,
        "kind": "fs-search",
        "digest": "0" * 64,
    }

    def candidate_fits(candidate: list[FsSearchResultItem]) -> bool:
        projected = _payload(
            candidate,
            truncated=True,
            limit_reason="max_results",
            files_scanned=files_scanned,
            bytes_scanned=bytes_scanned,
            skipped_invalid_utf8=skipped_invalid_utf8,
            skipped_nul=skipped_nul,
            skipped_symlinks=skipped_symlinks,
            next_cursor="x" * 1024,
            continuation_receipt=dummy_receipt,
        )
        return _serialized_size(projected) <= MAX_SERIALIZED_RESULT_BYTES

    try:
        if ".git" in root_components:
            observe_state({"root": normalized_root, "excluded_git_root": True})
        else:
            try:
                root_fd = open_directory_at(cwd_fd, root_components)
            except FsSafetyError as exc:
                raise _safety_failure(exc) from None
            os.close(root_fd)

            heap: list[tuple[str, int, tuple[str, ...]]] = []
            root_key = "/".join(root_components)
            heapq.heappush(heap, ((root_key + "/") if root_key else "", 0, root_components))
            needle = query if case_sensitive else query.casefold()

            while heap and limit_reason is None:
                if time.monotonic() >= deadline:
                    stop("deadline")
                    break
                _sort_key, kind, components = heapq.heappop(heap)

                if kind == 0:
                    try:
                        directory_fd = open_directory_at(cwd_fd, components)
                    except FsSafetyError as exc:
                        raise _safety_failure(exc) from None
                    directory_state: list[dict[str, object]] = []
                    invalid_before = skipped_invalid_utf8
                    try:
                        with os.scandir(directory_fd) as iterator:
                            for entry in iterator:
                                if time.monotonic() >= deadline:
                                    stop("deadline")
                                    break
                                name = entry.name
                                if not _strict_name(name):
                                    skipped_invalid_utf8 += 1
                                    continue
                                try:
                                    observed = entry.stat(follow_symlinks=False)
                                except PermissionError:
                                    raise CapabilityFailure(
                                        ContractErrorCode.PERMISSION_DENIED,
                                        "ENTRY_STAT_DENIED",
                                        "filesystem entry metadata could not be accessed",
                                    ) from None
                                except OSError:
                                    raise CapabilityFailure(
                                        ContractErrorCode.STATE_CHANGED,
                                        "LOCAL_STATE_CHANGED",
                                        "filesystem state changed during search",
                                    ) from None
                                child = components + (name,)
                                relative = "/".join(child)
                                directory_state.append(
                                    {
                                        "name": name,
                                        "mode": observed.st_mode,
                                        "size": observed.st_size,
                                        "mtime_ns": observed.st_mtime_ns,
                                    }
                                )
                                if stat.S_ISLNK(observed.st_mode):
                                    skipped_symlinks += 1
                                    continue
                                if stat.S_ISDIR(observed.st_mode):
                                    if name == ".git":
                                        continue
                                    heapq.heappush(heap, (relative + "/", 0, child))
                                elif stat.S_ISREG(observed.st_mode):
                                    heapq.heappush(heap, (relative, 1, child))
                    finally:
                        os.close(directory_fd)
                    directory_state.sort(key=lambda value: str(value["name"]))
                    observe_state(
                        {
                            "directory": "/".join(components) if components else ".",
                            "entries": directory_state,
                            "invalid_names": skipped_invalid_utf8 - invalid_before,
                        }
                    )
                    continue

                if files_scanned >= MAX_FILES_SCANNED:
                    stop("max_files")
                    break
                files_scanned += 1
                relative = "/".join(components)

                if mode == "path":
                    haystack = relative if case_sensitive else relative.casefold()
                    if needle not in haystack:
                        continue
                    match_count += 1
                    validate_resume_boundary()
                    if match_count <= offset:
                        continue
                    if boundary_digest is not None:
                        more_results = True
                        stop("max_results")
                        break
                    item = FsSearchResultItem(
                        path=relative,
                        line_number=None,
                        line_text=None,
                        line_truncated=False,
                        file_sha256=None,
                    )
                    candidate = results + [item]
                    if not candidate_fits(candidate):
                        if not results or last_result_digest is None:
                            stop("max_output")
                            break
                        boundary_digest = last_result_digest
                        more_results = True
                        stop("max_output")
                        break
                    results.append(item)
                    last_result_digest = state_hasher.digest()
                    if len(results) >= max_results:
                        boundary_digest = last_result_digest
                    continue

                remaining = MAX_CONTENT_BYTES_SCANNED - bytes_scanned
                if remaining <= 0:
                    stop("max_bytes")
                    break
                try:
                    file_fd, _observed = open_regular_at(cwd_fd, components)
                except FsSafetyError as exc:
                    raise _safety_failure(exc) from None
                try:
                    raw, consumed, read_limit = _read_complete(file_fd, remaining, deadline)
                except FsSafetyError as exc:
                    raise _safety_failure(exc) from None
                finally:
                    os.close(file_fd)
                bytes_scanned += consumed
                if raw is None:
                    stop(read_limit or "max_bytes")
                    break
                digest = hashlib.sha256(raw).hexdigest()
                observe_state({"file": relative, "bytes": len(raw), "sha256": digest})
                if b"\x00" in raw:
                    skipped_nul += 1
                    continue
                try:
                    text_value = raw.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    skipped_invalid_utf8 += 1
                    continue
                for line_number, line in enumerate(text_value.splitlines(), start=1):
                    if time.monotonic() >= deadline:
                        stop("deadline")
                        break
                    line_haystack = line if case_sensitive else line.casefold()
                    if needle not in line_haystack:
                        continue
                    match_count += 1
                    validate_resume_boundary()
                    if match_count <= offset:
                        continue
                    if boundary_digest is not None:
                        more_results = True
                        stop("max_results")
                        break
                    line_text, line_truncated = _truncate_line(line)
                    item = FsSearchResultItem(
                        path=relative,
                        line_number=line_number,
                        line_text=line_text,
                        line_truncated=line_truncated,
                        file_sha256=digest,
                    )
                    candidate = results + [item]
                    if not candidate_fits(candidate):
                        if not results or last_result_digest is None:
                            stop("max_output")
                            break
                        boundary_digest = last_result_digest
                        more_results = True
                        stop("max_output")
                        break
                    results.append(item)
                    last_result_digest = state_hasher.digest()
                    if len(results) >= max_results:
                        boundary_digest = last_result_digest
                if limit_reason is not None:
                    break

        if continuation_receipt is not None and not resume_validated:
            raise CapabilityFailure(
                ContractErrorCode.STATE_CHANGED,
                "CONTINUATION_STATE_CHANGED",
                "search traversal no longer reaches the issued continuation boundary",
            )

        resumable = more_results and boundary_digest is not None and limit_reason in {"max_results", "max_output"}
        receipt_digest_bytes = boundary_digest if resumable else state_hasher.digest()
        receipt = receipt_for(receipt_digest_bytes)
        receipt_result = ContinuationReceiptResult(
            schema_version=1,
            kind="fs-search",
            digest=receipt.digest,
        )
        next_cursor = (
            make_continuation_cursor(
                tool="fs_search",
                position=offset + len(results),
                semantic_parameters=semantic_parameters,
                receipt_digest=receipt.digest,
            )
            if resumable
            else None
        )
        payload = _payload(
            results,
            truncated=truncated,
            limit_reason=limit_reason,
            files_scanned=files_scanned,
            bytes_scanned=bytes_scanned,
            skipped_invalid_utf8=skipped_invalid_utf8,
            skipped_nul=skipped_nul,
            skipped_symlinks=skipped_symlinks,
            next_cursor=next_cursor,
            continuation_receipt=receipt_result.model_dump(),
        )
        if _serialized_size(payload) > MAX_SERIALIZED_RESULT_BYTES:
            raise CapabilityFailure(
                ContractErrorCode.LIMIT_EXCEEDED,
                "RESULT_METADATA_LIMIT",
                "search result metadata exceeds the serialized output bound",
            )
        return FsSearchResult(**payload)
    finally:
        os.close(cwd_fd)
