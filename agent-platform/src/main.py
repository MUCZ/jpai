"""FastAPI application — Agent Execution Service."""

import asyncio
import structlog
import time
import uuid
from collections import OrderedDict
from time import perf_counter

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel
from typing import Optional
from src.models import Priority, TaskStatus, TaskResult
from src.orchestrator import run_task
from src.config import (
    MAX_CONCURRENT_TASKS,
    RESPONSE_CACHE_MAX_ENTRIES,
    RESPONSE_CACHE_TTL_SECONDS,
    TASK_TIMEOUT_SECONDS,
)
from src.observability import (
    CACHE_ENTRIES,
    CACHE_OPERATIONS_TOTAL,
    HTTP_IN_FLIGHT,
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS_TOTAL,
    TASK_DURATION,
    TASK_IN_PROGRESS,
    TASK_QUEUE_WAIT,
    TASKS_TOTAL,
    TASK_TIMEOUTS_TOTAL,
    bind_context,
    current_trace_id,
    get_tracer,
    init_observability,
    metric_tenant_label,
    render_metrics,
    update_trace_context,
)

app = FastAPI(title="Agent Execution Service")
init_observability()

logger = structlog.get_logger(__name__)
tracer = get_tracer()

# Task storage
task_store: dict[str, TaskResult] = {}

# Response cache for repeated queries — avoids redundant LLM calls
_response_cache: OrderedDict[str, dict] = OrderedDict()

# Limit concurrent task executions to protect downstream LLM service
_task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

# Ensure tasks for the same tenant execute in submission order
# to prevent race conditions on downstream tenant state
_tenant_locks: dict[str, asyncio.Lock] = {}


class CreateTaskBody(BaseModel):
    task_description: str
    tenant_id: str
    priority: Priority = Priority.NORMAL


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    tenant_id: str
    priority: Priority
    result: Optional[str] = None
    error: Optional[str] = None
    token_usage: Optional[dict] = None
    created_at: Optional[float] = None
    completed_at: Optional[float] = None


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path_format", request.url.path)


# Paths exempt from INFO-level request logs (probes, metrics scrapes)
_QUIET_PATHS = frozenset({"/health", "/metrics"})


def _cache_get(cache_key: str) -> Optional[dict]:
    cached = _response_cache.get(cache_key)
    if cached is None:
        CACHE_OPERATIONS_TOTAL.labels(result="miss").inc()
        return None

    if time.time() - cached["cached_at"] > RESPONSE_CACHE_TTL_SECONDS:
        _response_cache.pop(cache_key, None)
        CACHE_ENTRIES.set(len(_response_cache))
        CACHE_OPERATIONS_TOTAL.labels(result="expired").inc()
        return None

    _response_cache.move_to_end(cache_key)
    CACHE_OPERATIONS_TOTAL.labels(result="hit").inc()
    return cached


def _cache_set(cache_key: str, result: str) -> None:
    _response_cache[cache_key] = {"result": result, "cached_at": time.time()}
    _response_cache.move_to_end(cache_key)
    while len(_response_cache) > RESPONSE_CACHE_MAX_ENTRIES:
        _response_cache.popitem(last=False)
    CACHE_ENTRIES.set(len(_response_cache))


def _record_task_metrics(
    tenant: str, priority: str, status: str, source: str, duration: float,
) -> None:
    """Bundle the counter + histogram updates that every task completion emits."""
    TASKS_TOTAL.labels(tenant_id=tenant, priority=priority, status=status, source=source).inc()
    TASK_DURATION.labels(tenant_id=tenant, priority=priority, status=status, source=source).observe(duration)


@app.middleware("http")
async def observe_requests(request: Request, call_next):
    method = request.method
    start = perf_counter()
    HTTP_IN_FLIGHT.inc()

    with tracer.start_as_current_span(
        f"{method} {request.url.path}",
        context=propagate.extract(request.headers),
        kind=SpanKind.SERVER,
    ) as span:
        update_trace_context()
        span.set_attribute("http.method", method)
        span.set_attribute("http.target", request.url.path)

        logger.debug(
            "request.started",
            path=request.url.path,
            method=method,
        )

        try:
            response = await call_next(request)
        except Exception as exc:
            duration = perf_counter() - start
            path = _route_label(request)
            span.set_attribute("http.route", path)
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status_code="500").inc()
            HTTP_REQUEST_DURATION.labels(
                method=method, path=path, status_code="500"
            ).observe(duration)
            logger.exception(
                "request.failed",
                path=path,
                method=method,
                duration_ms=round(duration * 1000, 2),
            )
            response = JSONResponse(
                status_code=500,
                content={"detail": "Internal Server Error"},
            )
        else:
            duration = perf_counter() - start
            path = _route_label(request)
            span.set_attribute("http.route", path)
            status_code = str(response.status_code)
            if response.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR))
            HTTP_REQUESTS_TOTAL.labels(
                method=method, path=path, status_code=status_code
            ).inc()
            HTTP_REQUEST_DURATION.labels(
                method=method, path=path, status_code=status_code
            ).observe(duration)
            if path not in _QUIET_PATHS:
                logger.info(
                    "request.completed",
                    path=path,
                    method=method,
                    status_code=response.status_code,
                    duration_ms=round(duration * 1000, 2),
                )
        finally:
            HTTP_IN_FLIGHT.dec()

        response.headers["X-Trace-Id"] = current_trace_id()
        return response


@app.post("/tasks", response_model=TaskResponse)
async def create_task(body: CreateTaskBody):
    task_id = str(uuid.uuid4())
    request_started = perf_counter()

    # Cache key: tenant + description (priority excluded because
    # task results are priority-independent in the current design)
    cache_key = f"{body.tenant_id}:{body.task_description}"
    metric_tenant = metric_tenant_label(body.tenant_id)
    with bind_context(
        task_id=task_id,
        tenant_id=body.tenant_id,
        priority=body.priority.value,
    ):
        span = trace.get_current_span()
        span.set_attribute("task.id", task_id)
        span.set_attribute("tenant.id", body.tenant_id)
        span.set_attribute("task.priority", body.priority.value)
        span.add_event("task.cache_lookup")
        logger.info("task.accepted", task_description=body.task_description[:200])

        cached = _cache_get(cache_key)
        if cached is not None:
            result = TaskResult(
                task_id=task_id,
                status=TaskStatus.COMPLETED,
                tenant_id=body.tenant_id,
                priority=body.priority,
                result=cached.get("result"),
                token_usage={"prompt_tokens": 0, "completion_tokens": 0},
                created_at=time.time(),
                completed_at=time.time(),
            )
            task_store[task_id] = result
            span.add_event("task.cache_hit")
            _record_task_metrics(
                metric_tenant, body.priority.value,
                result.status.value, "cache",
                max(result.completed_at - result.created_at, 0.0),
            )
            logger.info(
                "task.cached",
                status=result.status.value,
            )
            return _to_response(result)

        task_store[task_id] = TaskResult(
            task_id=task_id,
            status=TaskStatus.PENDING,
            tenant_id=body.tenant_id,
            priority=body.priority,
        )
        span.add_event("task.cache_miss")

        async def _guarded_execute():
            lock = _tenant_locks.setdefault(body.tenant_id, asyncio.Lock())

            tenant_wait_started = perf_counter()
            with tracer.start_as_current_span("task.wait_tenant_lock"):
                await lock.acquire()
            tenant_wait = perf_counter() - tenant_wait_started
            TASK_QUEUE_WAIT.labels(
                tenant_id=metric_tenant,
                priority=body.priority.value,
                queue="tenant_lock",
            ).observe(tenant_wait)
            logger.debug(
                "task.lock_acquired",
                queue="tenant_lock",
                wait_ms=round(tenant_wait * 1000, 2),
            )

            try:
                semaphore_wait_started = perf_counter()
                with tracer.start_as_current_span("task.wait_global_concurrency"):
                    await _task_semaphore.acquire()
                semaphore_wait = perf_counter() - semaphore_wait_started
                TASK_QUEUE_WAIT.labels(
                    tenant_id=metric_tenant,
                    priority=body.priority.value,
                    queue="global_concurrency",
                ).observe(semaphore_wait)
                logger.debug(
                    "task.concurrency_acquired",
                    queue="global_concurrency",
                    wait_ms=round(semaphore_wait * 1000, 2),
                )

                try:
                    task_store[task_id].status = TaskStatus.RUNNING
                    return await run_task(
                        task_id=task_id,
                        description=body.task_description,
                        tenant_id=body.tenant_id,
                        priority=body.priority,
                    )
                finally:
                    _task_semaphore.release()
            finally:
                lock.release()

        TASK_IN_PROGRESS.labels(
            tenant_id=metric_tenant, priority=body.priority.value
        ).inc()
        try:
            result = await asyncio.wait_for(
                _guarded_execute(), timeout=TASK_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            span.add_event("task.timeout")
            result = TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                tenant_id=body.tenant_id,
                priority=body.priority,
                error="Task execution exceeded time limit",
                token_usage={"prompt_tokens": 0, "completion_tokens": 0},
                created_at=time.time(),
                completed_at=time.time(),
            )
            TASK_TIMEOUTS_TOTAL.labels(
                tenant_id=metric_tenant, priority=body.priority.value
            ).inc()
            logger.warning(
                "task.timeout",
                timeout_seconds=TASK_TIMEOUT_SECONDS,
                task_description=body.task_description[:200],
            )
        finally:
            TASK_IN_PROGRESS.labels(
                tenant_id=metric_tenant, priority=body.priority.value
            ).dec()

        task_store[task_id] = result
        source = "fresh"
        _record_task_metrics(
            metric_tenant, body.priority.value,
            result.status.value, source,
            max(result.completed_at - result.created_at, 0.0),
        )

        if result.status == TaskStatus.COMPLETED:
            _cache_set(cache_key, result.result or "")

        logger.info(
            "task.finished",
            status=result.status.value,
            source=source,
            duration_ms=round((perf_counter() - request_started) * 1000, 2),
        )
        return _to_response(result)


@app.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    if task_id not in task_store:
        raise HTTPException(status_code=404, detail="Task not found")
    result = task_store[task_id]
    with bind_context(
        task_id=task_id,
        tenant_id=result.tenant_id,
        priority=result.priority.value,
    ):
        logger.info("task.fetched", status=result.status.value)
        return _to_response(result)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


def _to_response(r: TaskResult) -> TaskResponse:
    return TaskResponse(
        task_id=r.task_id, status=r.status,
        tenant_id=r.tenant_id, priority=r.priority,
        result=r.result, error=r.error,
        token_usage=r.token_usage,
        created_at=r.created_at, completed_at=r.completed_at,
    )
