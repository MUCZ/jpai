"""FastAPI application — Agent Execution Service."""

import asyncio
import heapq
import itertools
import structlog
import time
import uuid
from collections import OrderedDict
from time import perf_counter

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel
from typing import Optional
from src.models import Priority, TaskStatus, TaskResult
from src.orchestrator import run_task
from src.config import (
    MAX_CONCURRENT_TASKS,
    PRIORITY_POLICIES,
    RESPONSE_CACHE_MAX_ENTRIES,
    RESPONSE_CACHE_TTL_SECONDS,
    TASK_TIMEOUT_SECONDS,
)
from src.observability import (
    CACHE_ENTRIES,
    CACHE_OPERATIONS_TOTAL,
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

class _PrioritySlot:
    def __init__(
        self,
        scheduler: "_PriorityTaskScheduler",
        tenant_id: str,
        slot_id: int,
    ):
        self._scheduler = scheduler
        self._tenant_id = tenant_id
        self._slot_id = slot_id
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._scheduler.release(self._tenant_id, self._slot_id)


class _PriorityTaskScheduler:
    """Admit tasks by priority while preventing same-tenant concurrency."""

    def __init__(self, max_concurrent: int):
        self._max_concurrent = max_concurrent
        self._running = 0
        self._active_tenants: set[str] = set()
        self._active_slots: dict[int, str] = {}
        self._waiters: list[tuple[int, int, str, asyncio.Future[int]]] = []
        self._sequence = itertools.count()
        self._slot_sequence = itertools.count()
        self._lock = asyncio.Lock()

    async def acquire(self, tenant_id: str, priority: Priority) -> _PrioritySlot:
        waiter = asyncio.get_running_loop().create_future()
        async with self._lock:
            heapq.heappush(
                self._waiters,
                (_priority_rank(priority), next(self._sequence), tenant_id, waiter),
            )
            self._wake_waiters_locked()

        try:
            slot_id = await waiter
            return _PrioritySlot(self, tenant_id, slot_id)
        except BaseException:
            async with self._lock:
                if waiter.cancelled():
                    self._prune_waiters_locked()
                elif not waiter.done():
                    waiter.cancel()
                    self._prune_waiters_locked()
                else:
                    self._release_locked(tenant_id, waiter.result())
                    self._wake_waiters_locked()
            raise

    async def release(self, tenant_id: str, slot_id: int) -> None:
        async with self._lock:
            if self._active_slots.get(slot_id) != tenant_id:
                raise RuntimeError("priority scheduler released an inactive task")
            self._release_locked(tenant_id, slot_id)
            self._wake_waiters_locked()

    def _wake_waiters_locked(self) -> None:
        while self._running < self._max_concurrent:
            next_index = self._next_runnable_index_locked()
            if next_index is None:
                return
            _, _, tenant_id, waiter = self._waiters.pop(next_index)
            heapq.heapify(self._waiters)
            slot_id = next(self._slot_sequence)
            self._running += 1
            self._active_tenants.add(tenant_id)
            self._active_slots[slot_id] = tenant_id
            waiter.set_result(slot_id)

    def _release_locked(self, tenant_id: str, slot_id: int) -> None:
        if self._active_slots.pop(slot_id, None) != tenant_id:
            return
        self._running -= 1
        if tenant_id not in self._active_slots.values():
            self._active_tenants.discard(tenant_id)

    def _prune_waiters_locked(self) -> None:
        retained_waiters = [
            item for item in self._waiters if not item[3].done()
        ]
        if len(retained_waiters) != len(self._waiters):
            self._waiters = retained_waiters
            heapq.heapify(self._waiters)

    def _next_runnable_index_locked(self) -> Optional[int]:
        self._prune_waiters_locked()
        best_index = None
        best_item = None
        for index, item in enumerate(self._waiters):
            _, _, tenant_id, waiter = item
            if waiter.done():
                continue
            if tenant_id in self._active_tenants:
                continue
            if best_item is None or item[:2] < best_item[:2]:
                best_item = item
                best_index = index
        return best_index


def _priority_rank(priority: Priority) -> int:
    return PRIORITY_POLICIES.get(
        priority.value,
        PRIORITY_POLICIES[Priority.NORMAL.value],
    )["rank"]


# Limit concurrent task executions while keeping each tenant single-flight.
_task_scheduler = _PriorityTaskScheduler(MAX_CONCURRENT_TASKS)


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


def _cache_get(cache_key: str) -> Optional[dict]:
    cached = _response_cache.get(cache_key)
    if cached is None:
        CACHE_OPERATIONS_TOTAL.add(1, {"result": "miss"})
        return None

    if time.time() - cached["cached_at"] > RESPONSE_CACHE_TTL_SECONDS:
        _response_cache.pop(cache_key, None)
        CACHE_ENTRIES.add(-1)
        CACHE_OPERATIONS_TOTAL.add(1, {"result": "expired"})
        return None

    _response_cache.move_to_end(cache_key)
    CACHE_OPERATIONS_TOTAL.add(1, {"result": "hit"})
    return cached


def _cache_set(cache_key: str, result: str) -> None:
    if cache_key not in _response_cache:
        CACHE_ENTRIES.add(1)
    _response_cache[cache_key] = {"result": result, "cached_at": time.time()}
    _response_cache.move_to_end(cache_key)
    while len(_response_cache) > RESPONSE_CACHE_MAX_ENTRIES:
        _response_cache.popitem(last=False)
        CACHE_ENTRIES.add(-1)


def _record_task_metrics(
    tenant: str,
    priority: str,
    status: str,
    source: str,
    duration: float,
) -> None:
    """Bundle the counter + histogram updates that every task completion emits."""
    attrs = {
        "tenant_id": tenant,
        "priority": priority,
        "status": status,
        "source": source,
    }
    TASKS_TOTAL.add(1, attrs)
    TASK_DURATION.record(duration, attrs)


def _record_queue_wait(
    tenant: str,
    priority: str,
    queue: str,
    duration: float,
) -> None:
    TASK_QUEUE_WAIT.record(
        duration,
        {
            "tenant_id": tenant,
            "priority": priority,
            "queue": queue,
        },
    )


@app.middleware("http")
async def inject_trace_id_and_log(request: Request, call_next):
    """Log requests and inject X-Trace-Id header into response.

    Metrics and tracing are handled by FastAPIInstrumentor.
    """
    method = request.method
    start = perf_counter()
    trace_id = current_trace_id()
    path = _route_label(request)
    try:
        response = await call_next(request)
    except Exception as exc:
        duration = perf_counter() - start
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
        logger.info(
            "request.completed",
            path=path,
            method=method,
            status_code=response.status_code,
            duration_ms=round(duration * 1000, 2),
        )

    response.headers["X-Trace-Id"] = trace_id
    return response


@app.post("/tasks", response_model=TaskResponse)
async def create_task(body: CreateTaskBody):
    task_id = str(uuid.uuid4())
    request_started = perf_counter()
    task_created_at = time.time()

    # Cache key: tenant + description. Priority affects scheduling/retries,
    # not the task result.
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
                metric_tenant,
                body.priority.value,
                result.status.value,
                "cache",
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
            scheduler_wait_started = perf_counter()
            try:
                with tracer.start_as_current_span("task.wait_priority_scheduler"):
                    slot = await _task_scheduler.acquire(body.tenant_id, body.priority)
            except asyncio.CancelledError:
                scheduler_wait = perf_counter() - scheduler_wait_started
                _record_queue_wait(
                    metric_tenant,
                    body.priority.value,
                    "priority_scheduler",
                    scheduler_wait,
                )
                logger.debug(
                    "task.scheduler_cancelled",
                    queue="priority_scheduler",
                    wait_ms=round(scheduler_wait * 1000, 2),
                )
                raise
            scheduler_wait = perf_counter() - scheduler_wait_started
            _record_queue_wait(
                metric_tenant,
                body.priority.value,
                "priority_scheduler",
                scheduler_wait,
            )
            logger.debug(
                "task.scheduler_acquired",
                queue="priority_scheduler",
                wait_ms=round(scheduler_wait * 1000, 2),
            )
            try:
                TASK_IN_PROGRESS.add(
                    1, {"tenant_id": metric_tenant, "priority": body.priority.value}
                )
                task_store[task_id].status = TaskStatus.RUNNING
                return await run_task(
                    task_id=task_id,
                    description=body.task_description,
                    tenant_id=body.tenant_id,
                    priority=body.priority,
                    created=task_created_at,
                )
            finally:
                TASK_IN_PROGRESS.add(
                    -1, {"tenant_id": metric_tenant, "priority": body.priority.value}
                )
                await slot.release()

        try:
            result = await asyncio.wait_for(
                _guarded_execute(), timeout=TASK_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            span.set_status(StatusCode.ERROR, "Task execution exceeded time limit")
            span.record_exception(asyncio.TimeoutError("Task execution exceeded time limit"))
            span.add_event("task.timeout")
            result = TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                tenant_id=body.tenant_id,
                priority=body.priority,
                error="Task execution exceeded time limit",
                token_usage={"prompt_tokens": 0, "completion_tokens": 0},
                created_at=task_created_at,
                completed_at=time.time(),
            )
            TASK_TIMEOUTS_TOTAL.add(
                1, {"tenant_id": metric_tenant, "priority": body.priority.value}
            )
            logger.warning(
                "task.timeout",
                timeout_seconds=TASK_TIMEOUT_SECONDS,
                task_description=body.task_description[:200],
            )

        task_store[task_id] = result
        source = "fresh"
        _record_task_metrics(
            metric_tenant,
            body.priority.value,
            result.status.value,
            source,
            max(perf_counter() - request_started, 0.0),
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
        task_id=r.task_id,
        status=r.status,
        tenant_id=r.tenant_id,
        priority=r.priority,
        result=r.result,
        error=r.error,
        token_usage=r.token_usage,
        created_at=r.created_at,
        completed_at=r.completed_at,
    )


def _server_request_hook(span, scope):
    """Bind trace/span IDs into structlog context on each request."""
    update_trace_context()


FastAPIInstrumentor.instrument_app(
    app,
    server_request_hook=_server_request_hook,
    excluded_urls="health,metrics",
)
