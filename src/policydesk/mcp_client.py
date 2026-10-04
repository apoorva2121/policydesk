"""An MCP client: connect to a server, discover its tools, call one.

Why this file exists at all
---------------------------
It proves the loop closes. A server nobody has connected to is a server that
compiles, not a server that works.

It also shows the thing that makes MCP worth the extra machinery: **discovery**.
The client does not know what tools exist until it asks. With ordinary function
calling you import a known module; here you connect and are told. That is what
lets a server add a tool without every client being redeployed.

What the client must NOT do
---------------------------
Trust what it is told. A tool list comes from another program, and a tool's
description is text that will end up in a model's prompt. So:

- tool names and descriptions are treated as untrusted data, scanned like any
  retrieved content
- a tool the client was not expecting is reported, not silently used
- the client keeps its own allow-list, because "the server offered it" is not a
  reason to call something

This mirrors the lesson from Milestone 5: the dangerous input is the content that
arrives from elsewhere, not the user's typing.
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from .guardrails import scan_for_injection


@dataclass(frozen=True)
class DiscoveredTool:
    name: str
    description: str
    schema: dict[str, Any]
    suspicious: str | None = None


@asynccontextmanager
async def connect_stdio(command: str | None = None, args: list[str] | None = None):
    """Start a local MCP server as a subprocess and open a session to it."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=command or sys.executable,
        args=args if args is not None else ["-m", "policydesk.mcp_server"],
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


async def discover(session) -> list[DiscoveredTool]:
    """Ask the server what it can do, and scan what it answers.

    The scan is the point. A tool description goes straight into a model's prompt,
    so a hostile description is an injection vector that arrives through the
    protocol rather than through a document.
    """
    listing = await session.list_tools()
    found: list[DiscoveredTool] = []

    for tool in listing.tools:
        description = tool.description or ""
        report = scan_for_injection(f"{tool.name}\n{description}")
        found.append(
            DiscoveredTool(
                name=tool.name,
                description=description,
                # mcp 2.x uses snake_case `input_schema`; v1 used `inputSchema`.
                # Reading it off the object rather than guessing is why the
                # version is pinned in pyproject.
                schema=tool.input_schema or {},
                suspicious=report.summary() if report.suspicious else None,
            )
        )
    return found


async def call(
    session, name: str, arguments: dict[str, Any], allowed: set[str] | None = None
) -> str:
    """Call a tool, refusing any name not on the client's own allow-list."""
    if allowed is not None and name not in allowed:
        raise ValueError(
            f"'{name}' is not on this client's allow-list ({', '.join(sorted(allowed))}). "
            "A server offering a tool is not a reason to call it."
        )

    result = await session.call_tool(name, arguments)
    parts: list[str] = []
    for item in result.content:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


async def demo() -> int:
    """Connect, discover, call both tools, print what happened. Returns an exit code."""
    allowed = {"policy_search", "sql_query"}

    async with connect_stdio() as session:
        tools = await discover(session)

        print("Connected to the PolicyDesk MCP server.")
        print(f"It offers {len(tools)} tool(s):\n")
        for tool in tools:
            print(f"  {tool.name}")
            print(f"    {tool.description.strip().splitlines()[0] if tool.description else ''}")
            fields = ", ".join((tool.schema.get("properties") or {}).keys())
            print(f"    inputs: {fields or '(none)'}")
            if tool.suspicious:
                print(f"    WARNING: this tool's description {tool.suspicious}")
            print()

        unexpected = {tool.name for tool in tools} - allowed
        if unexpected:
            print(f"  NOTE: the server offers tools this client will not call: {unexpected}")
        # The write tool must not be reachable over the protocol at all.
        assert "flag_expense" not in {tool.name for tool in tools}

        print("-" * 70)
        print('policy_search("international meal per diem limit")')
        print(
            await call(
                session,
                "policy_search",
                {"query": "international meal per diem limit", "top_k": 2},
                allowed,
            )
        )

        print("-" * 70)
        print('sql_query("How many expenses did Priya Raman submit in October 2026?")')
        print(
            await call(
                session,
                "sql_query",
                {"question": "How many expenses did Priya Raman submit in October 2026?"},
                allowed,
            )
        )

        print("-" * 70)
        print("Refusing a tool that is not on the allow-list:")
        try:
            await call(session, "flag_expense", {"expense_id": 1101}, allowed)
        except ValueError as exc:
            print(f"  refused: {exc}")

    return 0
