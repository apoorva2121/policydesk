"""What a tool is, and the contract every tool must honour.

What "tool" means here
----------------------
A tool is an ordinary Python function that the model is allowed to ask for. The
model does not run code. It produces a request like
`{"tool": "sql_query", "question": "..."}`, and *our* code decides whether to
honour it and how.

That split is the whole security model. The model chooses **when**; your code
controls **what is possible**. Every real protection lives on our side.

The contract
------------
Every tool declares:

- a **name** the model uses to ask for it
- a **description** the model reads to decide when it applies
- an **input schema** (a Pydantic model) so bad input is rejected before anything runs
- whether it is **read-only**, and whether it **needs human approval**
- a **ToolResult**, which reports failure as data rather than raising

Why failures are data, not exceptions
-------------------------------------
An agent must be able to see that a tool failed and decide what to do next: try a
different tool, rephrase, or give up honestly. If a tool raises, the whole run
dies and the user gets a stack trace instead of "I could not look that up".

So a tool returns `ToolResult(ok=False, error="...")` and the agent reads it. We
also never put a raw internal error in `error` -- it goes in `internal_detail`,
which is logged but never shown to a user, because internal errors leak schemas,
paths and query text.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """The outcome of one tool call. Failure is a value, not an exception."""

    ok: bool
    tool: str
    # What the model and the user may see.
    summary: str = Field(default="", description="One line describing what happened")
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = Field(default=None, description="Safe, user-facing reason. No internals.")
    # Logged, never shown. Internal errors leak schemas, paths and query text.
    internal_detail: str | None = None
    duration_ms: int = 0

    @classmethod
    def failure(
        cls, tool: str, error: str, internal_detail: str | None = None, duration_ms: int = 0
    ) -> ToolResult:
        return cls(
            ok=False,
            tool=tool,
            error=error,
            internal_detail=internal_detail,
            duration_ms=duration_ms,
        )


class ToolError(Exception):
    """Raised inside a tool and converted to a ToolResult by `Tool.run`."""

    def __init__(self, safe_message: str, internal_detail: str | None = None):
        super().__init__(safe_message)
        self.safe_message = safe_message
        self.internal_detail = internal_detail


class Tool(ABC):
    """Base class. Subclasses implement `_execute`; `run` adds the safety net."""

    name: str
    description: str
    input_model: type[BaseModel]
    read_only: bool = True
    requires_human_approval: bool = False

    @abstractmethod
    def _execute(self, payload: BaseModel) -> ToolResult:
        """Do the actual work. May raise ToolError."""

    def run(self, raw_input: dict[str, Any]) -> ToolResult:
        """Validate the input, run the tool, and never raise.

        Three things happen here that must happen for *every* tool, which is
        exactly why they live in the base class and not in each tool:

        1. Input is validated against the schema before any work begins.
        2. Timing is recorded, so a slow tool is visible.
        3. Any exception becomes a ToolResult the agent can reason about -- with
           the internal detail kept out of the user-facing message.
        """
        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            payload = self.input_model.model_validate(raw_input)
        except Exception as exc:  # noqa: BLE001 - validation errors are expected
            return ToolResult.failure(
                self.name,
                error=f"the input to '{self.name}' was not valid. Check the required fields.",
                internal_detail=str(exc),
                duration_ms=elapsed_ms(),
            )

        try:
            result = self._execute(payload)
        except ToolError as exc:
            return ToolResult.failure(
                self.name,
                error=exc.safe_message,
                internal_detail=exc.internal_detail,
                duration_ms=elapsed_ms(),
            )
        except Exception as exc:  # noqa: BLE001 - a tool must never kill the run
            return ToolResult.failure(
                self.name,
                error=f"'{self.name}' failed unexpectedly.",
                internal_detail=f"{type(exc).__name__}: {exc}",
                duration_ms=elapsed_ms(),
            )

        return result.model_copy(update={"duration_ms": elapsed_ms()})

    def spec(self) -> dict[str, Any]:
        """What the model is told about this tool.

        Deliberately small. A model choosing between tools needs the name, what it
        is for, and the input shape -- not implementation notes.
        """
        return {
            "name": self.name,
            "description": self.description,
            "read_only": self.read_only,
            "requires_human_approval": self.requires_human_approval,
            "input_schema": self.input_model.model_json_schema(),
        }


class ToolRegistry:
    """The set of tools available in one run -- an allow-list, by construction.

    A model can only ask for a tool that is in here. Asking for anything else
    returns a clear failure, not an attempt. This is why "tool allow-list" is a
    real control and "we told the model which tools exist" is not.
    """

    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"a tool named '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [self._tools[name].spec() for name in self.names()]

    def describe_for_prompt(self) -> str:
        """A compact list for the router's prompt."""
        lines = []
        for name in self.names():
            tool = self._tools[name]
            flags = [] if tool.read_only else ["WRITES DATA"]
            if tool.requires_human_approval:
                flags.append("NEEDS APPROVAL")
            suffix = f" ({', '.join(flags)})" if flags else ""
            lines.append(f"- {name}{suffix}: {tool.description}")
        return "\n".join(lines)

    def call(self, name: str, raw_input: dict[str, Any]) -> ToolResult:
        """Call a tool by name. An unknown name is a clear failure, not a crash."""
        tool = self.get(name)
        if tool is None:
            return ToolResult.failure(
                name,
                error=(
                    f"there is no tool called '{name}'. Available tools: {', '.join(self.names())}."
                ),
            )
        return tool.run(raw_input)

    def __len__(self) -> int:
        return len(self._tools)
