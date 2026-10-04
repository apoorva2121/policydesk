"""The state object: everything the agent knows while it is working.

Why this file matters more than it looks
---------------------------------------
In LangGraph you do not write "the agent". You write a set of small functions
(nodes) and the rules for moving between them (edges). The only thing those
functions share is this state object. Each node reads some of it and returns the
part it changed.

So the state *is* the design. Interviewers ask "what is in your state object?"
precisely because the answer shows whether you designed a system or glued
together some prompts.

The rule we follow: **state holds facts, not prose.** Not "the model said the
evidence looked sufficient" but `grade="sufficient"`. A decision stored as a
sentence cannot be branched on, logged cleanly, or tested.

Four things in here earn their place
------------------------------------
- `attempts` — the retry loop is bounded by a counter in the state. Without a
  number that increments, a critique loop can run until your budget is gone. This
  is the single most common way an agent run costs £40.
- `evidence` — every piece carries its citation. Dropping provenance here would
  make the final answer uncitable, no matter how careful the last step is.
- `pending_action` — a proposed write, held while a human decides. Its presence is
  what makes the graph stop.
- `trace` — a plain-language log of what happened, built as the run proceeds. This
  is what you read when someone says "why did it do that?".
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

# Where a piece of evidence came from. Keeping these distinct matters: a number
# from the database is trustworthy in a way a sentence from a PDF is not, and the
# final answer should treat them differently.
EvidenceKind = Literal["policy_passage", "sql_rows"]

Grade = Literal["sufficient", "insufficient", "not_in_scope"]


class Evidence(BaseModel):
    """One piece of retrieved support, with where it came from."""

    model_config = {"frozen": True}

    kind: EvidenceKind
    citation: str = Field(description="What a user is shown, e.g. 'policy.pdf (p.1, 2. Meals)'")
    content: str = Field(description="The passage text, or the rendered rows")
    # Only set for sql_rows. Shown to the user so the interpretation is checkable.
    sql: str | None = None
    similarity: float | None = None

    def for_prompt(self, number: int) -> str:
        label = "POLICY" if self.kind == "policy_passage" else "DATA"
        header = f"[{number}] {label} from {self.citation}"
        if self.sql:
            header += f"\n    query: {self.sql}"
        return f"{header}\n{self.content}"


class PlannedStep(BaseModel):
    """One tool call the planner intends to make."""

    model_config = {"frozen": True}

    tool: str
    reason: str = Field(description="Why this tool, in one short phrase")
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Which planning round produced this step. Without it, `gather` cannot tell a
    # new step from one it already ran, and re-runs old calls on every retry --
    # paying twice for the same answer. Found by reading a trace, not the code.
    attempt: int = Field(default=0, ge=0)


class PendingAction(BaseModel):
    """A write the agent wants to perform, waiting for a human."""

    tool: str
    arguments: dict[str, Any]
    rationale: str = Field(description="What the agent will do and why, in plain language")


def append_only(existing: list, incoming: list) -> list:
    """A reducer: new items are added, never replacing what is already there.

    LangGraph needs to know how to merge what a node returns into the state. The
    default is "replace", which is right for a single value like `grade` and wrong
    for a log: a node returning one new trace line would wipe out the others.

    Getting this wrong produces a maddening bug where history silently disappears,
    so every accumulating field below is annotated with this.
    """
    return (existing or []) + (incoming or [])


class AgentState(TypedDict, total=False):
    """Everything one run knows. `total=False` means every key is optional,
    because a node only returns the keys it changed."""

    # --- the request ---
    question: str

    # --- planning ---
    plan: Annotated[list[PlannedStep], append_only]
    attempts: int  # bounded retries. A loop without this is a money leak.

    # --- what we found ---
    evidence: Annotated[list[Evidence], append_only]
    grade: Grade | None
    grade_reason: str | None
    missing: str | None  # what was absent, for an honest refusal

    # --- the answer ---
    answer: str | None
    # How many times we have tried to WRITE the answer (separate from how many
    # times we searched). The verifier can send one answer back to be redone.
    compose_attempts: int
    cited_numbers: list[int]
    citations: list[str]
    refused: bool
    verified: bool
    verify_problem: str | None

    # --- the human gate ---
    pending_action: PendingAction | None
    approval: Literal["approved", "rejected"] | None
    action_result: str | None

    # --- observability ---
    trace: Annotated[list[str], append_only]
    errors: Annotated[list[str], append_only]


def initial_state(question: str) -> AgentState:
    """A fresh run. Every accumulating field starts as an empty list, not None,
    so a node can append without checking."""
    return AgentState(
        question=question,
        plan=[],
        attempts=0,
        evidence=[],
        grade=None,
        grade_reason=None,
        missing=None,
        answer=None,
        compose_attempts=0,
        cited_numbers=[],
        citations=[],
        refused=False,
        verified=False,
        verify_problem=None,
        pending_action=None,
        approval=None,
        action_result=None,
        trace=[],
        errors=[],
    )
