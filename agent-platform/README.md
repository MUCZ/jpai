# Agent Platform

A FastAPI-based agent execution service with structured logging, Prometheus metrics, and OpenTelemetry traces.

## Local stack

Use Docker for the standard local setup:

```bash
docker compose up --build
```

This starts:

- API: `http://localhost:8080`
- Mock LLM: `http://localhost:8081`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000` (admin / `admin`)
- Jaeger: `http://localhost:16686`

The application exports spans to Jaeger over OTLP HTTP in local Docker via:

- `OTEL_SERVICE_NAME=agent-service`
- `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318/v1/traces`

`agent-service` waits for Jaeger to become healthy before startup so traces are not dropped during local boot.

## Useful checks

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

Fetch Prometheus metrics:

```bash
curl http://localhost:8080/metrics
```

## Trace workflow

1. Start the stack with `docker compose up --build`.
2. Send a `POST /tasks` request.
3. Note the `X-Trace-Id` response header.
4. Open Jaeger at `http://localhost:16686`.
5. Search for service `agent-service` and inspect the trace tree.
6. You should see the HTTP request span plus nested task, stage, tool, and LLM spans.

Grafana also provisions a Jaeger datasource, so you can inspect traces in Grafana Explore alongside Prometheus metrics.

## Tests

Run the observability test suite in Docker:

```bash
docker compose run --build --rm agent-service python -m unittest tests.test_observability
```

For concurrent traffic generation after the stack is running:

```bash
python -m tests.test_load
```
