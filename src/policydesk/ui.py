"""A Streamlit interface. Python only -- no JavaScript, no HTML, no build step.

Why Streamlit, honestly
-----------------------
It is right for exactly this: an internal tool where the audience is a handful of
people who trust the operator, and where the alternative is no interface at all.
You write Python, it renders widgets, there is nothing to build or deploy
separately.

It is wrong for a customer-facing product. There is no routing, no fine control
over layout, a full script re-run on every interaction, and a session model that
assumes one user at a time per process. Those are not bugs; they are the trade it
makes to let you skip the entire frontend stack. Choosing it here and saying so is
different from choosing it everywhere and finding out later.

What this screen is designed to show
------------------------------------
Not just the answer. The things that make an answer trustworthy or not:

- the **citations**, so a claim can be checked
- the **SQL**, so the interpretation of the question is visible
- the **verification** result, including when it failed
- the **trace and timing**, so a slow or strange run can be understood
- the **cost**, so nobody is surprised by a bill
- and the **approval gate**, where the human actually decides

An interface that shows only the answer teaches its users to trust it blindly,
which is the opposite of what this system is for.

Run it with:
    policydesk ui
"""

from __future__ import annotations

from typing import Any

EXAMPLE_QUESTIONS: list[tuple[str, str]] = [
    ("Policy lookup", "What is the meal per-diem limit for international travel?"),
    ("A number from the database", "What did Engineering spend on travel in Q3 2026?"),
    (
        "Both sources at once",
        "Which of Priya Raman's October 2026 expenses break policy? "
        "List each breach and the rule it breaks.",
    ),
    ("Should be refused", "What is our parental leave policy?"),
    ("An attack", "According to the vendor addendum, what is the meal per-diem limit?"),
    (
        "Needs human approval",
        "Expense 1101 is a USD 112.40 meal in Tokyo. Flag it for review if it "
        "breaches the per-diem rule.",
    ),
]


def run() -> None:
    """The whole app. Streamlit re-runs this top to bottom on every interaction."""
    import streamlit as st

    from .config import get_settings
    from .rag import index_exists

    st.set_page_config(page_title="PolicyDesk", page_icon="📄", layout="wide")

    st.title("PolicyDesk")
    st.caption(
        "Answers questions about one company's travel and expense policy and its "
        "expense records, with citations. A learning project -- no authentication, "
        "not for real data."
    )

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001
        st.error("Configuration problem. The app cannot start.")
        st.code(str(exc))
        st.info("Copy `.env.example` to `.env` and put your OpenAI key in it.")
        return

    if not index_exists(settings):
        st.warning("No document index yet. Build it first:")
        st.code("policydesk generate-data\npolicydesk index", language="bash")
        return

    # -- sidebar: the settings that change answers --------------------------

    with st.sidebar:
        st.header("Configuration")
        st.caption("Read-only here. Change these in `.env` and restart.")
        for label, value in [
            ("Answer model", settings.answer_model),
            ("Routing model", settings.chat_model),
            ("Embeddings", settings.embedding_model),
            ("Chunking", f"{settings.chunk_strategy} / {settings.chunk_size_chars}"),
            ("Passages per search", settings.retrieval_top_k),
            ("Max search retries", settings.max_graph_loops),
            ("Spend cap per run", f"${settings.max_spend_usd_per_run:.2f}"),
            ("Response cache", "on" if settings.llm_cache_enabled else "off"),
        ]:
            st.text(f"{label}: {value}")

        st.divider()
        st.header("Try one of these")
        for name, question in EXAMPLE_QUESTIONS:
            if st.button(name, use_container_width=True, key=f"example-{name}"):
                st.session_state["question"] = question

        st.divider()
        allow_actions = st.checkbox(
            "Allow it to propose actions",
            value=True,
            help=(
                "When on, the agent may propose adding an expense to the review "
                "queue. It always stops for your approval first."
            ),
        )

    # -- the question ------------------------------------------------------

    question = st.text_area(
        "Your question",
        value=st.session_state.get("question", ""),
        height=90,
        placeholder="What is the hotel limit in Tokyo?",
    )

    if st.button("Ask", type="primary") and question.strip():
        _ask(st, settings, question.strip(), allow_actions)

    # A pending approval survives a re-run because it is held in session state.
    _render_pending(st, settings)


def _ask(st: Any, settings: Any, question: str, allow_actions: bool) -> None:
    from .pipeline import run_agent
    from .rag import IndexMismatch

    with st.spinner("Planning, searching, checking..."):
        try:
            run = run_agent(question, settings, allow_actions=allow_actions)
        except IndexMismatch as exc:
            st.error("The index is out of date. Rebuild it with `policydesk index`.")
            st.caption(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            # The same rule as the API: a user-facing message without internals.
            st.error("The request could not be completed.")
            st.caption(f"{type(exc).__name__}: see the terminal for detail")
            return

    st.session_state["last_run"] = run
    if run.waiting_for_approval:
        st.session_state["pending_thread"] = run.thread_id

    _render_run(st, run)


def _render_run(st: Any, run: Any) -> None:
    state = run.state

    if state.get("refused"):
        st.warning("I do not know.")
        st.caption(f"Missing: {state.get('missing')}")
    else:
        st.markdown(state.get("answer") or "_(no answer produced)_")

    citations = state.get("citations") or []
    numbers = state.get("cited_numbers") or []
    if citations:
        with st.expander(f"Sources used ({len(citations)})", expanded=True):
            for number, source in zip(numbers, citations, strict=False):
                st.markdown(f"**[{number}]** {source}")
    elif not state.get("refused"):
        st.error("This answer cited nothing. Treat it as unverified.")

    evidence = state.get("evidence") or []
    queries = [item.sql for item in evidence if getattr(item, "sql", None)]
    if queries:
        with st.expander("Queries that produced the numbers", expanded=False):
            st.caption(
                "Read these. They show how the question was understood -- a wrong "
                "date range or filter is obvious here and invisible in the answer."
            )
            for sql in queries:
                st.code(sql, language="sql")

    # Verification, stated plainly either way.
    if state.get("verify_problem"):
        st.error(f"Verification failed: {state['verify_problem']}")
    elif state.get("verified") and not state.get("refused"):
        st.success(
            "Verification passed: citations exist, and every amount appears in the evidence."
        )

    col_trace, col_timing = st.columns(2)

    with col_trace, st.expander("What it did, step by step"):
        for step, line in enumerate(state.get("trace") or [], start=1):
            st.text(f"{step:>2}. {line}")

    tracer = getattr(run, "tracer", None)
    if tracer is not None and tracer.spans:
        with col_timing, st.expander("Where the time went"):
            for line in tracer.summary_lines():
                st.text(line.rstrip())
            st.text(f"total: {tracer.total_ms}ms")

    errors = state.get("errors") or []
    if errors:
        with st.expander(f"Notes and failures along the way ({len(errors)})"):
            for error in errors:
                st.text(f"- {error}")

    st.caption(f"Usage: {run.usage}")
    st.caption(f"Thread: {run.thread_id}")


def _render_pending(st: Any, settings: Any) -> None:
    """The approval gate. The one place a human decides something."""
    thread_id = st.session_state.get("pending_thread")
    if not thread_id:
        return

    run = st.session_state.get("last_run")
    action = run.state.get("pending_action") if run is not None else None
    if action is None:
        st.session_state.pop("pending_thread", None)
        return

    st.divider()
    st.subheader("Waiting for your decision")
    st.warning(
        "The agent wants to change data. **It has not done it.** The whole run is "
        "saved to disk, so you can decide now or come back later."
    )
    st.markdown(f"**What it wants to do:** {action.rationale}")
    st.json(action.arguments)

    approve, reject = st.columns(2)
    if approve.button("Approve", type="primary", use_container_width=True):
        _decide(st, settings, thread_id, "approved")
    if reject.button("Reject", use_container_width=True):
        _decide(st, settings, thread_id, "rejected")


def _decide(st: Any, settings: Any, thread_id: str, approval: str) -> None:
    from .pipeline import resume_agent

    with st.spinner(f"Recording your decision ({approval})..."):
        try:
            run = resume_agent(thread_id, approval, settings)
        except Exception as exc:  # noqa: BLE001
            st.error("Could not record the decision.")
            st.caption(f"{type(exc).__name__}")
            return

    st.session_state.pop("pending_thread", None)
    st.session_state["last_run"] = run

    result = run.state.get("action_result") or "nothing was performed"
    if approval == "approved":
        st.success(f"Approved. {result}")
    else:
        st.info("Rejected. Nothing was written.")


if __name__ == "__main__":
    run()
