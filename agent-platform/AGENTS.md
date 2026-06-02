# Repository Guidelines

## Project Structure & Module Organization
Core application code lives in `src/`. `main.py` exposes the FastAPI service, `orchestrator.py` runs the plan/execute/summarise flow, `llm_client.py` wraps downstream LLM calls, `tool_executor.py` simulates tools, and `mock_llm_server.py` provides the local mock dependency. Shared enums and dataclasses are in `models.py`; runtime constants are in `config.py`. Load-oriented verification lives in `tests/test_load.py`. Container entrypoints are defined in `Dockerfile` and `docker-compose.yml`.

## Build, Test, and Development Commands
Use Docker for the standard local setup:

```bash
docker compose up --build
```

This starts the API on `localhost:8080` and the mock LLM on `localhost:8081`.

Smoke-check the service:

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

Exercise concurrent traffic with `python -m tests.test_load`.

## Coding Style & Naming Conventions
Follow standard Python style: 4-space indentation, `snake_case` for functions and variables, `PascalCase` for classes, and explicit type hints where the code already uses them. Keep module-level docstrings concise and factual. Prefer small async functions with clear boundaries between HTTP, orchestration, and mock infrastructure. No formatter or linter is configured in this repository, so keep changes PEP 8-aligned and consistent with surrounding files.

## Testing Guidelines
There is no formal unit-test suite yet; the current test asset is the load script in `tests/test_load.py`. For behavior changes, add focused tests under `tests/` using `test_*.py` naming. At minimum, verify `/health`, one successful `POST /tasks`, and failure paths involving the mock LLM.

## Configuration & Runtime Notes
`LLM_SERVER_URL` is the main runtime setting. Task timeout, retry, and rate-limit settings are defined in `src/config.py`; update them deliberately and mention any operational impact in the PR.

# Code Quality Guidelines
1. There should be only one way to do something. Always query for existing solutions when implementing a new feature.
2. Fail fast, fail early, and fail with clear error messages.
3. Don't reinvent the wheel, avoid repetition, leverage open-source libraries, build good abstractions.
4. Keep things as simple as possible; extend only when necessary.
5. Think using first principles, always question whether existing design and functions are really necessary.
6. Leave TODO comments where necessary. A TODO is not necessarily for doing, but an indication of uncovered edge cases or incomplete handling.
7. Prefer table-driven unit tests.
8. Good taste of code matters, keep code elegant and beautiful. Less is more.
