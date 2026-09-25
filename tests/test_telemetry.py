from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_runtime import telemetry
from agent_runtime.capability_registry import ADVERTISED_TOOL_NAMES
from agent_runtime.version import RUNTIME_VERSION


ALLOWED_TOOLS = frozenset(ADVERTISED_TOOL_NAMES)
RUNTIME_CALL_ID = "0123456789abcdef0123456789abcdef"
SECRET_SENTINELS = (
    "argv-secret",
    "/Users/private/repository",
    "https://github.com/private/repository",
    "request-argument-secret",
    "result-secret",
    "stdout-secret",
    "stderr-secret",
    "exception-secret",
    "environment-secret",
    "git-identity-secret",
    "api-token-secret",
    "session-secret",
    "start-identity-secret",
    "continuation-receipt-secret",
)


def _event(kind: str, **extra: object) -> dict[str, object]:
    event: dict[str, object] = {
        "event_kind": kind,
        "runtime_call_id": RUNTIME_CALL_ID,
        "raw_request_id": "request-argument-secret",
        "request_id_type": "str",
        "tool_name": "terminal_start",
        "wall_clock_start": 100.0,
        "wall_clock_end": 101.0,
        "monotonic_duration_ms": 1000.0,
        "outcome": "ok",
        "argv": ["argv-secret"],
        "cwd": "/Users/private/repository",
        "arguments": {"secret": "request-argument-secret"},
        "result": "result-secret",
        "stdout": "stdout-secret",
        "stderr": "stderr-secret",
        "exception_message": "exception-secret",
        "environment": {"TOKEN": "environment-secret"},
        "git_identity": "git-identity-secret",
        "api_token": "api-token-secret",
        "repo_url": "https://github.com/private/repository",
        "session_id": "session-secret",
        "start_identity": "start-identity-secret",
        "continuation_receipt": "continuation-receipt-secret",
    }
    event.update(extra)
    return event


class TelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        resource = Resource(
            {
                "service.name": "agent-runtime",
                "runtime.version": RUNTIME_VERSION,
                "runtime.revision": "a" * 40,
            }
        )
        self.span_exporter = InMemorySpanExporter()
        self.tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
        self.tracer_provider.add_span_processor(SimpleSpanProcessor(self.span_exporter))
        self.metric_reader = InMemoryMetricReader()
        self.meter_provider = MeterProvider(
            metric_readers=(self.metric_reader,),
            resource=resource,
            shutdown_on_exit=False,
        )
        self.sink = telemetry._TelemetrySink(
            self.tracer_provider.get_tracer("test.telemetry"),
            self.meter_provider.get_meter("test.telemetry"),
        )

    def tearDown(self) -> None:
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()

    def _metric_points(self) -> dict[str, list[object]]:
        data = self.metric_reader.get_metrics_data()
        result: dict[str, list[object]] = {}
        for resource_metrics in data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    result.setdefault(metric.name, []).extend(metric.data.data_points)
        return result

    def test_metrics_and_spans_are_correlated_and_privacy_bounded(self) -> None:
        self.sink.observe(
            _event("mcp_request_end"),
            allowed_tool_names=ALLOWED_TOOLS,
        )
        self.sink.observe(
            _event("tool_execution_end", wall_clock_start=100.1, wall_clock_end=100.9),
            allowed_tool_names=ALLOWED_TOOLS,
        )
        self.sink.observe(
            _event(
                "process_end",
                wall_clock_start=100.1,
                wall_clock_end=105.0,
                monotonic_duration_ms=4900.0,
                process_kind="persistent_pty",
                termination_state="completed",
            ),
            allowed_tool_names=ALLOWED_TOOLS,
        )

        spans = self.span_exporter.get_finished_spans()
        self.assertEqual(
            {span.name for span in spans},
            {
                "agent_runtime.mcp.request",
                "agent_runtime.tool.execution",
                "agent_runtime.process.lifecycle",
            },
        )
        self.assertEqual({span.context.trace_id for span in spans}, {int(RUNTIME_CALL_ID, 16)})
        self.assertGreater(
            next(span.end_time for span in spans if span.name == "agent_runtime.process.lifecycle"),
            next(span.end_time for span in spans if span.name == "agent_runtime.mcp.request"),
        )
        for span in spans:
            self.assertLessEqual(
                set(span.attributes),
                {"tool.name", "outcome", "process.kind", "termination.state"},
            )

        points = self._metric_points()
        self.assertEqual(
            set(points),
            {
                telemetry.MCP_REQUESTS_METRIC,
                telemetry.MCP_REQUEST_DURATION_METRIC,
                telemetry.TOOL_EXECUTIONS_METRIC,
                telemetry.PROCESS_COMPLETIONS_METRIC,
            },
        )
        self.assertEqual(
            set(points[telemetry.MCP_REQUESTS_METRIC][0].attributes),
            {"tool.name", "outcome"},
        )
        self.assertEqual(
            set(points[telemetry.MCP_REQUEST_DURATION_METRIC][0].attributes),
            {"tool.name", "outcome"},
        )
        self.assertEqual(
            set(points[telemetry.TOOL_EXECUTIONS_METRIC][0].attributes),
            {"tool.name", "outcome"},
        )
        self.assertEqual(
            set(points[telemetry.PROCESS_COMPLETIONS_METRIC][0].attributes),
            {"tool.name", "process.kind", "termination.state"},
        )

        exported = repr(spans) + repr(points)
        self.assertNotIn(RUNTIME_CALL_ID, repr(points))
        for sentinel in SECRET_SENTINELS:
            self.assertNotIn(sentinel, exported)

    def test_unknown_or_high_cardinality_values_are_not_exported(self) -> None:
        self.sink.observe(
            _event("mcp_request_end", tool_name="operator-controlled-tool"),
            allowed_tool_names=ALLOWED_TOOLS,
        )
        self.sink.observe(
            _event("mcp_request_end", outcome="operator-controlled-outcome"),
            allowed_tool_names=ALLOWED_TOOLS,
        )
        self.sink.observe(
            _event(
                "process_end",
                process_kind="operator-controlled-kind",
                termination_state="operator-controlled-state",
            ),
            allowed_tool_names=ALLOWED_TOOLS,
        )
        self.assertEqual(self.span_exporter.get_finished_spans(), ())
        data = self.metric_reader.get_metrics_data()
        self.assertTrue(data is None or not data.resource_metrics)

    def test_resource_attributes_ignore_ambient_otel_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OTEL_SERVICE_NAME": "operator-controlled-service",
                "OTEL_RESOURCE_ATTRIBUTES": "secret=value",
                "AGENT_RUNTIME_REVISION": "b" * 40,
            },
            clear=False,
        ):
            self.assertEqual(
                telemetry._resource_attributes(),
                {
                    "service.name": "agent-runtime",
                    "runtime.version": RUNTIME_VERSION,
                    "runtime.revision": "b" * 40,
                },
            )

        with patch.dict(
            os.environ,
            {"AGENT_RUNTIME_REVISION": "invalid-revision-secret"},
            clear=False,
        ):
            self.assertEqual(
                telemetry._resource_attributes(),
                {
                    "service.name": "agent-runtime",
                    "runtime.version": RUNTIME_VERSION,
                },
            )

    def test_disabled_mode_does_not_initialize_sdk_or_exporter(self) -> None:
        adapter = telemetry._TelemetryAdapter()
        with patch.dict(os.environ, {telemetry.TELEMETRY_ENV: "off"}, clear=False), patch(
            "agent_runtime.telemetry._build_otlp_sink"
        ) as build:
            adapter.observe(_event("mcp_request_end"), allowed_tool_names=ALLOWED_TOOLS)
            adapter.observe(_event("mcp_request_end"), allowed_tool_names=ALLOWED_TOOLS)
        build.assert_not_called()

    def test_disabled_mode_imports_no_sdk_or_http_client_in_fresh_process(self) -> None:
        code = """
import os
import sys
os.environ["AGENT_RUNTIME_TELEMETRY"] = "off"
from agent_runtime import telemetry
adapter = telemetry._TelemetryAdapter()
adapter.observe(
    {
        "event_kind": "mcp_request_end",
        "runtime_call_id": "0123456789abcdef0123456789abcdef",
        "tool_name": "terminal_exec",
        "outcome": "ok",
        "wall_clock_start": 1.0,
        "wall_clock_end": 2.0,
        "monotonic_duration_ms": 1000.0,
    },
    allowed_tool_names=frozenset({"terminal_exec"}),
)
assert not any(name.startswith("opentelemetry.sdk") for name in sys.modules)
assert "requests" not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env={**os.environ, telemetry.TELEMETRY_ENV: "off"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exporter_initialization_failure_is_fail_open_and_not_retried(self) -> None:
        adapter = telemetry._TelemetryAdapter()
        with patch.dict(os.environ, {telemetry.TELEMETRY_ENV: "otlp"}, clear=False), patch(
            "agent_runtime.telemetry._build_otlp_sink",
            side_effect=RuntimeError("collector initialization sentinel"),
        ) as build:
            adapter.observe(_event("mcp_request_end"), allowed_tool_names=ALLOWED_TOOLS)
            adapter.observe(_event("mcp_request_end"), allowed_tool_names=ALLOWED_TOOLS)
        self.assertEqual(build.call_count, 1)

    def test_fixed_otlp_http_authority_and_proxy_hardening(self) -> None:
        self.assertEqual(telemetry.OTLP_HTTP_ENDPOINT, "http://127.0.0.1:4318")
        self.assertEqual(telemetry.OTLP_TRACES_ENDPOINT, "http://127.0.0.1:4318/v1/traces")
        self.assertEqual(telemetry.OTLP_METRICS_ENDPOINT, "http://127.0.0.1:4318/v1/metrics")
        self.assertEqual(telemetry.OTLP_HTTP_HEADERS, {"User-Agent": "agent-runtime-telemetry"})
        session = telemetry._loopback_http_session()
        try:
            self.assertIs(session.trust_env, False)
        finally:
            session.close()

    def test_process_trace_is_retroactive_and_requires_no_trace_registry(self) -> None:
        self.sink.observe(
            _event(
                "mcp_request_end",
                wall_clock_start=10.0,
                wall_clock_end=11.0,
                monotonic_duration_ms=1000.0,
            ),
            allowed_tool_names=ALLOWED_TOOLS,
        )
        self.sink.observe(
            _event(
                "process_end",
                wall_clock_start=10.2,
                wall_clock_end=30.0,
                monotonic_duration_ms=19_800.0,
                process_kind="persistent_pipe",
                termination_state="natural_exit",
            ),
            allowed_tool_names=ALLOWED_TOOLS,
        )
        spans = self.span_exporter.get_finished_spans()
        request = next(span for span in spans if span.name == "agent_runtime.mcp.request")
        process = next(span for span in spans if span.name == "agent_runtime.process.lifecycle")
        self.assertEqual(request.context.trace_id, process.context.trace_id)
        self.assertGreater(process.end_time, request.end_time)
        self.assertFalse(hasattr(telemetry._TelemetryAdapter(), "_trace_registry"))


if __name__ == "__main__":
    unittest.main()
