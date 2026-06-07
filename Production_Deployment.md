# What SLIs/SLOs would you define for this service?
## Primary SLO: Admitted Task Success Rate
### SLI
```promQL
sum(rate(agent_tasks_total{status="completed"}[5m]))
/
sum(rate(agent_tasks_total[5m]))
```

> This measures the percentage of admitted tasks that successfully complete. 

### SLO
- Urgent: >= 99%
- Normal: >= 98%
- Low: >= 95%

### Reasoning
The service’s core value is completing agent tasks, not merely accepting HTTP requests. This SLI captures the real user-visible outcome across orchestration, LLM calls, tool execution, retries, timeouts, and cache behavior.

### Important Scope
This SLO should apply only to **admitted traffic**, meaning requests accepted into the service’s declared execution capacity. If traffic exceeds backend or LLM capacity, the service should reject quickly with 429/503 instead of accepting unlimited work and letting success rate collapse. Ultimately, this component isn't the actual bottleneck of our system. Consequently, we can't guarantee that every request will succeed; we can only do our best to ensure the success of the requests we accept and a mechanism for rejecting requests that cannot be processed.

### Reliability Contract

For requests admitted within capacity, tasks should complete successfully at the target rate.
For overload traffic, the service should fail fast, protect the backend, and track rejection behavior separately from task success.

# What alerts would you set up?

| Alert | Condition | Severity | Why it matters |
| :--- | :--- | :--- | :--- |
| Task success SLO burn | `sum(rate(agent_tasks_total{status="completed"}[5m])) / sum(rate(agent_tasks_total[5m])) < 0.98` | critical | The core user outcome is failing: admitted tasks are not completing successfully. |
| Urgent task degradation | `sum(rate(agent_tasks_total{status="completed",priority="urgent"}[5m])) / sum(rate(agent_tasks_total{priority="urgent"}[5m])) < 0.99` | critical | Urgent traffic has the strictest reliability expectation. |
| Task timeout spike | `sum(rate(agent_task_timeouts_total[5m])) by (priority) / sum(rate(agent_tasks_total[5m])) by (priority) > 0.01` | critical | Tasks are hitting the 30s execution limit, usually due to LLM latency, queueing, or saturation. |
| LLM dependency degradation | `sum(rate(agent_llm_requests_total{outcome!="success"}[5m])) / sum(rate(agent_llm_requests_total[5m])) > 0.02` | critical | The main downstream dependency is failing or timing out. |
| HTTP 5xx spike | `sum(rate(http_server_duration_milliseconds_count{http_status_code=~"5.."}[5m])) / sum(rate(http_server_duration_milliseconds_count[5m])) > 0.01` | critical | The API itself is returning server errors. |
| Queue saturation | `histogram_quantile(0.95, sum(rate(agent_task_queue_wait_seconds_bucket{queue="priority_scheduler"}[5m])) by (le, priority)) > 10` | warn | Accepted requests are waiting too long before execution. |
| LLM retry spike | `sum(rate(agent_llm_retries_total[5m])) by (reason) > 1` | warn | Retries are an early signal of downstream instability. |
| Overload rejection too slow | <code>histogram_quantile(0.95, sum(rate(http_server_duration_milliseconds_bucket{http_status_code=~"429&#124;503"}[5m])) by (le)) / 1000 > 1</code> | warn | Overload rejection is expected, but it should be fast enough to protect clients and the backend. |

# What would you change for a production deployment on GCP/Kubernetes?
Due to time constraints, this project is still far from a true production environment distributed-deployment level readiness.
The following list outlines the code changes required and dependency deployments required for a cloud-native production environment.

## Potential changes for the application layer.
1. Refactoring related to persistent distributed deployment.
2. Budget management 
3. backend LLM configuration 
4. Downgrade or Circuit Breaker Protection
5. Tenant Management and Access Control
6. Tool calling implementation
7. Security enhancements

## Application Dependencies Deployment

#### 1. Persistent Storage

* **Production Component:** GCP Firestore (recommended) or MongoDB-compatible storage.
* **Role:** Stores task records, execution status, tenant metadata, and audit pointers. This replaces the current in-memory task store so `GET /tasks/{task_id}` works across pod restarts and multiple replicas.
* **Note:** NoSQL fits this service better because the access pattern is mostly key-based lookup and high-volume writes. We do not need joins or complex transactions here.

#### 2. Cache

* **Production Component:** GCP Memorystore for Redis.
* **Role:** Stores shared response-cache entries and distributed locks/rate-limit counters. This replaces the per-process cache and keeps cache behavior consistent across Kubernetes replicas.
* **Note:** Keep TTL enabled and avoid putting `priority` into the response-cache key, so the same tenant/task result can be reused across priorities. Keep in mind that some tasks are not suitable for caching. This issue should be addressed at the application layer, depending on the specific circumstances.

#### 3. Message Queue

* **Production Component:** GCP Pub/Sub (recommended) or Kafka if strict ordering/replay control is required.
* **Role:** Buffers asynchronous work, exports execution audit events, and protects the API layer from downstream spikes.
* **Note:** Use a dead-letter topic for failed tasks and keep the API fail-fast when the queue backlog is too high.

#### 4. Business Event Log Storage

* **Production Component:** GCS (recommended) with optional BigQuery for analysis.
* **Role:** Stores large business-level JSON logs, such as execution records, tool calls, model inputs/outputs, and audit events.

#### 5. LLM Inference

* **Production Component:** dedicated LLM inference service on GKE or Third-party service.
* **Role:** Replaces the local `mock-llm` service and provides the real `/v1/inference` endpoint used by `LLM_SERVER_URL`.
* **Note:** Add timeout, retry, and rate-limit policies at the client side because LLM latency is the main dependency risk.


## Observability Stack Dependencies
The single-container `docker-otel-lgtm` is strictly for development and testing. For production, each component must be deployed independently.

The most convenient method for deploying these components is to use the official Helm Chart to deploy an OpenTelemetry Operator, here's an simple installation example:
```sh
# Install OpenTelemetry Operator using helm
helm install my-otel-operator open-telemetry/opentelemetry-operator 
```

#### 1. Data Collector

* **Production Component:** OpenTelemetry Collector (recommended in K8s DaemonSet or Sidecar mode).
* **Role:** Acts as a high-performance local proxy (Agent) to receive standard OTLP data pushed from applications. It then forwards the data to the backend, protecting apps from OOM risks caused by network jitter or backend crashes.

#### 2. Grafana (Visualization & Alerting)

* **Production Component:** Grafana (highly available multi-instance cluster) + external relational database (e.g., PostgreSQL).
* **Role:** Shares metadata across instances, including dashboard configurations, user permissions, and alerts.

#### 3. Loki (Log Storage)

* **Production Component:** Grafana Loki (Distributed mode) + Object Storage (e.g., Google Cloud Storage (GCS), MinIO).
* **Role:** Persists all raw log data and indices into highly reliable, cost-effective object storage.

#### 4. Tempo (Distributed Tracing)

* **Production Component:** Grafana Tempo (Distributed mode) + Object Storage (e.g., Google Cloud Storage (GCS)).
* **Role:** Saves trace snapshots directly into object storage.

#### 5. Prometheus (Metrics Storage)

* **Production Component:** Grafana Mimir or a Prometheus cluster (integrated with Thanos / Cortex) + Object Storage.
* **Role:** Provides multi-tenant, long-term, and highly available metrics storage for Prometheus, offloading historical data to object storage.

## Configurations
When deploying to the production environment, the following environment variables should be configured correctly.

1. Core Dependencies & Environment

| Environment Variable | Default Value | Brief Description |
| :--- | :--- | :--- |
| **`LLM_SERVER_URL`** | `"http://mock-llm:8081"` | The base URL endpoint for the downstream LLM inference service. |
| **`DEPLOYMENT_ENV`** | `"development"` | Indicates the current deployment environment (e.g., `production`, `staging`). |
| **`SERVICE_VERSION`** | `"dev"` | The version of the deployed service (e.g., git commit hash). |

2. Application Behavior & Caching

| Environment Variable | Default Value | Brief Description |
| :--- | :--- | :--- |
| **`RESPONSE_CACHE_MAX_ENTRIES`** | `256` | Maximum number of entries retained in the local deduplication response cache. |
| **`RESPONSE_CACHE_TTL_SECONDS`** | `300` | Time-to-live (in seconds) for each cached LLM/Task response before it expires. |

3. Observability & Telemetry (OpenTelemetry)

| Environment Variable | Default Value | Brief Description |
| :--- | :--- | :--- |
| **`LOG_LEVEL`** | `"DEBUG"` | Application logging verbosity (e.g., `DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| **`OTEL_SERVICE_NAME`** | `"agent-service"` | The logical service name reported to OpenTelemetry. |
| **`OTEL_EXPORTER_OTLP_ENDPOINT`** | `None` | The OTLP endpoint (e.g., Jaeger or Collector) where metrics, logs, and traces are exported. |
| **`METRICS_TENANT_LABEL_MODE`** | `"direct"` | Controls tenant metric cardinality: `"direct"` logs raw IDs, while bucketed hashes them. |
| **`METRICS_TENANT_BUCKET_COUNT`** | `64` | Number of metric buckets used when `METRICS_TENANT_LABEL_MODE` is set to bucketed. |
