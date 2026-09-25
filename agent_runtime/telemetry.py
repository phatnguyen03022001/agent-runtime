from __future__ import annotations

import math
import os
import re
import threading
from collections.abc import Mapping
from typing import AbstractSet, Any

from .version import RUNTIME_VERSION


TELEMETRY_ENV = "AGENT_RUNTIME_TELEMETRY"
TELEMETRY_MODES = frozenset({"off", "otlp"})
OTLP_HTTP_ENDPOINT = "http://127.0.0.1:4318"
OTLP_TRACES_ENDPOINT = OTLP_HTTP_ENDPOINT + "/v1/traces"
OTLP_METRICS_ENDPOINT = OTLP_HTTP_ENDPOINT + "/v1/metrics"
OTLP_HTTP_HEADERS = {"User-Agent": "agent-runtime-telemetry"}

SPAN_MAX_QUEUE_SIZE = 256
SPAN_MAX_EXPORT_BATCH_SIZE = 64
SPAN_SCHEDULE_DELAY_MILLIS = 1000
EXPORT_TIMEOUT_MILLIS = 1000
METRIC_EXPORT_INTERVAL_MILLIS = 10_000

MCP_REQUESTS_METRIC = "agent_runtime.mcp.requests"
MCP_REQUEST_DURATION_METRIC = "agent_runtime.mcp.request.duration"
TOOL_EXECUTIONS_METRIC = "agent_runtime.tool.executions"
PROCESS_COMPLETIONS_METRIC = "agent_runtime.process.completions"

_OUTCOMES = frozenset({"ok", "error", "cancelled", "timed_out"})
_PROCESS_KINDS = frozenset({"one_shot", "persistent_pty", "persistent_pipe"})
_TERMINATION_STATES = frozenset(
    {
        "completed",
        "natural_exit",
        "explicit_terminate",
        "timed_out",
        "hard_wall_timeout",
        "shutdown",
        "start_failed_post_effect",
        "idle_reap",
    }
)
_RUNTIME_CALL_ID = re.compile(r"^[0-9a-f]{32}$")
_RUNTIME_REVISION = re.compile(r"^[0-9a-f]{40}$")


def configured_mode(value: str | None = None) -> str:
    mode = os.environ.get(TELEMETRY_ENV, "off") if value is None else value
    if mode not in TELEMETRY_MODES:
        raise ValueError(f"{TELEMETRY_ENV} must be off or otlp")
    return mode


def _resource_attributes() -> dict[str, str]:
    attributes = {
        "service.name": "agent-runtime",
        "runtime.version": RUNTIME_VERSION,
    }
    revision = os.environ.get("AGENT_RUNTIME_REVISION")
    if isinstance(revision, str) and _RUNTIME_REVISION.fullmatch(revision):
        attributes["runtime.revision"] = revision
    return attributes


def _fixed_tool_name(
    event: Mapping[str, Any],
    allowed_tool_names: AbstractSet[str],
) -> str | None:
    tool_name = event.get("tool_name")
    if isinstance(tool_name, str) and tool_name in allowed_tool_names:
        return tool_name
    return None


def _fixed_outcome(event: Mapping[str, Any]) -> str | None:
    outcome = event.get("outcome")
    if isinstance(outcome, str) and outcome in _OUTCOMES:
        return outcome
    return None


def _fixed_duration(event: Mapping[str, Any]) -> float | None:
    value = event.get("monotonic_duration_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    duration = float(value)
    if not math.isfinite(duration) or duration < 0:
        return None
    return duration


def _wall_time_ns(event: Mapping[str, Any]) -> tuple[int, int] | None:
    start = event.get("wall_clock_start")
    end = event.get("wall_clock_end")
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, (int, float))
        or not isinstance(end, (int, float))
    ):
        return None
    start_value = float(start)
    end_value = float(end)
    if (
        not math.isfinite(start_value)
        or not math.isfinite(end_value)
        or start_value < 0
        or end_value < start_value
    ):
        return None
    return int(start_value * 1_000_000_000), int(end_value * 1_000_000_000)


def _runtime_trace_context(event: Mapping[str, Any]) -> object | None:
    runtime_call_id = event.get("runtime_call_id")
    if not isinstance(runtime_call_id, str) or _RUNTIME_CALL_ID.fullmatch(runtime_call_id) is None:
        return None

    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context

    trace_id = int(runtime_call_id, 16)
    if trace_id == 0:
        return None
    parent_span_id = int(runtime_call_id[-16:], 16) or 1
    span_context = SpanContext(
        trace_id=trace_id,
        span_id=parent_span_id,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return set_span_in_context(NonRecordingSpan(span_context))


def _loopback_http_session() -> object:
    import requests

    session = requests.Session()
    session.trust_env = False
    return session


class _TelemetrySink:
    def __init__(self, tracer: object, meter: object, *, owners: tuple[object, ...] = ()) -> None:
        self._tracer = tracer
        self._owners = owners
        self._mcp_requests = meter.create_counter(
            MCP_REQUESTS_METRIC,
            unit="{request}",
            description="Completed Agent Runtime MCP tool requests.",
        )
        self._mcp_request_duration = meter.create_histogram(
            MCP_REQUEST_DURATION_METRIC,
            unit="ms",
            description="Agent Runtime MCP tool request duration.",
        )
        self._tool_executions = meter.create_counter(
            TOOL_EXECUTIONS_METRIC,
            unit="{execution}",
            description="Completed Agent Runtime tool executions.",
        )
        self._process_completions = meter.create_counter(
            PROCESS_COMPLETIONS_METRIC,
            unit="{process}",
            description="Completed Agent Runtime process lifecycles.",
        )

    def _record_span(
        self,
        event: Mapping[str, Any],
        *,
        name: str,
        attributes: Mapping[str, str],
    ) -> None:
        times = _wall_time_ns(event)
        parent_context = _runtime_trace_context(event)
        if times is None or parent_context is None:
            return
        started_ns, ended_ns = times
        span = self._tracer.start_span(
            name,
            context=parent_context,
            attributes=dict(attributes),
            start_time=started_ns,
        )
        span.end(end_time=ended_ns)

    def observe(
        self,
        event: Mapping[str, Any],
        *,
        allowed_tool_names: AbstractSet[str],
    ) -> None:
        event_kind = event.get("event_kind")
        if event_kind not in {"mcp_request_end", "tool_execution_end", "process_end"}:
            return

        tool_name = _fixed_tool_name(event, allowed_tool_names)
        outcome = _fixed_outcome(event)
        if tool_name is None or outcome is None:
            return

        base_attributes = {"tool.name": tool_name, "outcome": outcome}
        if event_kind == "mcp_request_end":
            duration = _fixed_duration(event)
            if duration is None:
                return
            self._mcp_requests.add(1, attributes=base_attributes)
            self._mcp_request_duration.record(duration, attributes=base_attributes)
            self._record_span(
                event,
                name="agent_runtime.mcp.request",
                attributes=base_attributes,
            )
            return

        if event_kind == "tool_execution_end":
            self._tool_executions.add(1, attributes=base_attributes)
            self._record_span(
                event,
                name="agent_runtime.tool.execution",
                attributes=base_attributes,
            )
            return

        process_kind = event.get("process_kind")
        termination_state = event.get("termination_state")
        if (
            not isinstance(process_kind, str)
            or process_kind not in _PROCESS_KINDS
            or not isinstance(termination_state, str)
            or termination_state not in _TERMINATION_STATES
        ):
            return
        process_attributes = {
            "tool.name": tool_name,
            "process.kind": process_kind,
            "termination.state": termination_state,
        }
        self._process_completions.add(1, attributes=process_attributes)
        self._record_span(
            event,
            name="agent_runtime.process.lifecycle",
            attributes={**base_attributes, **process_attributes},
        )


def _build_otlp_sink() -> _TelemetrySink:
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    resource = Resource(_resource_attributes())
    span_session = _loopback_http_session()
    metric_session = _loopback_http_session()
    span_exporter = OTLPSpanExporter(
        endpoint=OTLP_TRACES_ENDPOINT,
        headers=dict(OTLP_HTTP_HEADERS),
        timeout=EXPORT_TIMEOUT_MILLIS / 1000,
        compression=Compression.NoCompression,
        session=span_session,
    )
    metric_exporter = OTLPMetricExporter(
        endpoint=OTLP_METRICS_ENDPOINT,
        headers=dict(OTLP_HTTP_HEADERS),
        timeout=EXPORT_TIMEOUT_MILLIS / 1000,
        compression=Compression.NoCompression,
        session=metric_session,
    )

    tracer_provider = TracerProvider(
        resource=resource,
        sampler=ALWAYS_ON,
        shutdown_on_exit=False,
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            span_exporter,
            max_queue_size=SPAN_MAX_QUEUE_SIZE,
            schedule_delay_millis=SPAN_SCHEDULE_DELAY_MILLIS,
            max_export_batch_size=SPAN_MAX_EXPORT_BATCH_SIZE,
            export_timeout_millis=EXPORT_TIMEOUT_MILLIS,
        )
    )
    metric_reader = PeriodicExportingMetricReader(
        metric_exporter,
        export_interval_millis=METRIC_EXPORT_INTERVAL_MILLIS,
        export_timeout_millis=EXPORT_TIMEOUT_MILLIS,
    )
    meter_provider = MeterProvider(
        metric_readers=(metric_reader,),
        resource=resource,
        shutdown_on_exit=False,
    )
    return _TelemetrySink(
        tracer_provider.get_tracer("agent_runtime.telemetry"),
        meter_provider.get_meter("agent_runtime.telemetry"),
        owners=(
            tracer_provider,
            meter_provider,
            span_exporter,
            metric_exporter,
            span_session,
            metric_session,
        ),
    )


class _TelemetryAdapter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._resolved = False
        self._sink: _TelemetrySink | None = None

    def _resolve(self) -> _TelemetrySink | None:
        if self._resolved:
            return self._sink
        with self._lock:
            if self._resolved:
                return self._sink
            try:
                mode = configured_mode()
                if mode == "otlp":
                    self._sink = _build_otlp_sink()
            except Exception:
                self._sink = None
            self._resolved = True
            return self._sink

    def observe(
        self,
        event: Mapping[str, Any],
        *,
        allowed_tool_names: AbstractSet[str],
    ) -> None:
        try:
            sink = self._resolve()
            if sink is not None:
                sink.observe(event, allowed_tool_names=allowed_tool_names)
        except Exception:
            return


_ADAPTER = _TelemetryAdapter()


def observe_timing_event(
    event: Mapping[str, Any],
    *,
    allowed_tool_names: AbstractSet[str],
) -> None:
    _ADAPTER.observe(event, allowed_tool_names=allowed_tool_names)
