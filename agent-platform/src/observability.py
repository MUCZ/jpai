"""Observability primitives for tracing, metrics, and structured logging."""

from __future__ import annotations


import os
from hashlib import blake2b
import time
import functools
import asyncio
from contextlib import contextmanager
from typing import Iterator, Callable

import structlog

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from src.models import Outcome, Priority
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from src.config import METRICS_TENANT_BUCKET_COUNT, METRICS_TENANT_LABEL_MODE

_LOGGING_INITIALIZED = False
_TRACING_INITIALIZED = False

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS_TOTAL = Counter(
    "agent_http_requests_total",
    "Total HTTP requests served by the API.",
    ("method", "path", "status_code"),
    registry=REGISTRY,
)
HTTP_REQUEST_DURATION = Histogram(
    "agent_http_request_duration_seconds",
    "HTTP request latency.",
    ("method", "path", "status_code"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    registry=REGISTRY,
)
HTTP_IN_FLIGHT = Gauge(
    "agent_http_in_flight_requests",
    "Current number of in-flight HTTP requests.",
    registry=REGISTRY,
)

TASKS_TOTAL = Counter(
    "agent_tasks_total",
    "Completed task requests by outcome.",
    ("tenant_id", "priority", "status", "source"),
    registry=REGISTRY,
)
TASK_DURATION = Histogram(
    "agent_task_duration_seconds",
    "End-to-end task duration.",
    ("tenant_id", "priority", "status", "source"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    registry=REGISTRY,
)
TASK_STAGE_DURATION = Histogram(
    "agent_task_stage_duration_seconds",
    "Task pipeline stage duration.",
    ("tenant_id", "priority", "stage", "outcome"),
    buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    registry=REGISTRY,
)
TASK_TIMEOUTS_TOTAL = Counter(
    "agent_task_timeouts_total",
    "Task timeouts.",
    ("tenant_id", "priority"),
    registry=REGISTRY,
)
TASK_IN_PROGRESS = Gauge(
    "agent_task_in_progress",
    "Tasks currently executing.",
    ("tenant_id", "priority"),
    registry=REGISTRY,
)
TASK_QUEUE_WAIT = Histogram(
    "agent_task_queue_wait_seconds",
    "Time spent waiting on task execution queues.",
    ("tenant_id", "priority", "queue"),
    buckets=(0.0005, 0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
    registry=REGISTRY,
)

LLM_REQUESTS_TOTAL = Counter(
    "agent_llm_requests_total",
    "LLM request attempts.",
    ("tenant_id", "priority", "operation", "outcome"),
    registry=REGISTRY,
)
LLM_REQUEST_DURATION = Histogram(
    "agent_llm_request_duration_seconds",
    "LLM request latency.",
    ("tenant_id", "priority", "operation", "outcome"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    registry=REGISTRY,
)
LLM_RETRIES_TOTAL = Counter(
    "agent_llm_retries_total",
    "LLM retry attempts.",
    ("tenant_id", "priority", "operation", "reason"),
    registry=REGISTRY,
)
LLM_TOKENS_TOTAL = Counter(
    "agent_llm_tokens_total",
    "LLM token usage.",
    ("tenant_id", "priority", "operation", "token_type"),
    registry=REGISTRY,
)
LLM_COST_USD_TOTAL = Counter(
    "agent_llm_estimated_cost_usd_total",
    "Estimated LLM cost in USD.",
    ("tenant_id", "priority", "operation"),
    registry=REGISTRY,
)
LLM_RATE_LIMIT_WAIT = Histogram(
    "agent_llm_rate_limit_wait_seconds",
    "Time spent waiting on the LLM rate limiter.",
    buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
    registry=REGISTRY,
)

TOOL_CALLS_TOTAL = Counter(
    "agent_tool_calls_total",
    "Tool call attempts.",
    ("tenant_id", "priority", "tool_name", "outcome"),
    registry=REGISTRY,
)
TOOL_CALL_DURATION = Histogram(
    "agent_tool_call_duration_seconds",
    "Tool call latency.",
    ("tenant_id", "priority", "tool_name", "outcome"),
    buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
    registry=REGISTRY,
)

CACHE_OPERATIONS_TOTAL = Counter(
    "agent_cache_operations_total",
    "Cache lookups by result.",
    ("result",),
    registry=REGISTRY,
)
CACHE_ENTRIES = Gauge(
    "agent_cache_entries",
    "Current number of entries in the response cache.",
    registry=REGISTRY,
)


def setup_logging() -> None:
    """Configure root logging once with JSON output using structlog."""
    global _LOGGING_INITIALIZED
    if _LOGGING_INITIALIZED:
        return

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _LOGGING_INITIALIZED = True


def _parse_otlp_headers(value: str) -> dict[str, str]:
    """Parse OTLP headers from comma-separated key=value pairs."""
    if not value.strip():
        return {}

    headers: dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue

        key, separator, raw_value = item.partition("=")
        if not separator:
            continue

        headers[key.strip()] = raw_value.strip()
    return headers


def setup_tracing() -> None:
    """Initialize tracing with optional OTLP export."""
    global _TRACING_INITIALIZED
    if _TRACING_INITIALIZED:
        return

    resource = Resource.create(
        {SERVICE_NAME: os.getenv("OTEL_SERVICE_NAME", "agent-service")}
    )
    provider = TracerProvider(resource=resource)

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers=_parse_otlp_headers(os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")),
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)
    _TRACING_INITIALIZED = True


def init_observability() -> None:
    """Initialize logging and tracing."""
    setup_logging()
    setup_tracing()


def render_metrics() -> tuple[bytes, str]:
    """Render metrics in Prometheus text format."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def metric_tenant_label(tenant_id: str) -> str:
    """Return a bounded-cardinality tenant label for metrics."""
    if METRICS_TENANT_LABEL_MODE == "direct":
        return tenant_id

    if METRICS_TENANT_BUCKET_COUNT <= 0:
        return "bucket-0"

    digest = blake2b(tenant_id.encode("utf-8"), digest_size=4).hexdigest()
    bucket = int(digest, 16) % METRICS_TENANT_BUCKET_COUNT
    return f"bucket-{bucket:02d}"


def get_tracer():
    """Get the application tracer."""
    return trace.get_tracer("agent-platform")


def current_trace_id() -> str:
    """Return the current trace ID in W3C hex format."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return ""
    return format(span_context.trace_id, "032x")


def current_span_id() -> str:
    """Return the current span ID in W3C hex format."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return ""
    return format(span_context.span_id, "016x")


@contextmanager
def bind_context(**values: str) -> Iterator[None]:
    """Bind request context to logs within the current execution flow."""
    # Only bind non-None values
    bound_values = {k: str(v) for k, v in values.items() if v is not None}
    with structlog.contextvars.bound_contextvars(**bound_values):
        yield


def update_trace_context() -> None:
    """Refresh trace and span IDs from the active span."""
    structlog.contextvars.bind_contextvars(
        trace_id=current_trace_id(),
        span_id=current_span_id()
    )


class StageState:
    """State object for stage observability context."""
    def __init__(self):
        self.outcome = Outcome.SUCCESS
        self.error: Exception | str | None = None

    def set_error(self, err: Exception | str):
        self.outcome = Outcome.FAILURE
        self.error = err


@contextmanager
def observe_stage(stage_name: str, tenant_id: str, priority: Priority) -> Iterator[StageState]:
    """Context manager for tracing and metrics of a pipeline stage."""
    tracer = get_tracer()
    metric_tenant = metric_tenant_label(tenant_id)
    state = StageState()
    
    with tracer.start_as_current_span(f"task.{stage_name}") as span, bind_context(
        stage=stage_name, operation=stage_name
    ):
        started = time.perf_counter()
        try:
            yield state
            if state.error:
                span.set_status(Status(StatusCode.ERROR, description=str(state.error)))
        except Exception as e:
            state.outcome = Outcome.FAILURE
            span.set_status(Status(StatusCode.ERROR, description=str(e)))
            structlog.get_logger(__name__).exception("stage.exception", stage=stage_name)
            raise
        finally:
            duration = time.perf_counter() - started
            TASK_STAGE_DURATION.labels(
                tenant_id=metric_tenant,
                priority=priority.value,
                stage=stage_name,
                outcome=state.outcome.value,
            ).observe(duration)
            
            logger = structlog.get_logger(__name__)
            if state.outcome == Outcome.SUCCESS:
                logger.info(
                    "stage.completed",
                    stage=stage_name,
                    outcome=state.outcome.value,
                    duration_ms=round(duration * 1000, 2),
                )
            elif state.error:
                # If there was an exception, we already logged it in except block.
                # If error was set but no exception was raised, log error here.
                logger.error(
                    "stage.failed",
                    stage=stage_name,
                    outcome=state.outcome.value,
                    duration_ms=round(duration * 1000, 2),
                    error=str(state.error),
                )


def observe_tool(func: Callable) -> Callable:
    """Decorator to trace and monitor tool execution."""
    @functools.wraps(func)
    async def wrapper(tool_name: str, args: dict, *, tenant_id: str, priority: Priority) -> dict:
        tracer = get_tracer()
        metric_tenant = metric_tenant_label(tenant_id)
        
        with tracer.start_as_current_span(f"tool.{tool_name}") as span, bind_context(
            operation=tool_name
        ):
            started = time.perf_counter()
            outcome = Outcome.UNKNOWN
            logger = structlog.get_logger(__name__)
            try:
                result = await func(tool_name, args, tenant_id=tenant_id, priority=priority)
                outcome = Outcome.SUCCESS
                return result
            except asyncio.CancelledError:
                outcome = Outcome.CANCELLED
                span.set_status(Status(StatusCode.ERROR, description="cancelled"))
                logger.warning("tool.cancelled", tool_name=tool_name)
                raise
            except Exception as e:
                outcome = Outcome.FAILURE
                span.set_status(Status(StatusCode.ERROR, description=str(e)))
                logger.exception("tool.exception", tool_name=tool_name)
                raise
            finally:
                duration = time.perf_counter() - started
                TOOL_CALLS_TOTAL.labels(
                    tenant_id=metric_tenant,
                    priority=priority.value,
                    tool_name=tool_name,
                    outcome=outcome.value,
                ).inc()
                TOOL_CALL_DURATION.labels(
                    tenant_id=metric_tenant,
                    priority=priority.value,
                    tool_name=tool_name,
                    outcome=outcome.value,
                ).observe(duration)
                logger.info(
                    "tool.completed",
                    tool_name=tool_name,
                    outcome=outcome.value,
                    duration_ms=round(duration * 1000, 2),
                )
    return wrapper
