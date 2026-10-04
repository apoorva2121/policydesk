"""The study interface: three modes over the same project.

What makes this different from reading the docs
-----------------------------------------------
Three things, and all three matter:

1. **It shows the real source.** Not a copy. `snippets.py` reads the actual file by
   symbol name, so a lesson cannot teach code that no longer exists.
2. **It runs the real commands.** The button next to a question executes the actual
   CLI in a subprocess and shows you the output. You are not reading about the
   system; you are operating it.
3. **It makes you commit to an answer first.** The expected answer is hidden until
   you have written your prediction. Reading an answer creates the feeling of
   understanding; being wrong in writing creates the fact of it.

A note on the "Run" buttons
---------------------------
They call `python -m policydesk.cli ...` as a subprocess rather than importing and
calling the functions. That is deliberate: it is the same path you use in a
terminal, so what you see here is exactly what you would see there — including the
exit code. Importing would be faster and would quietly diverge.

Commands that cost money are marked. Most are free, and the response cache makes
repeats free too.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from policydesk.config import Settings, get_settings
from policydesk.study import launcher
from policydesk.study import progress as progress_store
from policydesk.study.content import DRILL, SESSIONS, TOUR, drill_groups, tour_groups
from policydesk.study.progress import StudyProgress
from policydesk.study.snippets import Snippet, read_constant, read_module_docstring, read_symbol

# Commands that call the API. Everything else is free.
COSTS_MONEY = ("index", "ask", "agent", "eval", "mcp-demo", "approve", "reject")


# ---------------------------------------------------------------------------
# Running real commands
# ---------------------------------------------------------------------------


def run_cli(arguments: str, timeout: int = 180) -> tuple[int, str]:
    """Run the real CLI in a subprocess and return (exit code, output).

    `__pytest__` is a small special case so a lesson can point at the test suite,
    which is part of the project just as much as the application.
    """
    if arguments.startswith("__pytest__"):
        command = [sys.executable, "-m", "pytest", *arguments.split()[1:]]
    else:
        command = [sys.executable, "-m", "policydesk.cli", *_split(arguments)]

    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, f"Timed out after {timeout}s. Run it in a terminal to watch it work."
    except Exception as exc:  # noqa: BLE001
        return 1, f"Could not start the command: {type(exc).__name__}: {exc}"

    output = (completed.stdout or "") + (completed.stderr or "")
    # The HTTP log lines are noise here; the point is the result, not the traffic.
    kept = [line for line in output.splitlines() if "HTTP Request" not in line]
    return completed.returncode, "\n".join(kept).strip()


def _split(arguments: str) -> list[str]:
    """Split a command line, keeping quoted questions in one piece."""
    import shlex

    return shlex.split(arguments)


def costs_money(arguments: str) -> bool:
    first = arguments.split()[0] if arguments.split() else ""
    return first in COSTS_MONEY


# ---------------------------------------------------------------------------
# Shared rendering
# ---------------------------------------------------------------------------


def _show_snippet(st: Any, snippet: Snippet, language: str = "python") -> None:
    if not snippet.found:
        st.error(f"**{snippet.symbol}** — {snippet.error}")
        return
    st.caption(f"`{snippet.location}` — read from the real file, not a copy")
    st.code(snippet.code, language=language)


def _run_block(st: Any, label: str, arguments: str, key: str, show_command: bool = True) -> None:
    """A labelled button that runs a real command and shows its output."""
    warning = "  ·  costs a fraction of a cent" if costs_money(arguments) else "  ·  free"
    st.markdown(f"**{label}**{warning}")
    if show_command:
        st.code(
            f"policydesk {arguments}".replace("policydesk __pytest__", "pytest"),
            language="bash",
        )

    if st.button("Run it", key=f"run-{key}", use_container_width=False):
        with st.spinner("Running the real command..."):
            code, output = run_cli(arguments)
        st.session_state[f"out-{key}"] = (code, output)

    stored = st.session_state.get(f"out-{key}")
    if stored:
        code, output = stored
        if code == 0:
            st.success(f"exit code {code}")
        else:
            st.warning(f"exit code {code} — non-zero, which may be the point of the exercise")
        st.code(output or "(no output)", language="text")


def _predict_then_reveal(
    st: Any,
    *,
    key: str,
    question: str,
    answer: str,
    record,  # noqa: ANN001
    saved_prediction: str,
    revealed: bool,
    run_label: str = "",
    run_arguments: str = "",
    run_key: str = "",
) -> None:
    """Commit to an answer, run the command, and only then read the explanation.

    The ordering is the lesson. Revealing before running spoils the run: you
    already know what will happen, so watching it teaches nothing. And the reveal
    button stays disabled until something is written, because an answer you read
    before committing teaches almost nothing.
    """
    st.markdown(f"#### {question}")
    written = st.text_area(
        "Your answer — write it before revealing",
        value=saved_prediction,
        key=f"pred-{key}",
        height=110,
        placeholder="One or two sentences is enough. Being wrong here is the point.",
    )

    left, right = st.columns([1, 3])
    if left.button("Save", key=f"save-{key}"):
        record(key, prediction=written)
        st.toast("Saved")

    can_reveal = bool(written.strip()) or revealed

    if run_label and run_arguments:
        st.divider()
        # The command was already shown above the question, so do not repeat it.
        _run_block(st, run_label, run_arguments, run_key or key, show_command=False)

    if right.button(
        "Reveal the explanation",
        key=f"reveal-{key}",
        disabled=not can_reveal,
        type="primary",
    ):
        record(key, prediction=written, revealed=True)
        st.session_state[f"revealed-{key}"] = True

    if not can_reveal:
        right.caption("Write something first — even a guess.")

    if revealed or st.session_state.get(f"revealed-{key}"):
        st.markdown("**Why that happened**")
        st.info(answer)
        if written:
            st.caption(f"You predicted: {written[:300]}")


# ---------------------------------------------------------------------------
# Mode 1: the guided path
# ---------------------------------------------------------------------------


def render_guided(st: Any, settings: Settings, progress: StudyProgress) -> None:
    st.header("Guided path")
    st.caption(
        "Seven sessions, in order. Predict, run, be wrong, understand. Start here if "
        "you have not studied the project before."
    )

    titles = [f"Session {s.number} — {s.title} ({s.minutes} min)" for s in SESSIONS]
    chosen = st.selectbox("Session", titles, key="session-pick")
    session = SESSIONS[titles.index(chosen)]

    keys = [f"s{session.number}-step{i}" for i in range(len(session.steps))]
    done = progress.completed_steps(keys)
    st.progress(done / len(keys) if keys else 0.0, text=f"{done} of {len(keys)} steps done")
    st.info(f"**Aim of this session:** {session.aim}")

    for index, step in enumerate(session.steps):
        key = f"s{session.number}-step{index}"
        record = progress.step(key)
        mark = "✓ " if record.done else ""
        with st.expander(f"{mark}{index + 1}. {step.title}", expanded=not record.done):
            st.markdown(step.instruction)

            # The command comes FIRST, before the question.
            #
            # A bug a reader found in the terminal version and which was here too:
            # being asked "which document will it cite?" without having been shown
            # the question put to the system is not a prediction, it is a guessing
            # game. You cannot reason about a command you have not seen.
            if step.run:
                st.divider()
                command, extra = step.run
                st.markdown("**What we are about to run:**")
                st.code(f"policydesk {command} {extra}".strip(), language="bash")

            if step.read:
                st.divider()
                st.markdown("**Read these, from the real files:**")
                for path, symbol in step.read:
                    _show_snippet(st, read_symbol(path, symbol))

            if step.predict:
                st.divider()
                _predict_then_reveal(
                    st,
                    key=key,
                    question=step.predict,
                    answer=step.answer,
                    record=progress.record_step,
                    saved_prediction=record.prediction,
                    revealed=record.revealed,
                    run_label="Now run it and watch" if step.run else "",
                    run_arguments=(f"{step.run[0]} {step.run[1]}".strip() if step.run else ""),
                    run_key=key,
                )

            st.divider()
            if st.checkbox("I understand this step", value=record.done, key=f"done-{key}"):
                if not record.done:
                    progress.record_step(key, done=True)
                    progress_store.save(progress, settings)
            elif record.done:
                progress.record_step(key, done=False)
                progress_store.save(progress, settings)


# ---------------------------------------------------------------------------
# Mode 2: the complete code tour
# ---------------------------------------------------------------------------


def render_tour(st: Any, settings: Settings, progress: StudyProgress) -> None:
    st.header("Code tour")
    st.caption(
        "Every substantive file in the project, in the order it makes sense to read "
        "them. This is the route for understanding every bit of it."
    )

    all_keys = [item.key for item in TOUR]
    done = progress.completed_tour(all_keys)
    st.progress(done / len(all_keys), text=f"{done} of {len(all_keys)} files understood")

    group = st.selectbox("Part of the system", tour_groups(), key="tour-group")
    files = [item for item in TOUR if item.group == group]

    for item in files:
        record = progress.tour_entry(item.key)
        mark = "✓ " if record.done else ""
        with st.expander(f"{mark}`{item.path}` — {item.title}", expanded=not record.done):
            st.markdown(item.why)

            if item.show_module_doc:
                st.divider()
                st.markdown("**Why this file exists, in its own words:**")
                doc = read_module_docstring(item.path)
                if doc.found:
                    st.markdown(f"> {doc.code.replace(chr(10), chr(10) + '> ')}")
                else:
                    st.error(doc.error)

            if item.symbols or item.constants:
                st.divider()
                st.markdown("**The parts worth reading:**")
                for symbol in item.symbols:
                    _show_snippet(st, read_symbol(item.path, symbol))
                for constant in item.constants:
                    _show_snippet(st, read_constant(item.path, constant))

            if item.question:
                st.divider()
                _predict_then_reveal(
                    st,
                    key=item.key,
                    question=item.question,
                    answer=item.answer,
                    record=progress.record_tour,
                    saved_prediction=record.prediction,
                    revealed=record.revealed,
                )

            for number, (label, arguments) in enumerate(item.runs):
                st.divider()
                _run_block(st, label, arguments, f"{item.key}-{number}")

            st.divider()
            if st.checkbox("I understand this file", value=record.done, key=f"tdone-{item.key}"):
                if not record.done:
                    progress.record_tour(item.key, done=True)
                    progress_store.save(progress, settings)
            elif record.done:
                progress.record_tour(item.key, done=False)
                progress_store.save(progress, settings)


# ---------------------------------------------------------------------------
# Mode 3: the interview drill
# ---------------------------------------------------------------------------

ANSWER_SHAPE = (
    "**define or confirm → why it happens → the cheapest fix first → one trade-off → "
    "one concrete example from this project**"
)


def render_drill(st: Any, settings: Settings, progress: StudyProgress) -> None:
    st.header("Interview drill")
    st.caption("Answer out loud first, in four to six sentences, in this shape: " + ANSWER_SHAPE)

    mean = progress.mean_drill_score
    answered = len(progress.drill_scores)
    columns = st.columns(3)
    columns[0].metric("Questions scored", f"{answered} / {len(DRILL)}")
    columns[1].metric("Mean score", f"{mean:.2f}" if mean is not None else "—")
    columns[2].metric("Target", "4.0+")

    if mean is not None and mean < 3.5:
        st.warning(
            "Under 3.5 usually means delivery, not knowledge: an answer arriving as a "
            "keyword list with no 'why' and no example. Re-answer the lowest-scored "
            "ones out loud, with the shape above, before moving on."
        )

    weakest = progress.weakest_drill()
    if weakest:
        labels = ", ".join(f"{key} ({score}/5)" for key, score in weakest)
        st.info(f"**Weakest so far — practise these first:** {labels}")

    group = st.selectbox("Area", drill_groups(), key="drill-group")
    questions = [q for q in DRILL if q.group == group]

    for item in questions:
        record = progress.drill_entry(item.id)
        mark = f"[{record.score}/5] " if record.score else ""
        with st.expander(f"{mark}{item.id} — {item.question}", expanded=record.score is None):
            written = st.text_area(
                "Your answer (say it out loud first, then summarise it here)",
                value=record.answer,
                key=f"drill-ans-{item.id}",
                height=150,
            )

            left, right = st.columns([1, 3])
            if left.button("Save", key=f"drill-save-{item.id}"):
                progress.record_drill(item.id, answer=written, attempts=record.attempts + 1)
                progress_store.save(progress, settings)
                st.toast("Saved")

            can_reveal = bool(written.strip()) or record.revealed
            if right.button(
                "Compare with the model answer",
                key=f"drill-reveal-{item.id}",
                disabled=not can_reveal,
                type="primary",
            ):
                progress.record_drill(item.id, answer=written, revealed=True)
                progress_store.save(progress, settings)
                st.session_state[f"drill-shown-{item.id}"] = True

            if not can_reveal:
                right.caption(
                    "Write your answer first. Reading the model answer first teaches nothing."
                )

            if record.revealed or st.session_state.get(f"drill-shown-{item.id}"):
                st.markdown("**A model answer:**")
                st.info(item.model_answer)
                if item.example:
                    st.markdown("**The example to use — from your own code:**")
                    st.success(item.example)

                st.divider()
                st.markdown(
                    "**Score your own answer honestly.** The score is only useful if it is."
                )
                st.caption(
                    "5 = structured, specific, owns the decision, names the trade-off, has "
                    "the example · 4 = one element missing · 3 = correct but generic · "
                    "2 = vague or drifts · 1 = wrong"
                )
                score = st.radio(
                    "Score",
                    [1, 2, 3, 4, 5],
                    index=(record.score - 1) if record.score else 2,
                    horizontal=True,
                    key=f"drill-score-{item.id}",
                )
                if st.button("Record this score", key=f"drill-rec-{item.id}"):
                    progress.record_drill(item.id, answer=written, score=int(score))
                    progress_store.save(progress, settings)
                    st.toast(f"Recorded {score}/5")
                    st.rerun()


# ---------------------------------------------------------------------------
# Mode 4: where things stand
# ---------------------------------------------------------------------------


def render_status(st: Any, settings: Settings, progress: StudyProgress) -> None:
    st.header("Where things stand")

    tour_keys = [item.key for item in TOUR]
    step_keys = [f"s{s.number}-step{i}" for s in SESSIONS for i in range(len(s.steps))]

    columns = st.columns(3)
    columns[0].metric("Guided steps", f"{progress.completed_steps(step_keys)} / {len(step_keys)}")
    columns[1].metric(
        "Files understood", f"{progress.completed_tour(tour_keys)} / {len(tour_keys)}"
    )
    mean = progress.mean_drill_score
    columns[2].metric(
        "Drill mean",
        f"{mean:.2f}" if mean is not None else "—",
        delta=f"{len(progress.drill_scores)} scored",
    )

    st.divider()
    st.subheader("What the system itself scores")
    st.caption(
        "Read from the saved evaluation runs in `eval-runs/`, not typed in here. "
        "If this is empty, run `policydesk eval --save`."
    )

    from policydesk.evaluation import latest_reports

    reports = latest_reports(settings, count=5)
    if not reports:
        st.info("No saved evaluation runs yet.")
    else:
        rows = []
        for report in reports:
            rows.append(
                {
                    "run": report.label,
                    "pass rate": f"{report.pass_rate:.1%}",
                    "cost": f"${report.total_cost_usd:.4f}",
                    "answer model": report.configuration.answer_model,
                    "chunking": report.configuration.chunk_strategy,
                }
            )
        st.table(rows)
        st.caption(
            "The measured noise floor on this 22-case set is about one case — 4.5%. "
            "Treat any difference smaller than that as undemonstrated."
        )

    st.divider()
    st.subheader("Known weaknesses, stated plainly")
    st.markdown(
        "- **Cross-source questions pass 1 to 2 of 4** across four runs, and the "
        "same two fail every time. Measured, not hidden. "
        "Session 5 is about attacking it.\n"
        "- **No authentication anywhere.** The blocking gap before any real deployment.\n"
        "- **Reranking is not implemented**, so there is no second-pass scoring of "
        "retrieval results.\n"
        "- **`EMBEDDING_BACKEND=local` is declared but not implemented** — it now "
        "refuses to start rather than silently calling the API.\n"
        "- **The Docker image has not been built locally.**\n\n"
        "The full list, ordered by how soon it would hurt, is in `docs/DEPLOYMENT.md`."
    )

    st.divider()
    if st.button("Reset all my study progress"):
        st.session_state["confirm-reset"] = True
    if st.session_state.get("confirm-reset"):
        st.warning("This deletes every prediction, answer and score you have written.")
        if st.button("Yes, delete it", type="primary"):
            fresh = StudyProgress()
            progress_store.save(fresh, settings)
            st.session_state.clear()
            st.rerun()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

MODES = {
    "Guided path": render_guided,
    "Code tour": render_tour,
    "Interview drill": render_drill,
    "Where things stand": render_status,
}


def run() -> None:
    """The whole study app. Streamlit re-runs this on every interaction."""
    import streamlit as st

    st.set_page_config(page_title="PolicyDesk — study", page_icon="🎓", layout="wide")

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001
        st.title("PolicyDesk — study")
        st.error("Configuration problem. Fix this first.")
        st.code(str(exc))
        st.info("Copy `.env.example` to `.env` and put your OpenAI key in it.")
        return

    progress = progress_store.load(settings)

    with st.sidebar:
        st.title("🎓 Study PolicyDesk")
        mode = st.radio("Mode", list(MODES), key="study-mode")

        st.divider()
        st.markdown(
            "**The one rule.** Write your answer *before* revealing. Being wrong in "
            "writing is what makes this stick; being wrong in your head does not."
        )

        st.divider()

        # A link rather than a second browser tab opened for you.
        #
        # The launcher used to open two tabs, and on Windows the second URL handoff
        # can land on a browser window that is not ready for it: the reported result
        # was two tabs of the SAME app and no study tool at all. One tab cannot race
        # with anything, so the app itself is a click from here instead.
        app_url = os.environ.get(launcher.APP_URL_ENV)
        if app_url:
            st.markdown(f"**The app itself** → [{app_url}]({app_url})")
            st.caption("Opens in a new tab. Ask it questions and watch it work.")
            st.divider()

        st.caption(f"Progress saved to `{progress_store.progress_path(settings).name}`")
        st.caption("Buttons run the **real** CLI. Code shown is read from the **real** files.")

    MODES[mode](st, settings, progress)
    progress_store.save(progress, settings)


if __name__ == "__main__":
    run()
