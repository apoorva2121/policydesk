"""An MCP server exposing PolicyDesk's read-only tools over the protocol.

What MCP is, and how it differs from ordinary tool calling
---------------------------------------------------------
So far our tools have been Python classes the agent imports and calls directly.
That works perfectly well, and for a single application it is the right thing.

MCP (Model Context Protocol) is a **standard way for a tool provider and a tool
user to talk to each other as separate programs.** The difference is not about
capability, it is about who owns what:

| | Ordinary tool calling | MCP |
| --- | --- | --- |
| Where the tool runs | inside your app | a separate process, possibly another machine |
| Who can use it | only your app | any MCP-speaking client |
| How it is described | your own code | a standard schema the client discovers |
| Versioning | you redeploy together | server and client ship independently |

The useful question is "who else needs this tool?". If the answer is nobody, a
Python function is simpler and you should keep it. If the answer is "the support
team's assistant, and a notebook, and someone else's agent next quarter", MCP
stops you writing the same integration three times.

Why this server exposes only read-only tools
--------------------------------------------
`flag_expense` writes, and a write in this system requires human approval through
the graph's interrupt. Exposing it over a protocol would hand any client a way to
write with no approval anywhere in sight -- the same mistake as letting the planner
call it, which is the bug recorded in Milestone 4. A protocol boundary does not
carry your approval policy with it, so the dangerous tool simply does not cross it.

Running it
----------
    policydesk mcp-server          # speaks over stdin/stdout

That transport ("stdio") is how a desktop client launches a local server: it runs
the command and talks to it through pipes. There is nothing listening on a network
port, which is also why it needs no authentication.
"""

from __future__ import annotations

import json
from typing import Any

from .config import Settings, get_settings
from .pipeline import make_client, make_registry
from .tools.base import ToolRegistry


def build_mcp_server(settings: Settings | None = None):
    """Create the MCP server, exposing every read-only PolicyDesk tool.

    Imports are local because the MCP SDK pulls in a web stack, and the CLI should
    not pay that cost on every unrelated command.
    """
    # mcp 2.x renamed FastMCP to MCPServer. Pinning the version in pyproject is
    # what keeps this import working next month; writing it from memory is what
    # breaks it. This project pins mcp==2.3.0.
    from mcp.server.mcpserver import MCPServer

    settings = settings or get_settings()
    llm = make_client(settings)
    registry: ToolRegistry = make_registry(settings, llm)

    server = MCPServer(
        name="policydesk",
        instructions=(
            "Read-only access to one company's travel and expense policy documents "
            "and its expense database. Use policy_search for written rules and "
            "limits; use sql_query for amounts, totals and expense records. Numbers "
            "always come from the database, never from a model."
        ),
    )

    @server.tool()
    def policy_search(query: str, top_k: int = 6) -> str:
        """Search the company's written travel and expense policy.

        Returns passages with citations. Knows the rules, not the spending.
        """
        result = registry.call("policy_search", {"query": query, "top_k": top_k})
        return _render(result)

    @server.tool()
    def sql_query(question: str) -> str:
        """Answer a question about expense records with a real database query.

        Returns the SQL that was run, a plain-language explanation of it, and the
        rows. The SQL is included deliberately: it is how a caller can tell whether
        the question was understood.
        """
        result = registry.call("sql_query", {"question": question})
        return _render(result)

    # Deliberately absent: flag_expense. See the note at the top of this file.
    return server


def _render(result: Any) -> str:
    """Turn a ToolResult into text for an MCP client.

    Note what is NOT included: `internal_detail`. It can contain a rejected query,
    a file path or an exception message, and a protocol response goes to a client
    we do not control. The same split as everywhere else in this project.
    """
    if not result.ok:
        return f"ERROR: {result.error}"

    payload: dict[str, Any] = {"summary": result.summary}
    payload.update({key: value for key, value in result.data.items() if key != "rows"})
    if "rows" in result.data:
        payload["row_count"] = len(result.data["rows"])
    return json.dumps(payload, indent=2, default=str)


def run_stdio() -> None:
    """Run the server over stdin/stdout, the transport a local client launches."""
    build_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    run_stdio()
