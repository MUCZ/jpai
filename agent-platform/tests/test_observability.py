"""Observability tests."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from src import llm_client, main, orchestrator
from src.models import Priority
from src.observability import metric_tenant_label


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)

    async def post(self, url: str, json: dict) -> _FakeResponse:
        return self._responses.pop(0)


class ObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        main.task_store.clear()
        main._response_cache.clear()
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        self.client.close()

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

    def test_metrics_endpoint_exposes_expected_metrics(self) -> None:
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("agent_http_requests_total", response.text)
        self.assertIn("agent_tasks_total", response.text)
        self.assertIn("agent_llm_requests_total", response.text)
        self.assertIn("agent_tool_calls_total", response.text)
        self.assertIn("X-Trace-Id", response.headers)

    def test_successful_task_has_trace_header_and_updates_metrics(self) -> None:
        async def fake_call_llm(prompt: str, max_tokens: int, *, operation: str, tenant_id: str, priority: Priority) -> dict:
            return {
                "text": f"{operation}-ok",
                "prompt_tokens": 10,
                "completion_tokens": 5,
            }

        async def fake_execute_tools(tools, *, tenant_id: str, priority: Priority):
            return [{"tool": tool_name, "status": "success", "output": "ok"} for tool_name, _ in tools]

        payload = {
            "task_description": "Summarise customer feedback",
            "tenant_id": "tenant-alpha",
            "priority": "normal",
        }
        metric_tenant = metric_tenant_label(payload["tenant_id"])
        before_completed = self._metric_value(
            "agent_tasks_total",
            {
                "tenant_id": metric_tenant,
                "priority": "normal",
                "status": "completed",
                "source": "fresh",
            },
        )
        before_plan_stage = self._metric_value(
            "agent_task_stage_duration_seconds_count",
            {
                "tenant_id": metric_tenant,
                "priority": "normal",
                "stage": "plan",
                "outcome": "success",
            },
        )

        with patch.object(orchestrator, "call_llm", side_effect=fake_call_llm), patch.object(
            orchestrator, "execute_tools", side_effect=fake_execute_tools
        ):
            response = self.client.post("/tasks", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual(len(response.headers["X-Trace-Id"]), 32)
        self.assertGreater(
            self._metric_value(
                "agent_tasks_total",
                {
                    "tenant_id": metric_tenant,
                    "priority": "normal",
                    "status": "completed",
                    "source": "fresh",
                },
            ),
            before_completed,
        )
        self.assertGreater(
            self._metric_value(
                "agent_task_stage_duration_seconds_count",
                {
                    "tenant_id": metric_tenant,
                    "priority": "normal",
                    "stage": "plan",
                    "outcome": "success",
                },
            ),
            before_plan_stage,
        )

    def test_cache_hit_records_cache_metrics(self) -> None:
        calls = {"llm": 0, "tools": 0}

        async def fake_call_llm(prompt: str, max_tokens: int, *, operation: str, tenant_id: str, priority: Priority) -> dict:
            calls["llm"] += 1
            return {
                "text": f"{operation}-ok",
                "prompt_tokens": 10,
                "completion_tokens": 5,
            }

        async def fake_execute_tools(tools, *, tenant_id: str, priority: Priority):
            calls["tools"] += 1
            return [{"tool": tool_name, "status": "success", "output": "ok"} for tool_name, _ in tools]

        payload = {
            "task_description": "Summarise customer feedback",
            "tenant_id": "tenant-cache",
            "priority": "normal",
        }
        metric_tenant = metric_tenant_label(payload["tenant_id"])
        before_cache = self._metric_value(
            "agent_tasks_total",
            {
                "tenant_id": metric_tenant,
                "priority": "normal",
                "status": "completed",
                "source": "cache",
            },
        )

        with patch.object(orchestrator, "call_llm", side_effect=fake_call_llm), patch.object(
            orchestrator, "execute_tools", side_effect=fake_execute_tools
        ):
            first = self.client.post("/tasks", json=payload)
            second = self.client.post("/tasks", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(calls["llm"], 3)
        self.assertEqual(calls["tools"], 1)
        self.assertGreater(
            self._metric_value(
                "agent_tasks_total",
                {
                    "tenant_id": metric_tenant,
                    "priority": "normal",
                    "status": "completed",
                    "source": "cache",
                },
            ),
            before_cache,
        )

    def test_llm_retry_metrics_are_recorded(self) -> None:
        metric_tenant = metric_tenant_label("tenant-retry")
        before_retry = self._metric_value(
            "agent_llm_retries_total",
            {
                "tenant_id": metric_tenant,
                "priority": "normal",
                "operation": "plan",
                "reason": "status_500",
            },
        )
        before_success = self._metric_value(
            "agent_llm_requests_total",
            {
                "tenant_id": metric_tenant,
                "priority": "normal",
                "operation": "plan",
                "outcome": "success",
            },
        )

        fake_client = _FakeAsyncClient(
            [
                _FakeResponse(500),
                _FakeResponse(
                    200,
                    {"text": "ok", "prompt_tokens": 7, "completion_tokens": 3},
                ),
            ]
        )

        with patch.object(llm_client, "_get_client", return_value=fake_client), patch.object(
            llm_client, "RETRY_MAX_ATTEMPTS", 2
        ), patch.object(llm_client, "RETRY_BASE_DELAY", 0), patch.object(
            llm_client.random, "uniform", return_value=0
        ):
            result = asyncio.run(
                llm_client.call_llm(
                    "Plan the task",
                    max_tokens=64,
                    operation="plan",
                    tenant_id="tenant-retry",
                    priority=Priority.NORMAL,
                )
            )

        self.assertEqual(result["text"], "ok")
        self.assertGreater(
            self._metric_value(
                "agent_llm_retries_total",
                {
                    "tenant_id": metric_tenant,
                    "priority": "normal",
                    "operation": "plan",
                    "reason": "status_500",
                },
            ),
            before_retry,
        )
        self.assertGreater(
            self._metric_value(
                "agent_llm_requests_total",
                {
                    "tenant_id": metric_tenant,
                    "priority": "normal",
                    "operation": "plan",
                    "outcome": "success",
                },
            ),
            before_success,
        )

    def test_timeout_path_records_timeout_metric(self) -> None:
        async def slow_run_task(*args, **kwargs):
            await asyncio.sleep(0.05)
            raise AssertionError("wait_for should time out before completion")

        metric_tenant = metric_tenant_label("tenant-timeout")
        before_timeouts = self._metric_value(
            "agent_task_timeouts_total",
            {"tenant_id": metric_tenant, "priority": "normal"},
        )

        with patch.object(main, "run_task", side_effect=slow_run_task), patch.object(
            main, "TASK_TIMEOUT_SECONDS", 0.01
        ):
            response = self.client.post(
                "/tasks",
                json={
                    "task_description": "Run long task",
                    "tenant_id": "tenant-timeout",
                    "priority": "normal",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertGreater(
            self._metric_value(
                "agent_task_timeouts_total",
                {"tenant_id": metric_tenant, "priority": "normal"},
            ),
            before_timeouts,
        )

    def test_not_found_response_has_trace_header(self) -> None:
        response = self.client.get("/tasks/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(response.headers["X-Trace-Id"]), 32)

    def test_summary_failure_marks_task_failed(self) -> None:
        async def fake_call_llm(prompt: str, max_tokens: int, *, operation: str, tenant_id: str, priority: Priority) -> dict:
            if operation == "summarise":
                return {
                    "error": "LLM request timed out",
                    "text": "",
                    "prompt_tokens": 3,
                    "completion_tokens": 0,
                }
            return {
                "text": f"{operation}-ok",
                "prompt_tokens": 5,
                "completion_tokens": 2,
            }

        async def fake_execute_tools(tools, *, tenant_id: str, priority: Priority):
            return [{"tool": tool_name, "status": "success", "output": "ok"} for tool_name, _ in tools]

        with patch.object(orchestrator, "call_llm", side_effect=fake_call_llm), patch.object(
            orchestrator, "execute_tools", side_effect=fake_execute_tools
        ):
            response = self.client.post(
                "/tasks",
                json={
                    "task_description": "Summarise customer feedback",
                    "tenant_id": "tenant-summary-fail",
                    "priority": "normal",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(response.json()["error"], "LLM request timed out")

    def test_validation_failure_marks_task_failed(self) -> None:
        async def fake_call_llm(prompt: str, max_tokens: int, *, operation: str, tenant_id: str, priority: Priority) -> dict:
            if operation == "validate":
                return {
                    "error": "validation unavailable",
                    "text": "",
                    "prompt_tokens": 3,
                    "completion_tokens": 0,
                }
            return {
                "text": f"{operation}-ok",
                "prompt_tokens": 5,
                "completion_tokens": 2,
            }

        async def fake_execute_tools(tools, *, tenant_id: str, priority: Priority):
            return [{"tool": tool_name, "status": "success", "output": "ok"} for tool_name, _ in tools]

        with patch.object(orchestrator, "call_llm", side_effect=fake_call_llm), patch.object(
            orchestrator, "execute_tools", side_effect=fake_execute_tools
        ):
            response = self.client.post(
                "/tasks",
                json={
                    "task_description": "Summarise customer feedback",
                    "tenant_id": "tenant-validation-fail",
                    "priority": "normal",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(response.json()["error"], "validation unavailable")

    def test_response_cache_is_bounded(self) -> None:
        async def fake_call_llm(prompt: str, max_tokens: int, *, operation: str, tenant_id: str, priority: Priority) -> dict:
            return {
                "text": f"{operation}-ok",
                "prompt_tokens": 5,
                "completion_tokens": 2,
            }

        async def fake_execute_tools(tools, *, tenant_id: str, priority: Priority):
            return [{"tool": tool_name, "status": "success", "output": "ok"} for tool_name, _ in tools]

        with patch.object(orchestrator, "call_llm", side_effect=fake_call_llm), patch.object(
            orchestrator, "execute_tools", side_effect=fake_execute_tools
        ), patch.object(main, "RESPONSE_CACHE_MAX_ENTRIES", 2):
            for idx in range(3):
                response = self.client.post(
                    "/tasks",
                    json={
                        "task_description": f"task-{idx}",
                        "tenant_id": "tenant-cache-bound",
                        "priority": "normal",
                    },
                )
                self.assertEqual(response.status_code, 200)

        self.assertEqual(len(main._response_cache), 2)


if __name__ == "__main__":
    unittest.main()
