# Issue 1. The exposed `priority` field is not working 

## 1. Description
The API accepts `urgent`, `normal`, and `low`, but `priority` is not used to influence scheduling, timeout policy, retry policy, queue order, or cache segmentation.

## 2. Evidence

### 2.1 Code
The `run_task` function in `orchestrator.py` file and `create_task` function in `main.py ` doesn't use `priority` variables for queue scheduling.

### 2.2 Metrics
Running load test: with argument: 5 tenants, 30 concurrency, 1000 total requests and observe the system 30 minutes

In the `Task Execution & Queues` Section of the metrics dashboard, we can find some evidences that `priority` field is not working

- tasks of different priority have about the same latency
- tasks of different priority have about the same task Timeout Rate 
![evidance](pic/issue1_evidence1.png)

The system bottleneck is the backend IM rate limiting. In this scenario, it is restricted to a maximum of 5 requests(MAX_CONCURRENT_TASKS=5) Both evidences indicate that when the backend IM rate limiting is reached, we are not prioritizing their scheduling based on task priority.  Instead, we are scheduling them in an egalitarian manner, which has resulted in a uniform failure rate across the board.

## 3. Root Caude Analysis

Although the API accepts the priority field, scheduling mechanisms like locks, semaphores, rate limiters, and caches treat all tasks identically using standard FIFO ordering. The `priority` field is not really used for queue scheduling.

## 4. Discovery Path
- Codex: Code Investigation
    > Prompt: "The service is functional but has **several hidden issues** affecting reliability, performance, and cost efficiency. Find them"
- Codex: Observability Dashboard Investigation(using grafana MCP)
    > Prompt: "Explore the project observability dashboard/logs/traces to find any anomalies or patterns"
- Human Confirmation

## 5. Fix Proposition
* Priority-Aware Queuing Across All Bottlenecks
Upgrade all task execution queues or locks in the pipeline to use priority-aware data structures (like `heapq`). This ensures that urgent tasks consistently bypass normal or low tasks at every waiting stage.

* Differentiated Timeouts and Retries
Map the `Priority` enum to specific execution configurations. For example, assign aggressive retries (e.g., 5 attempts) and robust timeouts to urgent tasks, while giving low priority tasks fewer retries to conserve LLM API costs.

## 6. Before-After Comparision
See `FIX.md`

---
# Issue 2. Unbounded in-memory state causes eventual OOM
## 1. Description
The service stores task results and execution audit records in module-level memory with no cleanup policy. This means long-running processes accumulate data forever.

- `task_store` never evicts completed or failed tasks.
- `_response_cache` is a dict that never expiresicts any entries.
- `_execution_log` stores prompts, responses, tool outputs, and timestamps for every successful task with no cap.

## 2. Evidence
![memory leak](pic/issue2_evidence1.png)

## 3. Root Caude Analysis
The service keeps task results and audit records in module-level memory without TTL, size limits, or cleanup. Over time, task_store and _execution_log grow with traffic, causing unbounded memory usage.

## 4. Discovery Path
- Codex: Code Investigation
    > Prompt: "The service is functional but has **several hidden issues** affecting reliability, performance, and cost efficiency. Find them"
- Codex: Observability Dashboard Investigation(using grafana MCP)
    > Prompt: "Explore the project observability dashboard/metrics/logs/traces to find any anomalies or patterns"
- Human Confirmation

## 5. Fix Proposition
  Modify `_response_cache` into a LRU+TTL cache to limit the memory usage.
  Introduce a `sink` abstraction for `task_store` and `_execution_log` audit records. The service will publish records to bounded no-op sinks for now, while keeping the design ready for future Kafka, database, or observability pipeline exports.

## 6. Before-After Comparision
See `FIX.md`

---
# Issue 3 Timeout budgeting is inconsistent

## 1. Description
    Under queueing or late-stage retries, the LLM client thinks budget remains even though the outer task is about to cancel. In other words, the service mistakenly assumes it has a longer timeout period than it actually does. This causes inaccurate error attribution and potential waste of internal or downstream resources after the client has already disconnected.

## 2. Evidence
 traceID 5a630166a09a9047f2ab1f305fa46b40
 ![alt text](image.png)
 还有就是，在一个极高超时率的情况下，我们这个 token 的表现
不对，这个应该不是 token 的。这个主要就是说避免浪费，好像也是 token 的。
也就是说，如果我们只剩一秒的话，就不应该去执行这个东西了 


## 3. Root Caude Analysis

## 4. Discovery Path
- Codex: Code Investigation
    > Prompt: "The service is functional but has **several hidden issues** affecting reliability, performance, and cost efficiency. Find them"
- Codex: Observability Dashboard Investigation(using grafana MCP)
    > Prompt: "Explore the project observability dashboard/metrics/logs/traces to find any anomalies or patterns"
- Human Confirmation

## 5. Fix Proposition

---
# Issue 4. Cache can stampede and has an ambiguous key
## 1. Description
    src/main.py checks cache before scheduler acquisition, but there is no second cache check after queued same-key requests are admitted. Concurrent duplicates can run the full LLM pipeline one after another. Also src/main.py uses f"{tenant}:{description}", so values containing : can collide; use a tuple key or structured key.

The request path performs cache lookup before acquiring the tenant lock, but it does not repeat the cache lookup after entering the serialized execution section. Concurrent requests for the same tenant and same task description can therefore miss together and then execute the full pipeline one after another.

The first cache check happens before _guarded_execute() acquires the per-tenant lock.
Once inside the lock, there is no second cache lookup.
As a result, the first caller populates the cache, but the queued callers still continue with redundant LLM work.
This is a classic missing double-checked-locking problem.
## 2. Evidence
这个就是找一个并发的请求，然后看一下日志and trace


## 3. Root Caude Analysis

## 4. Discovery Path
- Codex: Code Investigation
    > Prompt: "The service is functional but has **several hidden issues** affecting reliability, performance, and cost efficiency. Find them"
- Codex: Observability Dashboard Investigation(using grafana MCP)
    > Prompt: "Explore the project observability dashboard/metrics/logs/traces to find any anomalies or patterns"
- Human Confirmation

## 5. Fix Proposition

---
# Issue 5. Tool calls are serialized even though they are independent
    src/tool_executor.py awaits each tool one by one. The fixed search/database/ calculator calls could run concurrently and preserve result order with asyncio.gather, reducing task latency.

看一下日志和 trace 注意这个时间点