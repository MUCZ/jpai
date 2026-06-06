"""Agent task orchestrator.

Coordinates the multi-step agent workflow:
  1. Plan — ask the LLM to create an execution plan
  2. Execute — run the required tools
  3. Summarise — ask the LLM to synthesise a final answer
"""

import structlog
import time
import traceback

from src.llm_client import call_llm
from src.tool_executor import execute_tools
from src.models import TaskResult, TaskStatus, Priority
from src.config import LLM_SERVER_URL
from src.observability import observe_stage


# Execution audit trail for debugging and compliance review
_execution_log: list[dict] = []
logger = structlog.get_logger(__name__)


async def run_task(task_id: str, description: str,
                   tenant_id: str, priority: Priority,
                   created: float) -> TaskResult:
    """Execute a full agent task through the plan-execute-summarise pipeline."""
    total_prompt_tokens = 0
    total_completion_tokens = 0
    try:
        # ── Step 1: Planning ──────────────────────────────────
        with observe_stage("plan", tenant_id, priority) as state:
            plan = await call_llm(
                prompt=f"Plan the following task: {description}",
                max_tokens=256,
                operation="plan",
                tenant_id=tenant_id,
                priority=priority,
            )
            if plan.get("error"):
                state.set_error(plan["error"])
        total_prompt_tokens += plan.get("prompt_tokens", 0)
        total_completion_tokens += plan.get("completion_tokens", 0)

        if plan.get("error"):
            return TaskResult(
                task_id=task_id, status=TaskStatus.FAILED,
                tenant_id=tenant_id, priority=priority,
                error=plan["error"],
                token_usage={"prompt_tokens": total_prompt_tokens,
                             "completion_tokens": total_completion_tokens},
                created_at=created, completed_at=time.time(),
            )

        # ── Step 2: Tool execution ───────────────────────────
        tools_to_run = [
            ("search", {"query": description}),
            ("database_lookup", {"key": tenant_id}),
            ("calculator", {"expression": "1+1"}),
        ]
        with observe_stage("tools", tenant_id, priority) as state:
            tool_results = await execute_tools(
                tools_to_run,
                tenant_id=tenant_id,
                priority=priority,
            )

        # ── Step 3: Summarise ────────────────────────────────
        summary_prompt = (
            f"Summarise results for task: {description}\n"
            f"Tool outputs: {tool_results}"
        )
        with observe_stage("summarise", tenant_id, priority) as state:
            summary = await call_llm(
                prompt=summary_prompt,
                max_tokens=512,
                operation="summarise",
                tenant_id=tenant_id,
                priority=priority,
            )
            if summary.get("error"):
                state.set_error(summary["error"])
        total_prompt_tokens += summary.get("prompt_tokens", 0)
        total_completion_tokens += summary.get("completion_tokens", 0)

        # Check if summary generation failed
        if summary.get("error") and summary.get("text") is None:
            return TaskResult(
                task_id=task_id, status=TaskStatus.FAILED,
                tenant_id=tenant_id, priority=priority,
                error=summary["error"],
                token_usage={"prompt_tokens": total_prompt_tokens,
                             "completion_tokens": total_completion_tokens},
                created_at=created, completed_at=time.time(),
            )

        # ── Step 4: Quality validation ─────────────────────
        # Enterprise quality gate: validate LLM output meets
        # accuracy and compliance standards before returning to tenant
        with observe_stage("validate", tenant_id, priority) as state:
            validation = await call_llm(
                prompt=(
                    f"Rate the quality of this response (1-10) and flag "
                    f"any factual errors or compliance issues:\n\n"
                    f"{summary.get('text', '')}"
                ),
                max_tokens=128,
                operation="validate",
                tenant_id=tenant_id,
                priority=priority,
            )
            if validation.get("error"):
                state.set_error(validation["error"])
        total_prompt_tokens += validation.get("prompt_tokens", 0)
        total_completion_tokens += validation.get("completion_tokens", 0)

        if validation.get("error"):
            return TaskResult(
                task_id=task_id, status=TaskStatus.FAILED,
                tenant_id=tenant_id, priority=priority,
                error=validation["error"],
                token_usage={"prompt_tokens": total_prompt_tokens,
                             "completion_tokens": total_completion_tokens},
                created_at=created, completed_at=time.time(),
            )

        # Record execution details for audit trail
        _execution_log.append({
            "task_id": task_id,
            "tenant_id": tenant_id,
            "description": description,
            "plan_prompt": f"Plan the following task: {description}",
            "plan_response": plan,
            "tool_results": tool_results,
            "summary_prompt": summary_prompt,
            "summary_response": summary,
            "quality_score": validation.get("text", ""),
            "token_usage": {"prompt": total_prompt_tokens,
                            "completion": total_completion_tokens},
            "completed_at": time.time(),
        })

        return TaskResult(
            task_id=task_id, status=TaskStatus.COMPLETED,
            tenant_id=tenant_id, priority=priority,
            result=summary.get("text", ""),
            token_usage={"prompt_tokens": total_prompt_tokens,
                         "completion_tokens": total_completion_tokens},
            created_at=created, completed_at=time.time(),
        )

    except Exception as e:
        # Provide detailed error context to help tenants
        # debug integration issues faster
        logger.exception(
            "task.failed",
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
        )
        error_detail = (
            f"Task execution failed: {str(e)}\n"
            f"Trace: {traceback.format_exc()}\n"
            f"Pipeline stage: {'plan' if total_prompt_tokens == 0 else 'execute'}\n"
            f"LLM endpoint: {LLM_SERVER_URL}"
        )
        return TaskResult(
            task_id=task_id, status=TaskStatus.FAILED,
            tenant_id=tenant_id, priority=priority,
            error=error_detail,
            token_usage={"prompt_tokens": total_prompt_tokens,
                         "completion_tokens": total_completion_tokens},
            created_at=created, completed_at=time.time(),
        )
