"""End-to-end demo: start the mock AEM, launch the MCP server over stdio and call a few tools.

    python examples/demo_client.py

This is exactly what an MCP host (VS Code, Claude Desktop, Copilot CLI) does under the hood.
"""

import asyncio
import json
import os
import sys

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

from aem_mcp.mock_aem import MockAem


async def run(aem_url: str) -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "aem_mcp.server"],
        env={**os.environ, "AEM_URL": aem_url, "AEM_USER": "admin", "AEM_PASSWORD": "admin"},
    )
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        print(f"Connected. {len(tools)} read-only tools: {', '.join(t.name for t in tools)}\n")

        result = await client.call_tool("aem_health_summary", {})
        health = json.loads(result.content[0].text)
        print(f"Overall: {health['overall'].upper()}")
        for problem in health["problems"]:
            print(f"  - {problem}")

        result = await client.call_tool("check_replication_queues", {})
        for queue in json.loads(result.content[0].text)["queues"]:
            if queue.get("hint"):
                print(f"\nReplication '{queue['agent']}': {queue['hint']}")

        result = await client.call_tool("query_content", {"path": "/"})
        print(f"\nGuardrail check (query on '/'): {result.content[0].text}")
        return 0 if health["overall"] == "critical" and result.is_error else 1


def main() -> int:
    with MockAem(credentials=("admin", "admin")) as mock:
        return asyncio.run(run(mock.url))


if __name__ == "__main__":
    raise SystemExit(main())
