from __future__ import annotations

import asyncio
import hashlib
import math
import sys

from . import server
from .capability_registry import (
    CAPABILITY_NAMES,
    CAPABILITY_REGISTRY,
    TOOL_CONTRACT_KERNEL_VERSION,
    descriptor_for,
    tool_contract_projection,
)
from .tool_contract import canonical_structured_bytes
from .version import RUNTIME_VERSION

SCHEMA_EXPORT_VERSION = 1


def _canonical_schema_value(value: object) -> object:
    value_type = type(value)
    if value is None or value_type in {bool, int, str}:
        return value
    if value_type is float:
        if not math.isfinite(value) or not value.is_integer():
            raise TypeError("registered schema contains a non-integral JSON number")
        return int(value)
    if value_type is list:
        return [_canonical_schema_value(item) for item in value]
    if value_type is dict:
        return {
            str(key): _canonical_schema_value(item)
            for key, item in value.items()
        }
    raise TypeError(f"registered schema contains unsupported value type: {value_type.__name__}")


async def build_schema_bundle() -> dict[str, object]:
    tools = await server.mcp.list_tools()
    names = tuple(tool.name for tool in tools)
    if names != CAPABILITY_NAMES:
        raise RuntimeError("registered MCP tools do not match canonical capability registry")

    entries: list[dict[str, object]] = []
    for binding, tool in zip(CAPABILITY_REGISTRY, tools, strict=True):
        request_schema = _canonical_schema_value(tool.input_schema)
        result_schema = (
            None
            if tool.output_schema is None
            else _canonical_schema_value(tool.output_schema)
        )
        if (result_schema is None) != (binding.result_schema_version is None):
            raise RuntimeError(
                f"{binding.contract.name} result schema availability binding does not match registered MCP output schema"
            )
        entries.append(
            {
                "descriptor": descriptor_for(binding).model_dump(mode="json"),
                "tool_contract": tool_contract_projection(binding.contract),
                "request_schema": request_schema,
                "result_schema": result_schema,
            }
        )

    body: dict[str, object] = {
        "schema_version": SCHEMA_EXPORT_VERSION,
        "runtime_version": RUNTIME_VERSION,
        "tool_contract_kernel_version": TOOL_CONTRACT_KERNEL_VERSION,
        "capabilities": entries,
    }
    digest = hashlib.sha256(canonical_structured_bytes(body)).hexdigest()
    return {**body, "bundle_sha256": digest}


def export_schema_bytes() -> bytes:
    bundle = asyncio.run(build_schema_bundle())
    return canonical_structured_bytes(bundle) + b"\n"


def _main() -> int:
    sys.stdout.buffer.write(export_schema_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
