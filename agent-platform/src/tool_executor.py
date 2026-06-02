"""Simulated tool execution layer.

Each tool simulates an external service call (search engine,
database, calculator, etc.) with realistic latency.
"""

import asyncio
import random

from src.models import Priority
from src.observability import observe_tool


@observe_tool
async def execute_tool(
    tool_name: str, args: dict, *, tenant_id: str, priority: Priority
) -> dict:
    """Execute a single tool and return its result."""
    latency_map = {
        "search": (0.1, 0.5),
        "database_lookup": (0.05, 0.2),
        "calculator": (0.01, 0.05),
    }
    low, high = latency_map.get(tool_name, (0.05, 0.3))
    await asyncio.sleep(random.uniform(low, high))
    return {
        "tool": tool_name,
        "status": "success",
        "output": f"Result from {tool_name}",
    }


async def execute_tools(
    tools: list[tuple[str, dict]], *, tenant_id: str, priority: Priority
) -> list[dict]:
    """Execute multiple tools and return results in order.

    Args:
        tools: List of (tool_name, args) tuples to execute.

    Returns:
        Ordered list of tool execution results.
    """
    results = []
    for tool_name, args in tools:
        result = await execute_tool(
            tool_name, args, tenant_id=tenant_id, priority=priority
        )
        results.append(result)
    return results
