from __future__ import annotations

from dataclasses import dataclass

from .capacity import CAPACITY_OBSERVER_CONTRACT, heavy_execution_admission
from .contracts import (
    CapabilityAnnotations,
    CapabilityAuthority,
    CapabilityDescriptor,
    CapabilityLifecycle,
    RuntimeCapabilitiesResult,
)
from .errors import RuntimeValidationError
from .executor import TERMINAL_EXEC_CONTRACT
from .fs_list import FS_LIST_CONTRACT
from .fs_manage import FS_MANAGE_CONTRACT
from .fs_patch import FS_PATCH_CONTRACT
from .fs_read import FS_READ_BATCH_CONTRACT
from .fs_search import FS_SEARCH_CONTRACT
from .fs_write import FS_WRITE_CONTRACT
from .repo_commit import REPO_COMMIT_CONTRACT
from .repo_diff import REPO_DIFF_CONTRACT
from .repo_fast_forward import REPO_FAST_FORWARD_CONTRACT
from .repo_observer import REPO_OBSERVER_CONTRACT
from .repo_remote_observer import REPO_REMOTE_OBSERVER_CONTRACT
from .repo_publish import REPO_PUBLISH_CONTRACT
from .repo_stage import REPO_STAGE_CONTRACT
from .screen_capture import SCREEN_CAPTURE_CONTRACT
from .session import (
    MAX_WAIT_MS,
    RUNNING_HARD_WALL_SECONDS,
    TERMINAL_CONTROL_CONTRACT,
    TERMINAL_POLL_CONTRACT,
    TERMINAL_RESIZE_CONTRACT,
    TERMINAL_START_CONTRACT,
    configured_session_limit,
)
from .tool_contract import (
    Authority,
    MutationAuthority,
    NetworkAuthority,
    ToolAnnotations,
    ToolClass,
    ToolContract,
)
from .version import RUNTIME_VERSION

TOOL_CONTRACT_KERNEL_VERSION = 2
DESCRIPTOR_SCHEMA_VERSION = 2
TOOL_CONTRACT_VERSION = 1
REQUEST_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
RUNTIME_REVISION: str | None = None

RUNTIME_CAPABILITIES_CONTRACT = ToolContract(
    name="runtime_capabilities",
    tool_class=ToolClass.READ,
    authority=Authority(
        workspace_bound=False,
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
        "detail": "summary-default-or-full",
        "names": "omitted-or-nonempty-unique-known-names-with-full-detail",
        "inventory": "static-runtime-registry",
    },
    bounds={
        "capabilities": 21,
        "advertised_tools": 20,
        "names": 21,
        "summary_utf8_bytes": 1024,
    },
    postconditions={
        "network_used": False,
        "host_probes": False,
        "repository_probes": False,
        "permission_probes": False,
        "mutation": False,
        "ordering": "registry-order",
    },
)


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    contract: ToolContract
    lifecycle: CapabilityLifecycle = "stable"
    request_schema_version: int = REQUEST_SCHEMA_VERSION
    result_schema_version: int | None = RESULT_SCHEMA_VERSION
    supported: bool = True
    available: bool = True
    advertised: bool = True
    unavailable_reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.request_schema_version not in {1, 2, 3, 4}:
            raise ValueError("request_schema_version must be 1, 2, 3, or 4")
        if self.result_schema_version not in {1, 2, 3, None}:
            raise ValueError("result_schema_version must be 1, 2, 3, or null")
        if self.available and self.unavailable_reason_code is not None:
            raise ValueError("available capability must not have unavailable_reason_code")
        if not self.available and not self.unavailable_reason_code:
            raise ValueError("unavailable capability requires unavailable_reason_code")


CAPABILITY_REGISTRY = (
    CapabilityBinding(TERMINAL_EXEC_CONTRACT, request_schema_version=2, result_schema_version=2),
    CapabilityBinding(TERMINAL_START_CONTRACT, request_schema_version=3, result_schema_version=3),
    CapabilityBinding(TERMINAL_POLL_CONTRACT, request_schema_version=4, result_schema_version=3),
    CapabilityBinding(TERMINAL_CONTROL_CONTRACT),
    CapabilityBinding(TERMINAL_RESIZE_CONTRACT),
    CapabilityBinding(CAPACITY_OBSERVER_CONTRACT, result_schema_version=2),
    CapabilityBinding(FS_READ_BATCH_CONTRACT, result_schema_version=2),
    CapabilityBinding(FS_LIST_CONTRACT, request_schema_version=2, result_schema_version=2),
    CapabilityBinding(FS_SEARCH_CONTRACT, request_schema_version=2, result_schema_version=2),
    CapabilityBinding(FS_PATCH_CONTRACT),
    CapabilityBinding(FS_WRITE_CONTRACT),
    CapabilityBinding(FS_MANAGE_CONTRACT),
    CapabilityBinding(REPO_OBSERVER_CONTRACT, request_schema_version=2, result_schema_version=2),
    CapabilityBinding(REPO_REMOTE_OBSERVER_CONTRACT),
    CapabilityBinding(REPO_DIFF_CONTRACT, request_schema_version=2, result_schema_version=2),
    CapabilityBinding(REPO_STAGE_CONTRACT),
    CapabilityBinding(REPO_COMMIT_CONTRACT),
    CapabilityBinding(REPO_FAST_FORWARD_CONTRACT),
    CapabilityBinding(REPO_PUBLISH_CONTRACT),
    CapabilityBinding(
        SCREEN_CAPTURE_CONTRACT,
        result_schema_version=None,
        available=False,
        advertised=False,
        unavailable_reason_code="VISUAL_PERCEPTION_BLOCKED",
    ),
    CapabilityBinding(
        RUNTIME_CAPABILITIES_CONTRACT,
        request_schema_version=2,
        result_schema_version=2,
    ),
)

CAPABILITY_NAMES = tuple(binding.contract.name for binding in CAPABILITY_REGISTRY)
ADVERTISED_TOOL_NAMES = tuple(
    binding.contract.name for binding in CAPABILITY_REGISTRY if binding.advertised
)

if len(CAPABILITY_NAMES) != len(set(CAPABILITY_NAMES)):
    raise RuntimeError("capability registry contains duplicate names")
if len(ADVERTISED_TOOL_NAMES) != len(set(ADVERTISED_TOOL_NAMES)):
    raise RuntimeError("advertised capability registry contains duplicate names")


def descriptor_for(binding: CapabilityBinding) -> CapabilityDescriptor:
    contract = binding.contract
    if not isinstance(contract.bounds, dict):
        raise TypeError(f"{contract.name} ToolContract bounds must be an object")
    return CapabilityDescriptor(
        schema_version=DESCRIPTOR_SCHEMA_VERSION,
        runtime_version=RUNTIME_VERSION,
        tool_contract_kernel_version=TOOL_CONTRACT_KERNEL_VERSION,
        name=contract.name,
        tool_contract_version=TOOL_CONTRACT_VERSION,
        lifecycle=binding.lifecycle,
        authority=CapabilityAuthority(
            workspace_bound=contract.authority.workspace_bound,
            network=contract.authority.network.value,
            mutation=contract.authority.mutation.value,
        ),
        annotations=CapabilityAnnotations(
            read_only=contract.annotations.read_only,
            destructive=contract.annotations.destructive,
            idempotent=contract.annotations.idempotent,
            open_world=contract.annotations.open_world,
        ),
        request_schema_version=binding.request_schema_version,
        result_schema_version=binding.result_schema_version,
        bounds=dict(contract.bounds),
        supported=binding.supported,
        available=binding.available,
        advertised=binding.advertised,
        unavailable_reason_code=binding.unavailable_reason_code,
    )


def capability_descriptors() -> tuple[CapabilityDescriptor, ...]:
    return tuple(descriptor_for(binding) for binding in CAPABILITY_REGISTRY)


def _runtime_capabilities_common() -> dict[str, object]:
    descriptors = capability_descriptors()
    available_count = sum(descriptor.available for descriptor in descriptors)
    return {
        "schema_version": 2,
        "runtime_version": RUNTIME_VERSION,
        "runtime_revision": RUNTIME_REVISION,
        "tool_contract_kernel_version": TOOL_CONTRACT_KERNEL_VERSION,
        "advertised_tool_count": len(ADVERTISED_TOOL_NAMES),
        "capability_count": len(descriptors),
        "available_count": available_count,
        "unavailable_count": len(descriptors) - available_count,
        "execution": {
            "heavy_ceiling": heavy_execution_admission().limit,
            "active_session_ceiling": configured_session_limit(),
            "terminal_poll_max_wait_ms": MAX_WAIT_MS,
            "running_hard_wall_ms": int(RUNNING_HARD_WALL_SECONDS * 1000),
        },
    }


def runtime_capabilities_result(
    detail: str = "summary",
    names: list[str] | None = None,
) -> RuntimeCapabilitiesResult:
    if detail not in {"summary", "full"}:
        raise RuntimeValidationError("detail must be summary or full")
    if names is not None:
        if detail != "full":
            raise RuntimeValidationError("names requires detail=full")
        if not names:
            raise RuntimeValidationError("names must not be empty")
        if len(names) != len(set(names)):
            raise RuntimeValidationError("names must not contain duplicates")
        unknown = [name for name in names if name not in CAPABILITY_NAMES]
        if unknown:
            raise RuntimeValidationError("names contains an unknown capability")

    common = _runtime_capabilities_common()
    if detail == "summary":
        return RuntimeCapabilitiesResult(detail="summary", **common)

    descriptors = capability_descriptors()
    if names is not None:
        selected = set(names)
        descriptors = tuple(
            descriptor for descriptor in descriptors if descriptor.name in selected
        )
    return RuntimeCapabilitiesResult(
        detail="full",
        capabilities=list(descriptors),
        **common,
    )


def tool_contract_projection(contract: ToolContract) -> dict[str, object]:
    return {
        "name": contract.name,
        "tool_class": contract.tool_class.value,
        "authority": {
            "workspace_bound": contract.authority.workspace_bound,
            "network": contract.authority.network.value,
            "mutation": contract.authority.mutation.value,
        },
        "annotations": {
            "read_only": contract.annotations.read_only,
            "destructive": contract.annotations.destructive,
            "idempotent": contract.annotations.idempotent,
            "open_world": contract.annotations.open_world,
        },
        "preconditions": contract.preconditions,
        "bounds": contract.bounds,
        "postconditions": contract.postconditions,
    }
