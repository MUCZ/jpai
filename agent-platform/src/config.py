"""Application configuration."""
import os

LLM_SERVER_URL = os.getenv("LLM_SERVER_URL", "http://mock-llm:8081")
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG")
TASK_TIMEOUT_SECONDS = 30
MAX_CONCURRENT_TASKS = 5
RESPONSE_CACHE_MAX_ENTRIES = int(os.getenv("RESPONSE_CACHE_MAX_ENTRIES", "256"))
RESPONSE_CACHE_TTL_SECONDS = int(os.getenv("RESPONSE_CACHE_TTL_SECONDS", "300"))

# LLM rate limiting
LLM_RATE_LIMIT_RPS = 10         # max LLM calls per second
LLM_RATE_LIMIT_BURST = 20       # burst capacity

# Cost tracking
TOKEN_COST_PER_1K_INPUT = 0.003    # $/1K tokens
TOKEN_COST_PER_1K_OUTPUT = 0.015   # $/1K tokens

# Retry configuration
RETRY_MAX_ATTEMPTS = 5
RETRY_BASE_DELAY = 0.5            # seconds
RETRY_BACKOFF_FACTOR = 2.0        # exponential multiplier

# Priority execution policy. The outer /tasks request timeout remains
# TASK_TIMEOUT_SECONDS; these values control queue order and per-LLM-call budget.
PRIORITY_POLICIES = {
    "urgent": {
        "rank": 0,
        "llm_attempt_timeout_seconds": 1.5,
        "llm_max_attempts": 5,
    },
    "normal": {
        "rank": 1,
        "llm_attempt_timeout_seconds": 5.0,
        "llm_max_attempts": 3,
    },
    "low": {
        "rank": 2,
        "llm_attempt_timeout_seconds": 5.0,
        "llm_max_attempts": 2,
    },
}

# Metrics label cardinality control
METRICS_TENANT_LABEL_MODE = os.getenv("METRICS_TENANT_LABEL_MODE", "direct") # bucketed or direct
METRICS_TENANT_BUCKET_COUNT = int(os.getenv("METRICS_TENANT_BUCKET_COUNT", "64"))
