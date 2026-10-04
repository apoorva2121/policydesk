"""Assembling the graph: the nodes, the routing rules, and the human gate.

What LangGraph actually gives us
--------------------------------
Three things a plain chain cannot do:

1. **Cycles.** `grade` can send us back to `plan`, so the agent can try again with
   a better search. A chain only goes forwards.
2. **Conditional routing.** After `grade` the next node depends on the state, not
   on a fixed order.
3. **Interrupts with durable state.** The graph can stop before a node, write its
   entire state to disk, and resume days later from exactly that point. That is
   what makes human approval real rather than decorative.

The shape
---------
    guard_input
        -> plan -> gather -> grade
            grade: insufficient and attempts left -> plan   (the cycle)
            grade: insufficient and out of attempts -> finalize
            grade: not_in_scope -> finalize
            grade: sufficient -> compose -> verify -> propose_action
                pending action -> [STOP for a human] -> execute_action -> finalize
                no pending action -> finalize

Why the loop is bounded in code, not by instruction
---------------------------------------------------
`attempts` is incremented in a node and checked in an edge. Nothing about this
depends on the model behaving. An agent whose loop limit lives only in a prompt
will one day run forty times and cost forty times as much; this one cannot.

Why the interrupt is before the node, not inside it
--------------------------------------------------
`interrupt_before=["execute_action"]` means the graph halts *having decided* to act
but *before* acting. The proposed action is in the state for a human to read. If
the check lived inside the node, the node would have to be trusted to stop itself,
and a node that performs a write is the worst possible place to put that trust.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from ..config import Settings
from ..guardrails import CallBudget
from ..llm.client import LLMClient
from ..tools.base import ToolRegistry
from .nodes import (
    compose_node,
    execute_action_node,
    finalize_node,
    gather_node,
    grade_node,
    guard_input_node,
    plan_node,
    propose_action_node,
    verify_node,
)
from .state import AgentState

# ---------------------------------------------------------------------------
# Routing rules. Each returns the NAME of the next node.
# ---------------------------------------------------------------------------


def route_after_guard(state: AgentState) -> Literal["plan", "finalize"]:
    return "finalize" if state.get("refused") else "plan"


def route_after_grade(
    state: AgentState, *, max_attempts: int
) -> Literal["plan", "compose", "finalize"]:
    """The decision that makes this an agent rather than a pipeline.

    What the grader is for -- and what it is NOT for
    -----------------------------------------------
    The grader decides **whether another search is worth doing**. It does not
    decide whether the question can be answered. That distinction was learned the
    hard way.

    The first version let an "insufficient" grade on the final attempt go straight
    to a refusal. Watching real runs, the cheap grader kept judging "which expenses
    break policy?" as insufficient even when the evidence held every limit *and*
    every expense row -- because it read the question as "has the breach already
    been identified?". Three searches, then a refusal, with the answer sitting in
    the evidence the whole time.

    The fix is not a better grader prompt. It is to stop giving two components the
    same veto. The composer already refuses honestly when the evidence is thin
    (it emits INSUFFICIENT_EVIDENCE), and the verifier checks whatever it produces.
    So when we run out of attempts we go to `compose` and let it judge, rather than
    refusing on the grader's behalf.

    `not_in_scope` is still a hard stop, because that is a different claim: not
    "I have not found it yet" but "it is not here". Searching again cannot help.
    """
    grade = state.get("grade")

    if grade == "sufficient":
        return "compose"

    if grade == "not_in_scope":
        return "finalize"  # searching again cannot help

    attempts = state.get("attempts", 0)
    if attempts >= max_attempts:
        # Out of attempts. Try to answer with what we have; the composer refuses
        # honestly if it cannot, and the verifier checks it either way.
        return "compose"
    return "plan"


def route_after_verify(
    state: AgentState, *, max_compose_attempts: int = 2
) -> Literal["propose_action", "compose", "finalize"]:
    """Where to go after the mechanical check.

    The second cycle in this graph. When verification fails we send the answer
    back to be written again, once, with the specific complaint attached -- "these
    amounts are not in the evidence" is actionable in a way "try harder" is not.

    This loop is bounded too, and by the same reasoning as the search loop: an
    unbounded rewrite cycle with a fussy checker is a way to spend money forever.
    After the limit we refuse honestly rather than keep paying.
    """
    if state.get("verified") and not state.get("refused"):
        return "propose_action"

    if (
        not state.get("verified")
        and not state.get("refused")
        and state.get("compose_attempts", 0) < max_compose_attempts
    ):
        return "compose"

    return "finalize"


def route_after_propose(state: AgentState) -> Literal["execute_action", "finalize"]:
    """When an action is pending we go towards `execute_action` -- and the graph
    is configured to stop just before it, for a human."""
    return "execute_action" if state.get("pending_action") else "finalize"


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def build_graph(
    settings: Settings,
    llm: LLMClient,
    registry: ToolRegistry,
    *,
    allow_actions: bool = True,
):
    """Wire the nodes and edges. Returns an uncompiled StateGraph."""
    graph = StateGraph(AgentState)

    def increment_then_plan(state: AgentState) -> AgentState:
        """Count the attempt, then plan.

        The counter lives here rather than in the edge so that it is impossible to
        route back to `plan` without also incrementing it. A loop guard you can
        forget to apply is not a loop guard.
        """
        changed = plan_node(state, llm=llm, registry=registry)
        changed["attempts"] = state.get("attempts", 0) + 1
        return changed

    graph.add_node(
        "guard_input", lambda state: guard_input_node(state, max_chars=settings.max_input_chars)
    )
    graph.add_node("plan", increment_then_plan)
    # One budget object for the whole run, so the cap is across every attempt
    # rather than per attempt -- otherwise three attempts triple the limit.
    budget = CallBudget(max_calls=settings.max_tool_calls_per_run)
    graph.add_node("gather", lambda state: gather_node(state, registry=registry, budget=budget))
    graph.add_node(
        "grade",
        lambda state: grade_node(state, llm=llm, max_attempts=settings.max_graph_loops + 1),
    )
    graph.add_node("compose", lambda state: compose_node(state, llm=llm))
    graph.add_node("verify", verify_node)
    graph.add_node(
        "propose_action",
        lambda state: propose_action_node(state, llm=llm, enabled=allow_actions),
    )
    graph.add_node("execute_action", lambda state: execute_action_node(state, registry=registry))
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "guard_input")
    graph.add_conditional_edges("guard_input", route_after_guard)
    graph.add_edge("plan", "gather")
    graph.add_edge("gather", "grade")
    graph.add_conditional_edges(
        "grade",
        lambda state: route_after_grade(state, max_attempts=settings.max_graph_loops + 1),
    )
    graph.add_edge("compose", "verify")
    graph.add_conditional_edges("verify", route_after_verify)  # may loop back to compose
    graph.add_conditional_edges("propose_action", route_after_propose)
    graph.add_edge("execute_action", "finalize")
    graph.add_edge("finalize", END)

    return graph


def compile_graph(
    settings: Settings,
    llm: LLMClient,
    registry: ToolRegistry,
    *,
    allow_actions: bool = True,
    checkpoint_path: Path | None = None,
):
    """Compile the graph with a durable checkpointer and the human interrupt.

    Returns `(compiled_graph, closer)`. Call `closer()` when finished: the SQLite
    checkpointer holds a connection, and leaving it open locks the file on Windows.

    The checkpointer is what makes the interrupt useful. Without it the graph could
    still pause, but the paused state would live in memory and vanish when the
    process exited -- so approval would have to happen within one run, which is not
    how humans work.
    """
    graph = build_graph(settings, llm, registry, allow_actions=allow_actions)

    path = checkpoint_path or (settings.data_dir / "processed" / "agent-checkpoints.sqlite")
    path.parent.mkdir(parents=True, exist_ok=True)

    import sqlite3

    connection = sqlite3.connect(str(path), check_same_thread=False)

    # Our own Pydantic types go into the saved state, so the serializer must be
    # told they are expected. Without this it warns on every load and will refuse
    # outright in a future version -- a deserializer that accepts any type it is
    # handed is how a checkpoint store becomes a way to execute arbitrary objects.
    serde = JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("policydesk.graph.state", "PlannedStep"),
            ("policydesk.graph.state", "Evidence"),
            ("policydesk.graph.state", "PendingAction"),
        ]
    )
    checkpointer = SqliteSaver(connection, serde=serde)

    compiled = graph.compile(
        checkpointer=checkpointer,
        # Stop BEFORE the write, with the proposal visible in the state.
        interrupt_before=["execute_action"],
    )
    return compiled, connection.close


def mermaid_diagram() -> str:
    """The graph as a Mermaid diagram, for the documentation.

    Written by hand rather than generated, because the generated version shows the
    nodes but not *why* each edge exists, and the why is the part worth reading.
    """
    return """flowchart TD
    START([question]) --> G[guard_input<br/>size - injection scan]
    G -->|rejected| F[finalize]
    G -->|accepted| P[plan<br/>which tools, what arguments<br/>attempts += 1]
    P --> GA[gather<br/>run the tool calls<br/>no model call here]
    GA --> GR{grade<br/>is this enough?}

    GR -->|insufficient<br/>attempts left| P
    GR -->|insufficient<br/>out of attempts| F
    GR -->|not_in_scope| F
    GR -->|sufficient| C[compose<br/>answer with citations]

    C --> V{verify<br/>citations real?<br/>amounts in evidence?<br/>no model call}
    V -->|failed| F
    V -->|passed| PA[propose_action]

    PA -->|nothing to flag| F
    PA -->|action proposed| HALT{{GRAPH STOPS<br/>state saved to disk
    <br/>human approves or rejects}}
    HALT -->|approved| EA[execute_action<br/>write to review queue]
    HALT -->|rejected| F
    EA --> F
    F --> E([answer or honest refusal])
"""
