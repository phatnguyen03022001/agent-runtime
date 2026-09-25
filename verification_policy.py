from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Iterable

L0_STATIC = "L0_STATIC"
L1_DETERMINISTIC_UNIT = "L1_DETERMINISTIC_UNIT"
L2_ISOLATED_INTEGRATION = "L2_ISOLATED_INTEGRATION"
L3_DETERMINISTIC_REGRESSION = "L3_DETERMINISTIC_REGRESSION"
L4_HOST_LIFECYCLE = "L4_HOST_LIFECYCLE"
L5_QUALIFICATION_CHAOS_CUTOVER = "L5_QUALIFICATION_CHAOS_CUTOVER"

TEST_LANES = (
    L1_DETERMINISTIC_UNIT,
    L2_ISOLATED_INTEGRATION,
    L3_DETERMINISTIC_REGRESSION,
    L4_HOST_LIFECYCLE,
    L5_QUALIFICATION_CHAOS_CUTOVER,
)

LANE_WORKER_LIMITS = {
    L1_DETERMINISTIC_UNIT: 4,
    L2_ISOLATED_INTEGRATION: 4,
    L3_DETERMINISTIC_REGRESSION: 4,
    L4_HOST_LIFECYCLE: 1,
    L5_QUALIFICATION_CHAOS_CUTOVER: 1,
}

GLOBAL_WORKER_LIMIT = 4
HEARTBEAT_SECONDS = 5.0


@dataclass(frozen=True)
class ModulePolicy:
    lane: str
    subsystem_tags: tuple[str, ...]
    isolation_key: str | None
    proof_rationale: str
    timeout_seconds: float


@dataclass(frozen=True)
class PathRule:
    pattern: str
    subsystem_tags: tuple[str, ...]
    docs_only: bool = False
    force_qualification: bool = False
    reason: str = ""


def module_policy(
    lane: str,
    tags: tuple[str, ...],
    rationale: str,
    *,
    timeout: float = 30.0,
    isolation_key: str | None = None,
) -> ModulePolicy:
    return ModulePolicy(
        lane=lane,
        subsystem_tags=tuple(tags),
        isolation_key=isolation_key,
        proof_rationale=rationale,
        timeout_seconds=float(timeout),
    )


def build_policy_map(
    entries: Iterable[tuple[str, ModulePolicy]],
) -> dict[str, ModulePolicy]:
    result: dict[str, ModulePolicy] = {}
    for module, policy in entries:
        if module in result:
            raise ValueError(f"duplicate verification classification: {module}")
        if policy.lane not in TEST_LANES:
            raise ValueError(f"invalid verification lane for {module}: {policy.lane}")
        if not policy.subsystem_tags:
            raise ValueError(f"missing subsystem tags for {module}")
        if not policy.proof_rationale.strip():
            raise ValueError(f"missing proof rationale for {module}")
        if policy.timeout_seconds <= 0:
            raise ValueError(f"invalid module timeout for {module}: {policy.timeout_seconds}")
        result[module] = policy
    return result


MODULE_POLICY_ENTRIES: tuple[tuple[str, ModulePolicy], ...] = (
    ("tests.test_candidate_cutover", module_policy(
        L5_QUALIFICATION_CHAOS_CUTOVER,
        ("cutover", "package", "host_lifecycle"),
        "Transactional package cutover, rollback and crash-boundary qualification.",
        timeout=120.0,
        isolation_key="qualification",
    )),
    ("tests.test_candidate_freeze", module_policy(
        L5_QUALIFICATION_CHAOS_CUTOVER,
        ("package", "release"),
        "Candidate freeze and release-boundary qualification.",
        timeout=30.0,
        isolation_key="qualification",
    )),
    ("tests.test_capability_registry", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("contract", "capability"),
        "Pure capability-registry contract and inventory checks.",
    )),
    ("tests.test_capacity_observer", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("capacity",),
        "Deterministic capacity policy checks with controlled signal inputs.",
    )),
    ("tests.test_doctor", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("doctor", "contract"),
        "Doctor integration uses disposable local fixtures without host-global ownership.",
    )),
    ("tests.test_durable_pipe", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("durable", "terminal"),
        "Durable-state integration owns disposable filesystem/process fixtures.",
        timeout=45.0,
    )),
    ("tests.test_durable_pipe_restart", module_policy(
        L5_QUALIFICATION_CHAOS_CUTOVER,
        ("durable", "restart", "terminal", "lifecycle"),
        "Restart, lost-ack and durable owner-recovery qualification.",
        timeout=45.0,
        isolation_key="qualification",
    )),
    ("tests.test_failure_effect_contract", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("contract", "failure"),
        "Pure failure/effect contract semantics.",
    )),
    ("tests.test_fs_list", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("fs",),
        "Deterministic bounded filesystem listing over disposable roots.",
    )),
    ("tests.test_fs_manage", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("fs",),
        "Deterministic guarded filesystem-management semantics over disposable roots.",
    )),
    ("tests.test_fs_patch", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("fs",),
        "Deterministic patch and expected-state semantics.",
    )),
    ("tests.test_fs_read_batch", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("fs",),
        "Deterministic bounded batch-read semantics.",
    )),
    ("tests.test_fs_read_batch_adversarial", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("fs", "security"),
        "Deterministic adversarial read-boundary coverage.",
    )),
    ("tests.test_fs_read_batch_mcp", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("fs", "mcp"),
        "MCP filesystem read integration with isolated request fixtures.",
    )),
    ("tests.test_fs_search", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("fs",),
        "Deterministic bounded search semantics.",
    )),
    ("tests.test_fs_write", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("fs",),
        "Deterministic guarded write/CAS semantics.",
    )),
    ("tests.test_fs_write_mcp", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("fs", "mcp"),
        "MCP filesystem write integration with isolated request fixtures.",
    )),
    ("tests.test_image_transport", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("screen", "transport"),
        "Image transport integration owns its local helper and disposable payloads.",
        timeout=45.0,
    )),
    ("tests.test_mcp_client_conformance", module_policy(
        L3_DETERMINISTIC_REGRESSION,
        ("contract", "mcp"),
        "Cross-module client/server MCP compatibility regression.",
    )),
    ("tests.test_mcp_contracts", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("contract", "mcp"),
        "Deterministic MCP schema and contract checks.",
    )),
    ("tests.test_package_provenance", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("package",),
        "Package provenance integration uses disposable signed fixtures.",
        timeout=45.0,
    )),
    ("tests.test_packaging_interpreter", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("package",),
        "Packaging interpreter integration uses disposable package fixtures.",
        timeout=45.0,
    )),
    ("tests.test_pressure_concurrency", module_policy(
        L3_DETERMINISTIC_REGRESSION,
        ("capacity", "concurrency"),
        "Deterministic cross-component pressure/concurrency regression.",
    )),
    ("tests.test_protected_runtime", module_policy(
        L4_HOST_LIFECYCLE,
        ("host_lifecycle", "protection"),
        "Protected-runtime ownership and lifecycle boundary proof.",
        timeout=30.0,
        isolation_key="host-lifecycle",
    )),
    ("tests.test_repo_commit", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("repo",),
        "Real Git commit integration over unique disposable repositories.",
        timeout=45.0,
    )),
    ("tests.test_repo_diff", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("repo",),
        "Real Git diff integration over unique disposable repositories.",
        timeout=30.0,
    )),
    ("tests.test_repo_fast_forward", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("repo",),
        "Real Git fast-forward integration over unique disposable repositories.",
        timeout=45.0,
    )),
    ("tests.test_repo_fast_forward_mcp", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("repo", "mcp"),
        "MCP fast-forward integration with disposable repositories.",
    )),
    ("tests.test_repo_observer", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("repo",),
        "Real Git repository observation over unique disposable repositories.",
        timeout=30.0,
    )),
    ("tests.test_repo_observer_mcp", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("repo", "mcp"),
        "MCP repository observation with disposable repositories.",
    )),
    ("tests.test_repo_publish", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("repo",),
        "Real Git publication/CAS integration over disposable local remotes.",
        timeout=45.0,
    )),
    ("tests.test_repo_publish_mcp", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("repo", "mcp"),
        "MCP publication integration with disposable local remotes.",
    )),
    ("tests.test_repo_remote_observer", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("repo",),
        "Remote-observer integration uses disposable local remote fixtures.",
        timeout=30.0,
    )),
    ("tests.test_repo_stage", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("repo",),
        "Real Git staging integration over unique disposable repositories.",
        timeout=45.0,
    )),
    ("tests.test_repo_stage_commit_mcp", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("repo", "mcp"),
        "MCP stage/commit integration with disposable repositories.",
        timeout=30.0,
    )),
    ("tests.test_revision4_runtime_config", module_policy(
        L4_HOST_LIFECYCLE,
        ("host_lifecycle", "config"),
        "Runtime configuration/start lifecycle compatibility proof.",
        timeout=30.0,
        isolation_key="host-lifecycle",
    )),
    ("tests.test_runtime_config", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("config",),
        "Pure runtime configuration parsing and validation.",
    )),
    ("tests.test_runtime_label_split", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("contract", "config"),
        "Deterministic split-label identity contract.",
    )),
    ("tests.test_schema_export", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("contract", "schema"),
        "Deterministic schema-export contract.",
    )),
    ("tests.test_screen_capture", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("screen",),
        "Screen-capture helper integration with isolated local fixtures.",
        timeout=45.0,
    )),
    ("tests.test_screen_capture_mcp", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("screen", "mcp"),
        "MCP screen-capture integration with isolated fixtures.",
    )),
    ("tests.test_service_recovery", module_policy(
        L4_HOST_LIFECYCLE,
        ("host_lifecycle", "recovery"),
        "Service recovery lifecycle and ownership proof.",
        timeout=30.0,
        isolation_key="host-lifecycle",
    )),
    ("tests.test_supervised_lifecycle", module_policy(
        L4_HOST_LIFECYCLE,
        ("host_lifecycle", "lifecycle", "terminal"),
        "Real supervised-process lifecycle, timeout and cleanup proof.",
        timeout=45.0,
        isolation_key="host-lifecycle",
    )),
    ("tests.test_surface_and_scripts", module_policy(
        L3_DETERMINISTIC_REGRESSION,
        ("contract", "scripts"),
        "Cross-surface script and public-contract regression.",
    )),
    ("tests.test_task0041_boundedness", module_policy(
        L3_DETERMINISTIC_REGRESSION,
        ("contract", "mcp", "boundedness"),
        "Cross-module boundedness and server reload compatibility regression.",
    )),
    ("tests.test_task0055_cutover", module_policy(
        L5_QUALIFICATION_CHAOS_CUTOVER,
        ("cutover", "host_lifecycle"),
        "Legacy cutover and rollback qualification proof.",
        timeout=45.0,
        isolation_key="qualification",
    )),
    ("tests.test_task0055_macos_contract", module_policy(
        L3_DETERMINISTIC_REGRESSION,
        ("contract", "macos"),
        "Deterministic macOS packaging/lifecycle contract regression.",
    )),
    ("tests.test_task0057_admission", module_policy(
        L3_DETERMINISTIC_REGRESSION,
        ("capacity", "admission"),
        "Cross-module admission and bounded-capacity regression.",
        timeout=30.0,
    )),
    ("tests.test_task0059_transport", module_policy(
        L3_DETERMINISTIC_REGRESSION,
        ("transport", "contract"),
        "Cross-module transport contract regression.",
        timeout=30.0,
    )),
    ("tests.test_task0140_qualification", module_policy(
        L5_QUALIFICATION_CHAOS_CUTOVER,
        ("qualification", "sanitization"),
        "Qualification-specific regression retained as full-proof evidence.",
        timeout=30.0,
        isolation_key="qualification",
    )),
    ("tests.test_task0141_productization", module_policy(
        L3_DETERMINISTIC_REGRESSION,
        ("contract", "productization"),
        "Productization/documented-surface regression.",
    )),
    ("tests.test_task0145_unified_lifecycle", module_policy(
        L4_HOST_LIFECYCLE,
        ("host_lifecycle", "lifecycle", "terminal"),
        "Unified lifecycle compatibility proof across real process boundaries.",
        timeout=30.0,
        isolation_key="host-lifecycle",
    )),
    ("tests.test_task0147_continuation", module_policy(
        L3_DETERMINISTIC_REGRESSION,
        ("continuation", "terminal", "contract"),
        "Deterministic bounded-continuation compatibility regression.",
    )),
    ("tests.test_telemetry", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("telemetry",),
        "Deterministic telemetry encoding and policy checks.",
    )),
    ("tests.test_terminal_exec", module_policy(
        L4_HOST_LIFECYCLE,
        ("host_lifecycle", "terminal", "lifecycle"),
        "Real terminal execution ownership, timeout and cleanup proof.",
        timeout=45.0,
        isolation_key="host-lifecycle",
    )),
    ("tests.test_terminal_session", module_policy(
        L4_HOST_LIFECYCLE,
        ("host_lifecycle", "terminal", "lifecycle", "durable"),
        "Real terminal session lifecycle, process ownership and retention proof.",
        timeout=60.0,
        isolation_key="host-lifecycle",
    )),
    ("tests.test_timing", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("timing",),
        "Deterministic timing/deadline primitives.",
    )),
    ("tests.test_tool_contract", module_policy(
        L1_DETERMINISTIC_UNIT,
        ("contract",),
        "Pure ToolContract serialization and compatibility semantics.",
    )),
    ("tests.test_tunnel_identity", module_policy(
        L4_HOST_LIFECYCLE,
        ("host_lifecycle", "install", "tunnel", "config"),
        "Install/start identity lifecycle proof in isolated HOME/launchctl fixtures.",
        timeout=75.0,
        isolation_key="host-lifecycle",
    )),
    ("tests.test_uninstall", module_policy(
        L4_HOST_LIFECYCLE,
        ("host_lifecycle", "uninstall"),
        "Uninstall ownership and cleanup lifecycle proof.",
        timeout=45.0,
        isolation_key="host-lifecycle",
    )),
    ("tests.test_verify_harness", module_policy(
        L3_DETERMINISTIC_REGRESSION,
        ("verification",),
        "Verification-policy, selection, isolation and bounded-worker regression.",
        timeout=60.0,
    )),
    ("tests.test_wave1_mcp", module_policy(
        L2_ISOLATED_INTEGRATION,
        ("mcp", "contract"),
        "MCP integration across the initial bounded tool surface.",
        timeout=30.0,
    )),
)

MODULE_POLICIES = build_policy_map(MODULE_POLICY_ENTRIES)

GLOBAL_QUICK_MODULES = (
    "tests.test_capability_registry",
    "tests.test_failure_effect_contract",
    "tests.test_mcp_contracts",
    "tests.test_runtime_label_split",
    "tests.test_schema_export",
    "tests.test_tool_contract",
)

PATH_RULES: tuple[PathRule, ...] = (
    PathRule(
        "verify",
        ("verification",),
        force_qualification=True,
        reason="verification entrypoint changed",
    ),
    PathRule(
        "verify_tests.py",
        ("verification",),
        force_qualification=True,
        reason="verification harness changed",
    ),
    PathRule(
        "verification_policy.py",
        ("verification",),
        force_qualification=True,
        reason="verification policy changed",
    ),
    PathRule(
        "tests/test_verify_harness.py",
        ("verification",),
        force_qualification=True,
        reason="verification harness tests changed",
    ),
    PathRule(
        "install.sh",
        ("host_lifecycle", "install", "cutover"),
        force_qualification=True,
        reason="install lifecycle changed",
    ),
    PathRule(
        "start.sh",
        ("host_lifecycle", "lifecycle", "tunnel"),
        force_qualification=True,
        reason="runtime start lifecycle changed",
    ),
    PathRule(
        "macos/**",
        ("host_lifecycle", "package", "cutover", "macos"),
        force_qualification=True,
        reason="macOS package/cutover surface changed",
    ),
    PathRule(
        "requirements.lock",
        ("package", "release"),
        force_qualification=True,
        reason="packaged dependency lock changed",
    ),
    PathRule(
        "agent_runtime/version.py",
        ("contract", "package", "release"),
        force_qualification=True,
        reason="runtime release identity changed",
    ),
    PathRule(
        "agent_runtime/session.py",
        ("terminal", "lifecycle", "durable"),
        reason="terminal/session lifecycle changed",
    ),
    PathRule(
        "agent_runtime/executor.py",
        ("terminal", "lifecycle"),
        reason="executor lifecycle changed",
    ),
    PathRule(
        "agent_runtime/durable_pipe.py",
        ("durable", "restart", "terminal", "lifecycle"),
        reason="durable pipe changed",
    ),
    PathRule(
        "agent_runtime/durable_pipe_runner.py",
        ("durable", "restart", "terminal", "lifecycle"),
        reason="durable pipe runner changed",
    ),
    PathRule(
        "agent_runtime/repo_*.py",
        ("repo",),
        reason="repository tool changed",
    ),
    PathRule(
        "agent_runtime/fs_*.py",
        ("fs",),
        reason="filesystem tool changed",
    ),
    PathRule(
        "agent_runtime/capability_registry.py",
        ("contract", "capability"),
        reason="capability registry changed",
    ),
    PathRule(
        "agent_runtime/contracts.py",
        ("contract", "mcp", "schema"),
        reason="public contract surface changed",
    ),
    PathRule(
        "agent_runtime/tool_contract.py",
        ("contract", "mcp", "schema"),
        reason="ToolContract surface changed",
    ),
    PathRule(
        "agent_runtime/schema_export.py",
        ("contract", "schema"),
        reason="schema export changed",
    ),
    PathRule(
        "agent_runtime/server.py",
        ("contract", "mcp", "schema", "runtime_core"),
        reason="server/tool surface changed",
    ),
    PathRule(
        "agent_runtime/capacity.py",
        ("capacity", "admission", "lifecycle"),
        reason="capacity/admission changed",
    ),
    PathRule(
        "agent_runtime/doctor.py",
        ("doctor", "contract"),
        reason="doctor surface changed",
    ),
    PathRule(
        "agent_runtime/protection.py",
        ("protection", "host_lifecycle"),
        reason="protected-runtime policy changed",
    ),
    PathRule(
        "agent_runtime/screen_capture.py",
        ("screen",),
        reason="screen-capture integration changed",
    ),
    PathRule(
        "agent_runtime/telemetry.py",
        ("telemetry",),
        reason="telemetry changed",
    ),
    PathRule(
        "agent_runtime/timing.py",
        ("timing",),
        reason="timing primitives changed",
    ),
    PathRule(
        "agent_runtime/errors.py",
        ("contract", "failure"),
        reason="shared failure surface changed",
    ),
    PathRule(
        "agent_runtime/*.py",
        ("runtime_core",),
        reason="Agent Runtime implementation changed",
    ),
    PathRule(
        "tests/fixtures/**",
        ("verification",),
        force_qualification=True,
        reason="shared test fixture changed",
    ),
    PathRule(
        "tests/*.py",
        ("verification",),
        force_qualification=True,
        reason="unclassified test helper changed",
    ),
    PathRule("README.md", ("docs",), docs_only=True, reason="documentation only"),
    PathRule("*.md", ("docs",), docs_only=True, reason="documentation only"),
    PathRule("docs/**", ("docs",), docs_only=True, reason="documentation only"),
    PathRule(".agent/**", ("docs",), docs_only=True, reason="task/report metadata only"),
)


def matching_path_rules(path: str) -> tuple[PathRule, ...]:
    normalized = path.replace("\\", "/")
    return tuple(rule for rule in PATH_RULES if fnmatchcase(normalized, rule.pattern))
