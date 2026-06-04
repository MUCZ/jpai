"""Observability: unified OpenTelemetry setup for traces, metrics, and logs."""

from __future__ import annotations

import logging
import os
import copy
import time
import functools
import asyncio
from hashlib import blake2b
from contextlib import contextmanager
from typing import Iterator, Callable

import structlog

from opentelemetry import trace, metrics
from opentelemetry.trace import Status, StatusCode
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

from src.models import Outcome, Priority
from src.config import METRICS_TENANT_BUCKET_COUNT, METRICS_TENANT_LABEL_MODE, LOG_LEVEL

_INITIALIZED = False


# ── Resource (shared by all signals) ──────────────────────────
def _build_resource() -> Resource:
    return Resource.create(
        {
            SERVICE_NAME: os.getenv("OTEL_SERVICE_NAME", "agent-service"),
            "service.version": os.getenv("SERVICE_VERSION", "dev"),
            "deployment.environment": os.getenv("DEPLOYMENT_ENV", "development"),
        }
    )


# ── Init (one function, all three signals) ────────────────────
def init_observability() -> None:
    """Initialize tracing, metrics, and logging. Call once at startup."""
    global _INITIALIZED
    if _INITIALIZED:
        return

    resource = _build_resource()
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

    # 1. Tracing
    tracer_provider = TracerProvider(resource=resource)
    if endpoint:
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
    trace.set_tracer_provider(tracer_provider)

    # 2. Metrics (Push to OTLP + Expose for Prometheus)
    readers = [PrometheusMetricReader()]
    if endpoint:
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint),
                export_interval_millis=5000,
            )
        )
        
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
    # User requested buckets: [0.1, 0.5, 1, 2, 5, 10, 20, 30, 45]
    # Because http.server.duration is recorded in milliseconds by default, 
    # we convert these seconds into milliseconds.
    http_duration_view = View(
        instrument_name="http.server.duration",
        aggregation=ExplicitBucketHistogramAggregation(
            boundaries=(100.0, 500.0, 1000.0, 2000.0, 5000.0, 10000.0, 20000.0, 29000.0, 30000.0, 31000.0, 32000.0, 40000.0, 45000.0, 60000.0)
        ),
    )
    
    meter_provider = MeterProvider(
        resource=resource, 
        metric_readers=readers,
        views=[http_duration_view]
    )
    metrics.set_meter_provider(meter_provider)

    # 3. Logging (structlog → stdlib → OTEL handler → Loki)
    _setup_logging(resource, endpoint)

    # 4. Auto-instrumentation
    HTTPXClientInstrumentor().instrument()

    _INITIALIZED = True


class OTelLoggingHandler(LoggingHandler):
    """Custom OTel LoggingHandler to copy LogRecord and sanitize non-primitive attributes

    This prevents warnings/errors during attribute serialization and avoids side effects
    on other standard library handlers (like the console formatter).
    """
    def emit(self, record: logging.LogRecord) -> None:
        # Create a shallow copy of the LogRecord to prevent attribute mutations
        # from leaking to other handlers sharing the same LogRecord instance by reference.
        record_copy = copy.copy(record)
        if hasattr(record_copy, "_logger"):
            delattr(record_copy, "_logger")
        super().emit(record_copy)


# ── Structured logging setup ──────────────────────────────────
def _setup_logging(resource: Resource, endpoint: str | None) -> None:
    """Configure structlog + OTEL log bridge."""
    # OTEL log provider → sends logs to Collector → Loki
    log_provider = LoggerProvider(resource=resource)
    if endpoint:
        log_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint))
        )
    set_logger_provider(log_provider)

    # Bridge: stdlib logging → OTEL
    otel_handler = OTelLoggingHandler(logger_provider=log_provider)
    
    # Configure logger levels using standard hierarchy (Approach 1):
    # Set the root logger level to INFO (or higher) to suppress third-party debug logs,
    # and configure the 'src' package logger to the application's LOG_LEVEL.
    app_level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    root_level = max(logging.INFO, app_level)
    
    root = logging.getLogger()
    root.addHandler(otel_handler)
    root.setLevel(root_level)
    
    logging.getLogger("src").setLevel(app_level)

    # structlog → stdlib (so logs flow through the OTEL handler)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Formatter for console output (also JSON, also goes to OTEL)
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[structlog.processors.JSONRenderer()],
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)


_meter = metrics.get_meter("agent-platform")


# Task
TASKS_TOTAL = _meter.create_counter(
    "agent.tasks", unit="1", description="Completed tasks by outcome"
)
TASK_DURATION = _meter.create_histogram(
    "agent.task.duration", unit="s", description="End-to-end task duration"
)
TASK_STAGE_DURATION = _meter.create_histogram(
    "agent.task.stage.duration", unit="s", description="Pipeline stage duration"
)
TASK_TIMEOUTS_TOTAL = _meter.create_counter(
    "agent.task.timeouts", unit="1", description="Task timeouts"
)
TASK_IN_PROGRESS = _meter.create_up_down_counter(
    "agent.task.in_progress", unit="1", description="Tasks currently executing"
)
TASK_QUEUE_WAIT = _meter.create_histogram(
    "agent.task.queue.wait", unit="s", description="Queue wait time"
)

# LLM
LLM_REQUESTS_TOTAL = _meter.create_counter(
    "agent.llm.requests", unit="1", description="LLM request attempts"
)
LLM_REQUEST_DURATION = _meter.create_histogram(
    "agent.llm.request.duration", unit="s", description="LLM request latency"
)
LLM_RETRIES_TOTAL = _meter.create_counter(
    "agent.llm.retries", unit="1", description="LLM retry attempts"
)
LLM_TOKENS_TOTAL = _meter.create_counter(
    "agent.llm.tokens", unit="1", description="LLM token usage"
)
LLM_COST_USD_TOTAL = _meter.create_counter(
    "agent.llm.cost.usd", unit="USD", description="Estimated LLM cost"
)
LLM_RATE_LIMIT_WAIT = _meter.create_histogram(
    "agent.llm.rate_limit.wait", unit="s", description="Rate limiter wait time"
)

# Tool
TOOL_CALLS_TOTAL = _meter.create_counter(
    "agent.tool.calls", unit="1", description="Tool call attempts"
)
TOOL_CALL_DURATION = _meter.create_histogram(
    "agent.tool.call.duration", unit="s", description="Tool call latency"
)

# Cache
CACHE_OPERATIONS_TOTAL = _meter.create_counter(
    "agent.cache.operations", unit="1", description="Cache lookups"
)
CACHE_ENTRIES = _meter.create_up_down_counter(
    "agent.cache.entries", unit="1", description="Current cache entries"
)


# ── Helpers (public API stays similar) ────────────────────────


def render_metrics() -> tuple[bytes, str]:
    """Render metrics in Prometheus text format."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def get_tracer():
    return trace.get_tracer("agent-platform")


def current_trace_id() -> str:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else ""


def current_span_id() -> str:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.span_id, "016x") if ctx.is_valid else ""


def metric_tenant_label(tenant_id: str) -> str:
    if METRICS_TENANT_LABEL_MODE == "direct":
        return tenant_id
    if METRICS_TENANT_BUCKET_COUNT <= 0:
        return "bucket-0"
    digest = blake2b(tenant_id.encode(), digest_size=4).hexdigest()
    return f"bucket-{int(digest, 16) % METRICS_TENANT_BUCKET_COUNT:02d}"


@contextmanager
def bind_context(**values: str) -> Iterator[None]:
    bound = {k: str(v) for k, v in values.items() if v is not None}
    with structlog.contextvars.bound_contextvars(**bound):
        yield


def update_trace_context() -> None:
    structlog.contextvars.bind_contextvars(
        trace_id=current_trace_id(),
        span_id=current_span_id(),
    )


class StageState:
    def __init__(self):
        self.outcome = Outcome.SUCCESS
        self.error: Exception | str | None = None

    def set_error(self, err: Exception | str):
        self.outcome = Outcome.FAILURE
        self.error = err


@contextmanager
def observe_stage(
    stage_name: str, tenant_id: str, priority: Priority
) -> Iterator[StageState]:
    tracer = get_tracer()
    mt = metric_tenant_label(tenant_id)
    state = StageState()
    attrs = {"tenant_id": mt, "priority": priority.value, "stage": stage_name}

    with tracer.start_as_current_span(f"task.{stage_name}") as span, bind_context(
        stage=stage_name, operation=stage_name
    ):
        started = time.perf_counter()
        try:
            yield state
            if state.error:
                span.set_status(Status(StatusCode.ERROR, str(state.error)))
        except Exception as e:
            state.outcome = Outcome.FAILURE
            span.set_status(Status(StatusCode.ERROR, str(e)))
            structlog.get_logger(__name__).exception(
                "stage.exception", stage=stage_name
            )
            raise
        finally:
            dur = time.perf_counter() - started
            TASK_STAGE_DURATION.record(dur, {**attrs, "outcome": state.outcome.value})
            log = structlog.get_logger(__name__)
            if state.outcome == Outcome.SUCCESS:
                log.info(
                    "stage.completed",
                    stage=stage_name,
                    outcome=state.outcome.value,
                    duration_ms=round(dur * 1000, 2),
                )
            elif state.error:
                log.error(
                    "stage.failed",
                    stage=stage_name,
                    outcome=state.outcome.value,
                    duration_ms=round(dur * 1000, 2),
                    error=str(state.error),
                )


def observe_tool(func: Callable) -> Callable:
    @functools.wraps(func)
    async def wrapper(
        tool_name: str, args: dict, *, tenant_id: str, priority: Priority
    ) -> dict:
        tracer = get_tracer()
        mt = metric_tenant_label(tenant_id)
        attrs = {"tenant_id": mt, "priority": priority.value, "tool_name": tool_name}

        with tracer.start_as_current_span(f"tool.{tool_name}") as span, bind_context(
            operation=tool_name
        ):
            started = time.perf_counter()
            outcome = Outcome.UNKNOWN
            log = structlog.get_logger(__name__)
            try:
                result = await func(
                    tool_name, args, tenant_id=tenant_id, priority=priority
                )
                outcome = Outcome.SUCCESS
                return result
            except asyncio.CancelledError:
                outcome = Outcome.CANCELLED
                span.set_status(Status(StatusCode.ERROR, "cancelled"))
                log.warning("tool.cancelled", tool_name=tool_name)
                raise
            except Exception as e:
                outcome = Outcome.FAILURE
                span.set_status(Status(StatusCode.ERROR, str(e)))
                log.exception("tool.exception", tool_name=tool_name)
                raise
            finally:
                dur = time.perf_counter() - started
                TOOL_CALLS_TOTAL.add(1, {**attrs, "outcome": outcome.value})
                TOOL_CALL_DURATION.record(dur, {**attrs, "outcome": outcome.value})
                log.info(
                    "tool.completed",
                    tool_name=tool_name,
                    outcome=outcome.value,
                    duration_ms=round(dur * 1000, 2),
                )

    return wrapper
