"""
Manual end-to-end test of the npm MCP server.

Spawns mcp_server.py over stdio, lists the tools it exposes, and calls each
one with a realistic input. Useful for verifying the server works without
running the full Claude CLI.

Run:   uv run test_npm_tool.py
"""
import asyncio
import json

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def call(session: ClientSession, tool_name: str, args: dict) -> None:
    """Helper: call a tool and pretty-print its response."""
    print(f"\n▶ {tool_name}({json.dumps(args)})")
    result = await session.call_tool(tool_name, arguments=args)
    for block in result.content:
        # Tool outputs are TextContent blocks — each has a .text attribute.
        text = getattr(block, "text", str(block))
        # Try to re-indent JSON payloads so they're readable.
        try:
            parsed = json.loads(text)
            print(json.dumps(parsed, indent=2)[:1200])
        except (ValueError, TypeError):
            print(text[:1200])


async def main() -> None:
    server_params = StdioServerParameters(
        command="uv",
        args=["run", "mcp_server.py"],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. Confirm the server registered all six tools.
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("Registered tools:", names)

            # 2. Exercise each tool with a known-good input.
            await call(session, "get_latest_version", {"package_name": "react"})
            await call(session, "get_dist_tags", {"package_name": "react"})
            await call(session, "get_version_info",
                       {"package_name": "react", "version": "18.2.0"})
            await call(session, "check_version_exists",
                       {"package_name": "react", "version": "18.2.0"})
            await call(session, "check_version_exists",
                       {"package_name": "react", "version": "999.0.0"})

            # get_all_versions can return a long list — we just show the count.
            print("\n▶ get_all_versions({\"package_name\": \"express\"})")
            r = await session.call_tool(
                "get_all_versions", arguments={"package_name": "express"}
            )
            data = json.loads(r.content[0].text)
            print(f"express has {data['count']} published versions "
                  f"(first 3: {data['versions'][:3]})")

            # get_changelog: ask for a specific release so the output is small.
            await call(session, "get_changelog",
                       {"package_name": "react", "version": "18.2.0"})


if __name__ == "__main__":
    asyncio.run(main())
