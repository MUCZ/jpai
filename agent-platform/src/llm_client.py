"""LLM inference client.

Handles communication with the LLM inference server,
including retry logic with exponential backoff.
"""

import httpx
import asyncio
import structlog
import random
import time

from opentelemetry.trace import Status, StatusCode

from src.config import (
    LLM_SERVER_URL,
    RETRY_MAX_ATTEMPTS,
    RETRY_BASE_DELAY,
    RETRY_BACKOFF_FACTOR,
    LLM_RATE_LIMIT_RPS,
    LLM_RATE_LIMIT_BURST,
    PRIORITY_POLICIES,
    TASK_TIMEOUT_SECONDS,
    TOKEN_COST_PER_1K_INPUT,
    TOKEN_COST_PER_1K_OUTPUT,
)
from src.models import Outcome, Priority
from src.observability import (
    LLM_COST_USD_TOTAL,
    LLM_RATE_LIMIT_WAIT,
    LLM_REQUEST_DURATION,
    LLM_REQUESTS_TOTAL,
    LLM_RETRIES_TOTAL,
    LLM_TOKENS_TOTAL,
    bind_context,
    get_tracer,
    metric_tenant_label,
)

# Shared HTTP client (connection pooling)
_http_client: httpx.AsyncClient | None = None
logger = structlog.get_logger(__name__)
tracer = get_tracer()


def _get_client() -> httpx.AsyncClient:
    """Get or create the shared HTTP client."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=TASK_TIMEOUT_SECONDS,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _http_client


class _TokenBucket:
    """Rate limiter to protect the downstream LLM service from overload
    and prevent runaway inference costs during traffic spikes."""

    def __init__(self, rate: float, capacity: int):
        self._rate = rate
        self._capacity = capacity
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity,
                               self._tokens + elapsed * self._rate)
            self._last_refill = now
            while self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self._capacity,
                                   self._tokens + elapsed * self._rate)
                self._last_refill = now
            self._tokens -= 1


# Global rate limiter for LLM calls
_rate_limiter = _TokenBucket(rate=LLM_RATE_LIMIT_RPS, capacity=LLM_RATE_LIMIT_BURST)


def _priority_policy(priority: Priority) -> dict:
    return PRIORITY_POLICIES.get(
        priority.value,
        PRIORITY_POLICIES[Priority.NORMAL.value],
    )


async def call_llm(
    prompt: str,
    max_tokens: int = 512,
    *,
    operation: str,
    tenant_id: str,
    priority: Priority,
) -> dict:
    """Call the LLM inference endpoint with retry and exponential backoff.

    Returns a dict with keys: text, prompt_tokens, completion_tokens.
    On failure after all retries, returns dict with 'error' key.
    """
    client = _get_client()
    last_error = None
    last_status = None
    accumulated_tokens = 0
    policy = _priority_policy(priority)
    max_attempts = min(policy["llm_max_attempts"], RETRY_MAX_ATTEMPTS)
    attempt_timeout = policy["llm_attempt_timeout_seconds"]
    deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    labels = {
        "tenant_id": metric_tenant_label(tenant_id),
        "priority": priority.value,
        "operation": operation,
    }

    # Unified retry policy: all transient errors (500, 429, timeout)
    # use the same exponential backoff strategy for simplicity
    with tracer.start_as_current_span(
        f"llm.{operation}"
    ) as span, bind_context(operation=operation):
        span.set_attribute("llm.operation", operation)
        span.set_attribute("llm.max_tokens", max_tokens)
        span.set_attribute("llm.prompt_length", len(prompt))
        span.set_attribute("llm.attempt_timeout_seconds", attempt_timeout)
        span.set_attribute("llm.max_attempts", max_attempts)
        logger.debug(
            "llm.started",
            operation=operation,
            prompt_length=len(prompt),
            max_tokens=max_tokens,
            attempt_timeout_seconds=attempt_timeout,
            max_attempts=max_attempts,
        )

        for attempt in range(max_attempts):
            attempt_number = attempt + 1
            started = time.perf_counter()
            outcome = Outcome.ERROR
            status_code = "0"
            try:
                rl_start = time.perf_counter()
                await _rate_limiter.acquire()
                LLM_RATE_LIMIT_WAIT.record(time.perf_counter() - rl_start)
                response = await client.post(
                    f"{LLM_SERVER_URL}/v1/inference",
                    json={"prompt": prompt, "max_tokens": max_tokens},
                    timeout=attempt_timeout,
                )

                status_code = str(response.status_code)
                if response.status_code == 200:
                    data = response.json()
                    data["prompt_tokens"] = data.get("prompt_tokens", 0) + accumulated_tokens
                    outcome = Outcome.SUCCESS
                    duration = time.perf_counter() - started
                    LLM_REQUESTS_TOTAL.add(1, {**labels, "outcome": outcome.value})
                    LLM_REQUEST_DURATION.record(duration, {**labels, "outcome": outcome.value})
                    prompt_tokens = data.get("prompt_tokens", 0)
                    completion_tokens = data.get("completion_tokens", 0)
                    LLM_TOKENS_TOTAL.add(prompt_tokens, {**labels, "token_type": "prompt"})
                    LLM_TOKENS_TOTAL.add(completion_tokens, {**labels, "token_type": "completion"})
                    estimated_cost = (
                        (prompt_tokens / 1000) * TOKEN_COST_PER_1K_INPUT
                        + (completion_tokens / 1000) * TOKEN_COST_PER_1K_OUTPUT
                    )
                    LLM_COST_USD_TOTAL.add(estimated_cost, labels)
                    span.set_attribute("llm.prompt_tokens", prompt_tokens)
                    span.set_attribute("llm.completion_tokens", completion_tokens)
                    span.set_attribute("llm.estimated_cost_usd", estimated_cost)
                    span.add_event(
                        "llm.attempt",
                        {
                            "attempt": attempt_number,
                            "status_code": response.status_code,
                            "outcome": outcome,
                            "duration_ms": round(duration * 1000, 2),
                        },
                    )
                    logger.info(
                        "llm.completed",
                        operation=operation,
                        attempt=attempt_number,
                        status_code=response.status_code,
                        outcome=outcome.value,
                        duration_ms=round(duration * 1000, 2),
                    )
                    return data

                outcome = Outcome.FAILURE
                last_status = response.status_code
                last_error = f"LLM returned {response.status_code}"
                if response.status_code == 500:
                    accumulated_tokens += max(1, len(prompt.split()))

            except httpx.TimeoutException:
                outcome = Outcome.TIMEOUT
                last_error = "LLM request timed out"
                last_status = 408
                status_code = "408"
            except Exception as e:
                outcome = Outcome.ERROR
                last_error = str(e)
                last_status = 0
                status_code = "0"

            duration = time.perf_counter() - started
            LLM_REQUESTS_TOTAL.add(1, {**labels, "outcome": outcome.value})
            LLM_REQUEST_DURATION.record(duration, {**labels, "outcome": outcome.value})
            span.add_event(
                "llm.attempt",
                {
                    "attempt": attempt_number,
                    "status_code": status_code,
                    "outcome": outcome,
                    "duration_ms": round(duration * 1000, 2),
                },
            )
            logger.warning(
                "llm.attempt_failed",
                operation=operation,
                attempt=attempt_number,
                status_code=status_code,
                outcome=outcome.value,
                duration_ms=round(duration * 1000, 2),
            )

            if attempt < max_attempts - 1:
                delay = RETRY_BASE_DELAY * (RETRY_BACKOFF_FACTOR ** attempt)
                jitter = random.uniform(0, delay * 0.3)
                sleep_for = delay + jitter
                reason = "timeout" if status_code == "408" else f"status_{status_code}"
                remaining_budget = deadline - time.monotonic()
                if remaining_budget < sleep_for + min(attempt_timeout, TASK_TIMEOUT_SECONDS):
                    last_error = (
                        f"LLM retry skipped: insufficient request budget after {reason}"
                    )
                    span.add_event(
                        "llm.retry_skipped",
                        {
                            "attempt": attempt_number,
                            "reason": reason,
                            "remaining_budget_ms": round(remaining_budget * 1000, 2),
                        },
                    )
                    logger.info(
                        "llm.retry_skipped",
                        operation=operation,
                        attempt=attempt_number,
                        reason=reason,
                        remaining_budget_ms=round(remaining_budget * 1000, 2),
                    )
                    break
                LLM_RETRIES_TOTAL.add(1, {**labels, "reason": reason})
                span.add_event(
                    "llm.retry_scheduled",
                    {
                        "attempt": attempt_number,
                        "reason": reason,
                        "delay_ms": round(sleep_for * 1000, 2),
                    },
                )
                logger.info(
                    "llm.retry",
                    operation=operation,
                    attempt=attempt_number,
                    reason=reason,
                    delay_ms=round(sleep_for * 1000, 2),
                )
                await asyncio.sleep(sleep_for)

        span.set_status(Status(StatusCode.ERROR, description=last_error or "llm failure"))
        logger.error(
            "llm.failed",
            operation=operation,
            status_code=last_status,
            error=last_error,
        )
        return {
            "error": last_error,
            "text": "",
            "prompt_tokens": accumulated_tokens,
            "completion_tokens": 0,
            "status_code": last_status,
        }
