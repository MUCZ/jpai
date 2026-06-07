"""Observability tests."""

from __future__ import annotations

import asyncio
import io
import json
import os
import unittest
from contextlib import contextmanager, suppress
from unittest.mock import patch

os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
os.environ.pop("OTEL_EXPORTER_OTLP_HEADERS", None)

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from src import llm_client, main, orchestrator
from src.models import Priority
from src.observability import metric_tenant_label
from src.sinks import NoopBoundedSink


class _FakeTracerProvider:
    def __init__(self, resource=None):
        self.resource = resource
        self.span_processors = []

    def add_span_processor(self, processor):
        self.span_processors.append(processor)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.timeouts: list[float | None] = []

    async def post(
        self, url: str, json: dict, timeout: float | None = None
    ) -> _FakeResponse:
        self.timeouts.append(timeout)
        return self._responses.pop(0)


class ObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        import structlog
        main.task_result_sink.clear()
        main._response_cache.clear()
        orchestrator.execution_audit_sink.clear()
        main._task_scheduler = main._PriorityTaskScheduler(main.MAX_CONCURRENT_TASKS)
        self.client = TestClient(main.app)
        self.log_stream = io.StringIO()
        structlog.reset_defaults()
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            logger_factory=structlog.PrintLoggerFactory(file=self.log_stream),
        )

    def tearDown(self) -> None:
        import structlog
        self.client.close()
        structlog.reset_defaults()
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            logger_factory=structlog.PrintLoggerFactory(),
        )

    def _metric_value(self, name: str, labels: dict | None = None) -> float:
        labels = labels or {}
        response = self.client.get("/metrics")
        total = 0.0
        for family in text_string_to_metric_families(response.text):
            for sample in family.samples:
                if sample.name != name:
                    continue
                if all(sample.labels.get(key) == value for key, value in labels.items()):
                    total += sample.value
        return total

    @contextmanager
    def _mock_orchestrator(self, stage_error: tuple[str, str] | None = None, llm_calls: dict | None = None, tools_calls: dict | None = None):
        async def fake_call_llm(prompt: str, max_tokens: int, *, operation: str, tenant_id: str, priority: Priority) -> dict:
            if llm_calls is not None:
                llm_calls["count"] += 1
            if stage_error and operation == stage_error[0]:
                return {
                    "error": stage_error[1],
                    "text": "",
                    "prompt_tokens": 3,
                    "completion_tokens": 0,
                }
            return {
                "text": f"{operation}-ok",
                "prompt_tokens": 10,
                "completion_tokens": 5,
            }

        async def fake_execute_tools(tools, *, tenant_id: str, priority: Priority):
            if tools_calls is not None:
                tools_calls["count"] += 1
            return [{"tool": tool_name, "status": "success", "output": "ok"} for tool_name, _ in tools]

        with patch.object(orchestrator, "call_llm", side_effect=fake_call_llm), \
             patch.object(orchestrator, "execute_tools", side_effect=fake_execute_tools):
            yield

    def test_metrics_endpoint_exposes_expected_metrics(self) -> None:
        # Trigger a task to ensure all metrics (HTTP, Task, LLM) are recorded at least once
        payload = {
            "task_description": "Summarise customer feedback",
            "tenant_id": "tenant-alpha",
            "priority": "normal",
        }
        with self._mock_orchestrator():
            self.client.post("/tasks", json=payload)

        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("http_server_duration_milliseconds", response.text)
        self.assertIn("agent_tasks_total", response.text)
        self.assertIn("agent_llm_requests_total", response.text)
        self.assertIn("X-Trace-Id", response.headers)

    def test_successful_task_has_trace_header_and_updates_metrics(self) -> None:
        payload = {
            "task_description": "Summarise customer feedback",
            "tenant_id": "tenant-alpha",
            "priority": "normal",
        }
        metric_tenant = metric_tenant_label(payload["tenant_id"])
        
        before_completed = self._metric_value("agent_tasks_total", {"tenant_id": metric_tenant, "priority": "normal", "status": "completed", "source": "fresh"})
        before_plan_stage = self._metric_value("agent_task_stage_duration_seconds_count", {"tenant_id": metric_tenant, "priority": "normal", "stage": "plan", "outcome": "success"})

        with self._mock_orchestrator():
            response = self.client.post("/tasks", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual(len(response.headers["X-Trace-Id"]), 32)
        
        # Verify prometheus metrics
        self.assertGreater(self._metric_value("agent_tasks_total", {"tenant_id": metric_tenant, "priority": "normal", "status": "completed", "source": "fresh"}), before_completed)
        self.assertGreater(self._metric_value("agent_task_stage_duration_seconds_count", {"tenant_id": metric_tenant, "priority": "normal", "stage": "plan", "outcome": "success"}), before_plan_stage)

        # Verify structlog structured output (contextvars)
        log_output = self.log_stream.getvalue().strip().split("\n")
        self.assertGreater(len(log_output), 0, "Expected structlog to emit JSON logs")
        
        found_trace_id = False
        for line in log_output:
            if not line.strip():
                continue
            log_data = json.loads(line)
            if "trace_id" in log_data and "tenant_id" in log_data:
                self.assertEqual(log_data["tenant_id"], "tenant-alpha")
                self.assertEqual(len(log_data["trace_id"]), 32)
                found_trace_id = True
        self.assertTrue(found_trace_id, "Logs did not contain injected trace_id and tenant_id")

    def test_cache_hit_records_cache_metrics(self) -> None:
        llm_calls = {"count": 0}
        tools_calls = {"count": 0}
        
        payload = {
            "task_description": "Summarise customer feedback",
            "tenant_id": "tenant-cache",
            "priority": "normal",
        }
        metric_tenant = metric_tenant_label(payload["tenant_id"])
        before_cache = self._metric_value("agent_tasks_total", {"tenant_id": metric_tenant, "priority": "normal", "status": "completed", "source": "cache"})

        with self._mock_orchestrator(llm_calls=llm_calls, tools_calls=tools_calls):
            first = self.client.post("/tasks", json=payload)
            second = self.client.post("/tasks", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(llm_calls["count"], 3)
        self.assertEqual(tools_calls["count"], 1)
        self.assertGreater(self._metric_value("agent_tasks_total", {"tenant_id": metric_tenant, "priority": "normal", "status": "completed", "source": "cache"}), before_cache)

    def test_response_cache_reuses_results_across_priorities(self) -> None:
        llm_calls = {"count": 0}
        tools_calls = {"count": 0}
        payload = {
            "task_description": "Summarise customer feedback",
            "tenant_id": "tenant-cache-priority",
            "priority": "normal",
        }

        with self._mock_orchestrator(llm_calls=llm_calls, tools_calls=tools_calls):
            first = self.client.post("/tasks", json=payload)
            second = self.client.post(
                "/tasks",
                json={**payload, "priority": "urgent"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(llm_calls["count"], 3)
        self.assertEqual(tools_calls["count"], 1)

    def test_llm_retry_metrics_are_recorded(self) -> None:
        metric_tenant = metric_tenant_label("tenant-retry")
        before_retry = self._metric_value("agent_llm_retries_total", {"tenant_id": metric_tenant, "priority": "normal", "operation": "plan", "reason": "status_500"})
        before_success = self._metric_value("agent_llm_requests_total", {"tenant_id": metric_tenant, "priority": "normal", "operation": "plan", "outcome": "success"})

        fake_client = _FakeAsyncClient([_FakeResponse(500), _FakeResponse(200, {"text": "ok", "prompt_tokens": 7, "completion_tokens": 3})])

        with patch.object(llm_client, "_get_client", return_value=fake_client), \
             patch.object(llm_client, "RETRY_MAX_ATTEMPTS", 2), \
             patch.object(llm_client, "RETRY_BASE_DELAY", 0), \
             patch.object(llm_client.random, "uniform", return_value=0):
            result = asyncio.run(llm_client.call_llm("Plan the task", max_tokens=64, operation="plan", tenant_id="tenant-retry", priority=Priority.NORMAL))

        self.assertEqual(result["text"], "ok")
        self.assertGreater(self._metric_value("agent_llm_retries_total", {"tenant_id": metric_tenant, "priority": "normal", "operation": "plan", "reason": "status_500"}), before_retry)
        self.assertGreater(self._metric_value("agent_llm_requests_total", {"tenant_id": metric_tenant, "priority": "normal", "operation": "plan", "outcome": "success"}), before_success)

    def test_priority_policy_controls_llm_timeout_and_attempts(self) -> None:
        test_cases = [
            (
                Priority.URGENT,
                1.5,
                [
                    _FakeResponse(500),
                    _FakeResponse(
                        200,
                        {"text": "ok", "prompt_tokens": 7, "completion_tokens": 3},
                    ),
                ],
                "ok",
                2,
            ),
            (
                Priority.NORMAL,
                5.0,
                [
                    _FakeResponse(500),
                    _FakeResponse(500),
                    _FakeResponse(
                        200,
                        {"text": "ok", "prompt_tokens": 7, "completion_tokens": 3},
                    ),
                ],
                "ok",
                3,
            ),
            (
                Priority.LOW,
                5.0,
                [_FakeResponse(500), _FakeResponse(200, {"text": "should-not-run"})],
                "",
                1,
            ),
        ]

        for priority, timeout, responses, text, calls in test_cases:
            with self.subTest(priority=priority.value):
                fake_client = _FakeAsyncClient(responses)
                with patch.object(llm_client, "_get_client", return_value=fake_client), \
                     patch.object(llm_client, "RETRY_BASE_DELAY", 0), \
                     patch.object(llm_client.random, "uniform", return_value=0):
                    result = asyncio.run(llm_client.call_llm(
                        "Plan the task",
                        max_tokens=64,
                        operation="plan",
                        tenant_id=f"tenant-{priority.value}",
                        priority=priority,
                    ))

                self.assertEqual(result["text"], text)
                self.assertEqual(len(fake_client.timeouts), calls)
                self.assertEqual(fake_client.timeouts, [timeout] * calls)

    def test_priority_limiter_admits_urgent_before_older_normal(self) -> None:
        async def run_test() -> list[str]:
            scheduler = main._PriorityTaskScheduler(1)
            first_slot = await scheduler.acquire("tenant-blocker", Priority.LOW)
            order = []

            async def wait_for_slot(priority: Priority, label: str):
                slot = await scheduler.acquire(f"tenant-{label}", priority)
                order.append(label)
                await slot.release()

            normal = asyncio.create_task(wait_for_slot(Priority.NORMAL, "normal"))
            await asyncio.sleep(0)
            urgent = asyncio.create_task(wait_for_slot(Priority.URGENT, "urgent"))
            await asyncio.sleep(0)
            await first_slot.release()
            await asyncio.gather(normal, urgent)
            return order

        self.assertEqual(asyncio.run(run_test()), ["urgent", "normal"])

    def test_same_tenant_scheduler_admits_urgent_before_older_low(self) -> None:
        async def run_test() -> list[str]:
            scheduler = main._PriorityTaskScheduler(1)
            blocker_slot = await scheduler.acquire("tenant-blocker", Priority.NORMAL)
            order = []

            async def wait_for_slot(priority: Priority, label: str):
                slot = await scheduler.acquire("tenant-shared", priority)
                order.append(label)
                await slot.release()

            low = asyncio.create_task(wait_for_slot(Priority.LOW, "low"))
            await asyncio.sleep(0)
            urgent = asyncio.create_task(wait_for_slot(Priority.URGENT, "urgent"))
            await asyncio.sleep(0)
            await blocker_slot.release()
            await asyncio.gather(low, urgent)
            return order

        self.assertEqual(asyncio.run(run_test()), ["urgent", "low"])

    def test_scheduler_prunes_cancelled_waiters_before_waking_next(self) -> None:
        async def run_test() -> tuple[int, set[str], int]:
            scheduler = main._PriorityTaskScheduler(1)
            blocker_slot = await scheduler.acquire("tenant-blocker", Priority.NORMAL)
            admitted = []

            async def wait_for_slot(tenant: str):
                slot = await scheduler.acquire(tenant, Priority.NORMAL)
                admitted.append(tenant)
                await slot.release()

            cancelled = asyncio.create_task(wait_for_slot("tenant-cancelled"))
            await asyncio.sleep(0)
            cancelled.cancel()
            with suppress(asyncio.CancelledError):
                await cancelled

            next_waiter = asyncio.create_task(wait_for_slot("tenant-next"))
            await asyncio.sleep(0)
            await blocker_slot.release()
            await asyncio.wait_for(next_waiter, timeout=0.1)

            return scheduler._running, scheduler._active_tenants, len(scheduler._waiters)

        running, active_tenants, waiter_count = asyncio.run(run_test())
        self.assertEqual(running, 0)
        self.assertEqual(active_tenants, set())
        self.assertEqual(waiter_count, 0)

    def test_scheduler_cancelled_acquire_does_not_corrupt_active_slot(self) -> None:
        async def run_test() -> tuple[int, set[str]]:
            scheduler = main._PriorityTaskScheduler(1)
            active_slot = await scheduler.acquire("tenant-active", Priority.NORMAL)

            waiter = asyncio.create_task(
                scheduler.acquire("tenant-waiting", Priority.NORMAL)
            )
            await asyncio.sleep(0)
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter

            await active_slot.release()
            return scheduler._running, scheduler._active_tenants

        running, active_tenants = asyncio.run(run_test())
        self.assertEqual(running, 0)
        self.assertEqual(active_tenants, set())

    def test_timeout_path_records_timeout_metric(self) -> None:
        async def slow_run_task(*args, **kwargs):
            await asyncio.sleep(0.05)
            raise AssertionError("wait_for should time out before completion")

        metric_tenant = metric_tenant_label("tenant-timeout")
        before_timeouts = self._metric_value("agent_task_timeouts_total", {"tenant_id": metric_tenant, "priority": "normal"})

        with patch.object(main, "run_task", side_effect=slow_run_task), patch.object(main, "TASK_TIMEOUT_SECONDS", 0.01):
            response = self.client.post("/tasks", json={"task_description": "Run long task", "tenant_id": "tenant-timeout", "priority": "normal"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertGreater(self._metric_value("agent_task_timeouts_total", {"tenant_id": metric_tenant, "priority": "normal"}), before_timeouts)

    def test_scheduler_wait_timeout_records_queue_wait_metric(self) -> None:
        main._task_scheduler = main._PriorityTaskScheduler(1)
        blocker = asyncio.run(
            main._task_scheduler.acquire("tenant-blocker", Priority.NORMAL)
        )
        metric_tenant = metric_tenant_label("tenant-queue-timeout")
        before_waits = self._metric_value(
            "agent_task_queue_wait_seconds_count",
            {
                "tenant_id": metric_tenant,
                "priority": "low",
                "queue": "priority_scheduler",
            },
        )

        try:
            with patch.object(main, "TASK_TIMEOUT_SECONDS", 0.01):
                response = self.client.post("/tasks", json={
                    "task_description": "Wait behind full scheduler",
                    "tenant_id": "tenant-queue-timeout",
                    "priority": "low",
                })
        finally:
            asyncio.run(blocker.release())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertGreater(
            self._metric_value(
                "agent_task_queue_wait_seconds_count",
                {
                    "tenant_id": metric_tenant,
                    "priority": "low",
                    "queue": "priority_scheduler",
                },
            ),
            before_waits,
        )

    def test_not_found_response_has_trace_header(self) -> None:
        response = self.client.get("/tasks/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(response.headers["X-Trace-Id"]), 32)

    def test_stage_failures_mark_task_failed(self) -> None:
        test_cases = [
            ("summarise", "LLM request timed out"),
            ("validate", "validation unavailable"),
        ]
        for stage, expected_error in test_cases:
            with self.subTest(stage=stage, expected_error=expected_error):
                with self._mock_orchestrator(stage_error=(stage, expected_error)):
                    response = self.client.post("/tasks", json={"task_description": "Test task", "tenant_id": f"tenant-{stage}-fail", "priority": "normal"})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["status"], "failed")
                    self.assertEqual(response.json()["error"], expected_error)

    def test_response_cache_is_bounded(self) -> None:
        with self._mock_orchestrator(), patch.object(main, "RESPONSE_CACHE_MAX_ENTRIES", 2):
            for idx in range(3):
                response = self.client.post("/tasks", json={"task_description": f"task-{idx}", "tenant_id": "tenant-cache-bound", "priority": "normal"})
                self.assertEqual(response.status_code, 200)

        self.assertEqual(len(main._response_cache), 2)

    def test_noop_bounded_sink_drops_oldest_records(self) -> None:
        sink = NoopBoundedSink(max_entries=2)

        sink.publish({"id": "first"})
        sink.publish({"id": "second"})
        sink.publish({"id": "third"})

        self.assertEqual(sink.snapshot(), [{"id": "second"}, {"id": "third"}])

    def test_task_result_sink_is_bounded_for_recent_lookup(self) -> None:
        bounded_sink = NoopBoundedSink(max_entries=2)
        with self._mock_orchestrator(), patch.object(main, "task_result_sink", bounded_sink):
            for idx in range(3):
                response = self.client.post(
                    "/tasks",
                    json={
                        "task_description": f"task-store-{idx}",
                        "tenant_id": "tenant-store-bound",
                        "priority": "normal",
                    },
                )
                self.assertEqual(response.status_code, 200)

        self.assertEqual(len(bounded_sink), 2)

    def test_task_result_and_audit_records_are_published_to_sinks(self) -> None:
        with self._mock_orchestrator():
            response = self.client.post(
                "/tasks",
                json={
                    "task_description": "Export this task",
                    "tenant_id": "tenant-export",
                    "priority": "normal",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(main.task_result_sink), 1)
        self.assertEqual(len(orchestrator.execution_audit_sink), 1)
        self.assertEqual(main.task_result_sink.snapshot()[0].tenant_id, "tenant-export")
        self.assertEqual(
            orchestrator.execution_audit_sink.snapshot()[0]["tenant_id"],
            "tenant-export",
        )

    def test_setup_tracing_configures_otlp_exporter_when_endpoint_is_set(self) -> None:
        from src import observability

        fake_provider = _FakeTracerProvider()
        fake_processor = object()

        with patch.object(observability, "_INITIALIZED", False), \
             patch.dict(os.environ, {
                 "OTEL_EXPORTER_OTLP_ENDPOINT": "http://jaeger:4318/v1/traces",
                 "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer token, X-Scope-OrgID=test-tenant",
                 "OTEL_SERVICE_NAME": "agent-service",
             }, clear=False), \
             patch.object(observability, "TracerProvider", return_value=fake_provider) as tracer_provider_cls, \
             patch.object(observability, "OTLPSpanExporter", return_value="fake-exporter") as exporter_cls, \
             patch.object(observability, "OTLPMetricExporter") as metric_exporter_cls, \
             patch.object(observability, "OTLPLogExporter") as log_exporter_cls, \
             patch.object(observability, "BatchSpanProcessor", return_value=fake_processor) as processor_cls, \
             patch.object(observability.trace, "set_tracer_provider") as set_provider, \
             patch.object(observability.metrics, "set_meter_provider"), \
             patch.object(observability, "_setup_logging"), \
             patch.object(observability, "set_logger_provider"):
            observability.init_observability()

        tracer_provider_cls.assert_called_once()
        exporter_cls.assert_called_once_with(
            endpoint="http://jaeger:4318/v1/traces",
        )
        processor_cls.assert_called_once_with("fake-exporter")
        self.assertEqual(fake_provider.span_processors, [fake_processor])
        set_provider.assert_called_once_with(fake_provider)
        self.assertTrue(observability._INITIALIZED)

    def test_setup_tracing_skips_exporter_when_endpoint_is_unset(self) -> None:
        from src import observability

        fake_provider = _FakeTracerProvider()

        with patch.object(observability, "_INITIALIZED", False), \
             patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "", "OTEL_EXPORTER_OTLP_HEADERS": ""}, clear=False), \
             patch.object(observability, "TracerProvider", return_value=fake_provider), \
             patch.object(observability, "OTLPSpanExporter") as exporter_cls, \
             patch.object(observability, "OTLPMetricExporter") as metric_exporter_cls, \
             patch.object(observability, "OTLPLogExporter") as log_exporter_cls, \
             patch.object(observability, "BatchSpanProcessor") as processor_cls, \
             patch.object(observability.trace, "set_tracer_provider") as set_provider, \
             patch.object(observability.metrics, "set_meter_provider"), \
             patch.object(observability, "_setup_logging"), \
             patch.object(observability, "set_logger_provider"):
            observability.init_observability()

        exporter_cls.assert_not_called()
        processor_cls.assert_not_called()
        self.assertEqual(fake_provider.span_processors, [])
        set_provider.assert_called_once_with(fake_provider)
        self.assertTrue(observability._INITIALIZED)


if __name__ == "__main__":
    unittest.main()
