from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import subprocess
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import ValidationError

from .contracts import ScreenCaptureMetadata, ScreenCaptureTarget
from .tool_contract import Authority, MutationAuthority, NetworkAuthority, ToolAnnotations, ToolClass, ToolContract

DEADLINE_SECONDS = 5.0
MAX_PNG_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 16 * 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_STDOUT_BYTES = MAX_HEADER_BYTES + 1 + MAX_PNG_BYTES
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

SCREEN_CAPTURE_CONTRACT = ToolContract(
    name="screen_capture",
    tool_class=ToolClass.HOST,
    authority=Authority(False, NetworkAuthority.NONE, MutationAuthority.NONE),
    annotations=ToolAnnotations(True, False, True, False),
    preconditions={"production_policy": "VISUAL_PERCEPTION_BLOCKED"},
    bounds={"error_metadata_bytes": MAX_HEADER_BYTES},
    postconditions={"native_capture_invoked": False, "permission_requested": False, "image_produced": False},
)

_SCREEN_ERROR_CODES = frozenset(
    {
        "INVALID_ARGUMENT",
        "SCREEN_CAPTURE_PERMISSION_REQUIRED",
        "CAPTURE_TARGET_NOT_FOUND",
        "CAPTURE_TARGET_AMBIGUOUS",
        "CAPTURE_PAYLOAD_TOO_LARGE",
        "CAPTURE_PROTOCOL_ERROR",
        "CAPTURE_HELPER_UNAVAILABLE",
        "DEADLINE_EXCEEDED",
        "INTERNAL_ERROR",
        "VISUAL_PERCEPTION_BLOCKED",
    }
)


class ScreenCaptureFailure(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        clean = " ".join(str(message).split())[:256] or "screen capture failed"
        super().__init__(clean)
        self.code = code if code in _SCREEN_ERROR_CODES else "INTERNAL_ERROR"
        self.message = clean
        self.retryable = bool(retryable)


def _bounded_json_text(value: dict[str, Any], *, overflow_message: str) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ScreenCaptureFailure("CAPTURE_PROTOCOL_ERROR", "screen capture metadata is not serializable") from exc
    if len(text.encode("utf-8")) > MAX_HEADER_BYTES:
        raise ScreenCaptureFailure("CAPTURE_PROTOCOL_ERROR", overflow_message)
    return text


def _metadata_text(metadata: ScreenCaptureMetadata) -> str:
    return _bounded_json_text(
        metadata.model_dump(mode="json"),
        overflow_message="screen capture metadata exceeded bounds",
    )


def capture_failure_result(failure: ScreenCaptureFailure) -> CallToolResult:
    # Compatibility helper: keep one public failure/effect translator.
    from .server import _runtime_error_from_exception

    return _runtime_error_from_exception("screen_capture", failure)

def _fixed_helper_path() -> Path:
    source = Path(__file__).resolve()
    runtime_root = source.parent.parent
    if (
        runtime_root.name == "runtime"
        and runtime_root.parent.name == "Resources"
        and runtime_root.parent.parent.name == "Contents"
    ):
        return runtime_root.parent.parent / "MacOS" / "AgentRuntimeScreenCapture"
    return runtime_root / ".package-owned-helper-unavailable" / "AgentRuntimeScreenCapture"


def _validate_request(
    target: ScreenCaptureTarget,
    window_id: int | None,
    application_bundle_id: str | None,
    display_id: int | None,
    x: float | None,
    y: float | None,
    width: float | None,
    height: float | None,
) -> None:
    selectors = {
        "window_id": window_id,
        "application_bundle_id": application_bundle_id,
        "display_id": display_id,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }
    present = {name for name, value in selectors.items() if value is not None}
    expected = {
        "frontmost_window": set(),
        "window": {"window_id"},
        "application_window": {"application_bundle_id"},
        "display": {"display_id"},
        "region": {"display_id", "x", "y", "width", "height"},
    }[target]
    if present != expected:
        required = ", ".join(sorted(expected)) or "no selector fields"
        raise ScreenCaptureFailure("INVALID_ARGUMENT", f"{target} requires exactly {required}")
    if target == "region":
        values = (x, y, width, height)
        if not all(isinstance(v, (int, float)) and math.isfinite(float(v)) for v in values):
            raise ScreenCaptureFailure("INVALID_ARGUMENT", "region geometry must be finite")
        if float(width) <= 0 or float(height) <= 0:
            raise ScreenCaptureFailure("INVALID_ARGUMENT", "region width and height must be positive")

def _helper_argv(
    helper: Path,
    target: ScreenCaptureTarget,
    window_id: int | None,
    application_bundle_id: str | None,
    display_id: int | None,
    x: float | None,
    y: float | None,
    width: float | None,
    height: float | None,
) -> list[str]:
    argv = [str(helper), "--target", target]
    if window_id is not None:
        argv += ["--window-id", str(window_id)]
    if application_bundle_id is not None:
        argv += ["--application-bundle-id", application_bundle_id]
    if display_id is not None:
        argv += ["--display-id", str(display_id)]
    if x is not None:
        argv += ["--x", repr(float(x))]
    if y is not None:
        argv += ["--y", repr(float(y))]
    if width is not None:
        argv += ["--width", repr(float(width))]
    if height is not None:
        argv += ["--height", repr(float(height))]
    return argv


def _invoke_helper(argv: list[str]) -> tuple[int, bytes]:
    helper = Path(argv[0])
    try:
        stat = helper.lstat()
    except OSError as exc:
        raise ScreenCaptureFailure(
            "CAPTURE_HELPER_UNAVAILABLE",
            "package-owned screen capture helper is unavailable",
        ) from exc
    if helper.is_symlink() or not helper.is_file() or not os.access(helper, os.X_OK) or stat.st_size <= 0:
        raise ScreenCaptureFailure(
            "CAPTURE_HELPER_UNAVAILABLE",
            "package-owned screen capture helper is invalid",
        )
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=DEADLINE_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScreenCaptureFailure(
            "DEADLINE_EXCEEDED",
            "screen capture helper exceeded its fixed deadline",
            True,
        ) from exc
    except OSError as exc:
        raise ScreenCaptureFailure(
            "CAPTURE_HELPER_UNAVAILABLE",
            "package-owned screen capture helper could not start",
        ) from exc
    if len(completed.stdout) > MAX_STDOUT_BYTES:
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "screen capture helper output exceeded bounds",
        )
    return completed.returncode, completed.stdout

def _parse_error_header(header: dict[str, Any], payload: bytes) -> None:
    if set(header) != {"status", "error_code", "message", "retryable"} or payload:
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "invalid screen capture error framing",
        )
    code = header.get("error_code")
    message = header.get("message")
    retryable = header.get("retryable")
    if (
        header.get("status") != "error"
        or code not in _SCREEN_ERROR_CODES
        or not isinstance(message, str)
        or not isinstance(retryable, bool)
    ):
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "invalid screen capture error header",
        )
    raise ScreenCaptureFailure(code, message, retryable)


def _validate_coordinate_invariants(metadata: ScreenCaptureMetadata) -> None:
    bounds = metadata.bounds
    values = (bounds.x, bounds.y, bounds.width, bounds.height, metadata.scale_factor)
    if not all(math.isfinite(float(value)) for value in values):
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "capture metadata contains non-finite geometry",
        )
    if bounds.width <= 0 or bounds.height <= 0 or metadata.scale_factor <= 0:
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "capture metadata contains invalid geometry",
        )
    if metadata.pixel_width <= 0 or metadata.pixel_height <= 0:
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "capture metadata contains invalid pixel size",
        )
    if metadata.pixel_width * metadata.pixel_height > MAX_PIXELS:
        raise ScreenCaptureFailure(
            "CAPTURE_PAYLOAD_TOO_LARGE",
            "capture exceeds pixel limit",
        )
    expected_width = bounds.width * metadata.scale_factor
    expected_height = bounds.height * metadata.scale_factor
    if abs(metadata.pixel_width - expected_width) > 1.0:
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "capture width mapping is inconsistent",
        )
    if abs(metadata.pixel_height - expected_height) > 1.0:
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "capture height mapping is inconsistent",
        )
    if metadata.deadline_seconds != DEADLINE_SECONDS:
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "capture deadline metadata is inconsistent",
        )


def _parse_helper_output(
    returncode: int,
    stdout: bytes,
) -> tuple[ScreenCaptureMetadata, bytes]:
    separator = stdout.find(b"\n")
    if separator < 0 or separator > MAX_HEADER_BYTES:
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "missing bounded screen capture header",
        )
    header_bytes = stdout[:separator]
    payload = stdout[separator + 1 :]
    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "invalid screen capture JSON header",
        ) from exc
    if not isinstance(header, dict):
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "screen capture header must be an object",
        )
    if header.get("status") == "error":
        _parse_error_header(header, payload)
    if returncode != 0:
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "screen capture helper exited unsuccessfully",
        )
    try:
        metadata = ScreenCaptureMetadata.model_validate(header)
    except ValidationError as exc:
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "invalid screen capture metadata",
        ) from exc
    if metadata.raw_bytes != len(payload) or metadata.raw_bytes > MAX_PNG_BYTES:
        code = (
            "CAPTURE_PAYLOAD_TOO_LARGE"
            if len(payload) > MAX_PNG_BYTES
            else "CAPTURE_PROTOCOL_ERROR"
        )
        raise ScreenCaptureFailure(code, "screen capture payload length is invalid")
    if not payload.startswith(PNG_SIGNATURE):
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "screen capture payload is not PNG",
        )
    digest = hashlib.sha256(payload).hexdigest()
    if metadata.sha256 != digest:
        raise ScreenCaptureFailure(
            "CAPTURE_PROTOCOL_ERROR",
            "screen capture SHA-256 mismatch",
        )
    _validate_coordinate_invariants(metadata)
    return metadata, payload


def capture_screen(
    target: ScreenCaptureTarget,
    window_id: int | None,
    application_bundle_id: str | None,
    display_id: int | None,
    x: float | None,
    y: float | None,
    width: float | None,
    height: float | None,
) -> CallToolResult:
    _validate_request(
        target,
        window_id,
        application_bundle_id,
        display_id,
        x,
        y,
        width,
        height,
    )
    helper = _fixed_helper_path()
    argv = _helper_argv(
        helper,
        target,
        window_id,
        application_bundle_id,
        display_id,
        x,
        y,
        width,
        height,
    )
    returncode, stdout = _invoke_helper(argv)
    metadata, png = _parse_helper_output(returncode, stdout)
    return CallToolResult(
        content=[
            ImageContent(
                type="image",
                data=base64.b64encode(png).decode("ascii"),
                mimeType="image/png",
            ),
            TextContent(type="text", text=_metadata_text(metadata)),
        ],
    )
