"""Load test script.

Sends concurrent requests to the agent execution service to simulate
realistic multi-tenant traffic.  Run after starting the platform:

    python -m tests.test_load [--tenants N] [--requests N | --minutes X] [--concurrency N] [--output PATH]

Parameters:
  --tenants       Number of tenants (0 to 10) to select from ALL_TENANTS (default: 5).
  --requests      Total number of requests to send (default: 10000).
  --minutes       Duration to run in minutes; mutually exclusive with --requests.
  --concurrency   Number of concurrent requests (default: 30).
  --output        Optional JSON report path.
"""

import argparse, asyncio, httpx, json, random, time, sys
from pathlib import Path

try:
    import resource
except ImportError:
    resource = None

BASE_URL = "http://localhost:8080"
ALL_TENANTS = ["tenant-alpha", "tenant-beta", "tenant-gamma", "tenant-delta", "tenant-epsilon", "tenant-zeta", "tenant-eta", "tenant-theta", "tenant-iota", "tenant-kappa"]
TENANTS = ["tenant-alpha", "tenant-beta", "tenant-gamma"]
PRIORITIES = ["urgent", "normal", "low"]
DEFAULT_REQUESTS = 10000
TOTAL_REQUESTS = DEFAULT_REQUESTS
CONCURRENCY = 30

TASK_TEMPLATES = [
    "Analyse quarterly revenue report for {tenant}",
    "Summarise customer feedback from last week for {tenant}",
    "Generate sales forecast for next quarter for {tenant}",
    "Review compliance documentation for {tenant}",
    "Prepare executive briefing on market trends for {tenant}",
    "Audit expense reports from last month for {tenant}",
    "Draft response to partner inquiry for {tenant}",
    "Evaluate vendor proposals for {tenant}",
    "Create onboarding checklist for new hires at {tenant}",
    "Analyse support ticket trends for {tenant}",
]


async def send_task(client: httpx.AsyncClient, idx: int, timeout: float = 60) -> dict:
    tenant = random.choice(TENANTS)
    priority = random.choice(PRIORITIES)
    template = random.choice(TASK_TEMPLATES)

    if random.random() < 0.3:
        description = template.format(tenant=tenant)
    else:
        description = f"[Task-{idx:04d}] {template.format(tenant=tenant)}"

    payload = {
        "task_description": description,
        "tenant_id": tenant,
        "priority": priority,
    }
    start = time.time()
    try:
        resp = await client.post(f"{BASE_URL}/tasks", json=payload, timeout=timeout)
        elapsed = time.time() - start
        data = resp.json()
        status = data.get("status", "unknown")
        has_result = bool(data.get("result"))
        tokens = data.get("token_usage", {})
        print(f"[{idx:03d}] tenant={tenant:<14s} priority={priority:<7s} "
              f"status={status:<10s} has_result={has_result}  "
              f"tokens={tokens}  {elapsed:.2f}s")
        return {"idx": idx, "status": status, "elapsed": elapsed,
                "tenant": tenant, "priority": priority,
                "has_result": has_result, "tokens": tokens}
    except Exception as e:
        elapsed = time.time() - start
        print(f"[{idx:03d}] tenant={tenant:<14s} priority={priority:<7s} "
              f"ERROR={e}  {elapsed:.2f}s")
        return {"idx": idx, "status": "error", "elapsed": elapsed,
                "tenant": tenant, "priority": priority,
                "has_result": False, "tokens": {}}


async def run_fixed_requests(client: httpx.AsyncClient) -> list[dict]:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def bounded(i):
        async with sem:
            return await send_task(client, i)

    tasks = [bounded(i) for i in range(TOTAL_REQUESTS)]
    return await asyncio.gather(*tasks)


async def run_for_duration(client: httpx.AsyncClient, duration_seconds: float) -> list[dict]:
    deadline = time.monotonic() + duration_seconds
    next_idx = 0

    async def worker() -> list[dict]:
        nonlocal next_idx
        worker_results = []
        while True:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break
            next_idx += 1
            worker_results.append(await send_task(client, next_idx, timeout=remaining_seconds))
        return worker_results

    worker_tasks = [asyncio.create_task(worker()) for _ in range(CONCURRENCY)]
    grouped_results = await asyncio.gather(*worker_tasks)
    results = [result for worker_results in grouped_results for result in worker_results]
    results.sort(key=lambda r: r["idx"])
    return results


def build_report(args: argparse.Namespace, results: list[dict]) -> dict:
    total = len(results)
    completed = [r for r in results if r["status"] == "completed"]
    failed = [r for r in results if r["status"] == "failed"]
    errors = [r for r in results if r["status"] == "error"]
    latencies = sorted(r["elapsed"] for r in results)

    completed_with_result = sum(1 for r in completed if r["has_result"])
    completed_without_result = sum(1 for r in completed if not r["has_result"])

    latency_summary = None
    if latencies:
        latency_summary = {
            "p50": latencies[len(latencies) // 2],
            "p95": latencies[int(len(latencies) * 0.95)],
            "p99": latencies[int(len(latencies) * 0.99)],
            "max": latencies[-1],
        }

    tenant_tokens: dict[str, dict] = {}
    for r in results:
        t = r["tenant"]
        if t not in tenant_tokens:
            tenant_tokens[t] = {"prompt": 0, "completion": 0, "count": 0}
        tenant_tokens[t]["prompt"] += r["tokens"].get("prompt_tokens", 0)
        tenant_tokens[t]["completion"] += r["tokens"].get("completion_tokens", 0)
        tenant_tokens[t]["count"] += 1

    mode = "duration" if args.minutes is not None else "requests"
    return {
        "config": {
            "mode": mode,
            "tenants": args.tenants,
            "requests": args.requests,
            "minutes": args.minutes,
            "concurrency": args.concurrency,
        },
        "summary": {
            "total_requests": total,
            "completed": len(completed),
            "completed_with_result": completed_with_result,
            "completed_without_result": completed_without_result,
            "failed": len(failed),
            "errors": len(errors),
            "latency": latency_summary,
            "token_usage_by_tenant": dict(sorted(tenant_tokens.items())),
        },
        "results": results,
    }


def print_report(report: dict) -> None:
    summary = report["summary"]

    print("\n" + "=" * 60)
    print("LOAD TEST SUMMARY")
    print("=" * 60)
    print(f"Total requests:    {summary['total_requests']}")
    print(f"Completed:         {summary['completed']}  "
          f"(with result: {summary['completed_with_result']}, "
          f"empty result: {summary['completed_without_result']})")
    print(f"Failed:            {summary['failed']}")
    print(f"Errors:            {summary['errors']}")

    latency = summary["latency"]
    if latency is not None:
        print(f"\nLatency  P50={latency['p50']:.2f}s  P95={latency['p95']:.2f}s  "
              f"P99={latency['p99']:.2f}s  Max={latency['max']:.2f}s")

    print("\nToken usage by tenant:")
    for t, v in summary["token_usage_by_tenant"].items():
        print(f"  {t}: {v['count']} tasks, "
              f"prompt={v['prompt']} tokens, "
              f"completion={v['completion']} tokens")


def write_report(report: dict, output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nJSON report written to: {path}")


async def main():
    parser = argparse.ArgumentParser(description="Load test script.")
    parser.add_argument("--tenants", type=int, default=5, help="Number of tenants (0 to 10)")
    parser.add_argument("--requests", type=int, default=None, help=f"Total requests (default: {DEFAULT_REQUESTS})")
    parser.add_argument("--minutes", type=float, default=None, help="Duration to run in minutes")
    parser.add_argument("--concurrency", type=int, default=30, help="Concurrency")
    parser.add_argument("--output", help="Optional JSON report path")
    args = parser.parse_args()

    if args.requests is not None and args.minutes is not None:
        parser.error("--requests and --minutes are mutually exclusive.")
    if args.requests is None and args.minutes is None:
        args.requests = DEFAULT_REQUESTS

    if not (0 <= args.tenants <= 10):
        print("Error: --tenants must be between 0 and 10.")
        sys.exit(1)
    if args.requests is not None and args.requests < 0:
        print("Error: --requests must be non-negative.")
        sys.exit(1)
    if args.minutes is not None and args.minutes <= 0:
        print("Error: --minutes must be greater than 0.")
        sys.exit(1)
    if args.concurrency <= 0:
        print("Error: --concurrency must be greater than 0.")
        sys.exit(1)
    if args.tenants == 0 and (args.requests is None or args.requests > 0):
        print("Error: Cannot send requests with 0 tenants.")
        sys.exit(1)

    global TOTAL_REQUESTS, CONCURRENCY
    TENANTS[:] = ALL_TENANTS[:args.tenants]
    if args.requests is not None:
        TOTAL_REQUESTS = args.requests
    CONCURRENCY = args.concurrency

    if resource is not None:
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            needed_limit = CONCURRENCY + 100
            if soft < needed_limit:
                new_soft = min(needed_limit, hard)
                resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
        except Exception as e:
            print(f"System configuration error: {e}")
    else:
        print("System configuration error: resource module not supported.")

    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)

    async with httpx.AsyncClient(limits=limits) as client:
        if args.minutes is not None:
            print(f"Running for {args.minutes:g} minute(s) with concurrency={CONCURRENCY}.")
            results = await run_for_duration(client, args.minutes * 60)
        else:
            print(f"Running {TOTAL_REQUESTS} request(s) with concurrency={CONCURRENCY}.")
            results = await run_fixed_requests(client)

    report = build_report(args, results)
    print_report(report)
    if args.output:
        write_report(report, args.output)


if __name__ == "__main__":
    asyncio.run(main())
