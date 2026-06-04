# Agent Platform

A FastAPI-based agent execution service with structured logging, Prometheus metrics, Loki logs, and OpenTelemetry traces.

## Local Stack

Use Docker for the standard local setup:

```bash
docker compose up --build
```

This starts the following services:

- **API**: `http://localhost:8080` (FastAPI Agent Execution Service)
- **Mock LLM**: `http://localhost:8081` (Mock LLM service providing `/v1/inference`)
- **Grafana (OTel LGTM)**: `http://localhost:3000` (Bundled Prometheus, Tempo, and Loki dashboard backend, default theme light)

### Observability Configuration

The application exports spans, metrics, and logs using OTLP over gRPC:

- `OTEL_SERVICE_NAME=agent-service`
- `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-backend:4317`

The `agent-service` container waits for the `otel-backend` service health check (`test -f /tmp/ready`) to report healthy before starting up.

## Useful Checks

Health endpoint:

```bash
curl http://localhost:8080/health
```

Create a task:

```bash
curl -X POST http://localhost:8080/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "task_description": "Summarise customer feedback",
    "tenant_id": "tenant-alpha",
    "priority": "normal"
  }'
```

Fetch Prometheus metrics directly from the API:

```bash
curl http://localhost:8080/metrics
```

## Trace and Log Workflow

1. Start the stack with `docker compose up --build`.
2. Send a `POST /tasks` request.
3. Note the `X-Trace-Id` response header returned by the API.
4. Open Grafana at `http://localhost:3000`.
5. Go to **Explore** and select the **Tempo** or **Loki** data sources to search by trace ID or service name to view structured traces, spans, and correlating logs.

## Tests

Run the observability test suite inside the Docker environment (matching Python 3.12):

```bash
docker compose run --build --rm agent-service python -m unittest tests.test_observability
```

For concurrent load testing against a running stack:

```bash
python -m tests.test_load
```
