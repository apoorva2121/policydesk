"""Tests for the study interface.

The first test is the one that matters: **every reference in the curriculum must
resolve against the real source.** A study tool whose lessons point at code that
has moved is worse than no study tool, because it teaches something that is no
longer true — and it does so confidently.

That test is what makes it safe to refactor this project later. Rename a function
and the suite tells you which lesson to update.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from policydesk.config import Settings
from policydesk.study.app import costs_money, run_cli
from policydesk.study.content import DRILL, SESSIONS, TOUR, drill_groups, tour_groups
from policydesk.study.progress import (
    StudyProgress,
    load,
    progress_path,
    save,
)
from policydesk.study.snippets import read_constant, read_module_docstring, read_symbol

# ---------------------------------------------------------------------------
# The test that guards against drift
# ---------------------------------------------------------------------------


def test_every_curriculum_reference_resolves_against_the_real_code() -> None:
    """The most important test in this file.

    The study UI contains no source code, only names. If a name no longer exists,
    a lesson is silently teaching something that has moved. This test turns that
    into a failing build.
    """
    broken: list[str] = []

    for item in TOUR:
        if item.show_module_doc:
            doc = read_module_docstring(item.path)
            if not doc.found:
                broken.append(f"{item.path} module docstring: {doc.error}")
        for symbol in item.symbols:
            found = read_symbol(item.path, symbol)
            if not found.found:
                broken.append(f"{item.path}::{symbol} — {found.error}")
        for constant in item.constants:
            found = read_constant(item.path, constant)
            if not found.found:
                broken.append(f"{item.path}::{constant} — {found.error}")

    for session in SESSIONS:
        for step in session.steps:
            for path, symbol in step.read:
                found = read_symbol(path, symbol)
                if not found.found:
                    broken.append(f"session {session.number} → {path}::{symbol} — {found.error}")

    assert not broken, "the curriculum points at code that has moved:\n" + "\n".join(broken)


def test_every_command_the_curriculum_offers_is_a_real_command() -> None:
    """A Run button that cannot run is a broken promise."""
    import policydesk.cli as cli_module

    source = Path(cli_module.__file__).read_text(encoding="utf-8")

    offered: set[str] = set()
    for item in TOUR:
        for _, arguments in item.runs:
            offered.add(arguments.split()[0])
    for session in SESSIONS:
        for step in session.steps:
            if step.run:
                offered.add(step.run[0])

    for command in sorted(offered):
        if command in ("__pytest__", "--help", ""):
            continue
        assert f'"{command}"' in source, f"the curriculum offers '{command}', which the CLI lacks"


# ---------------------------------------------------------------------------
# Reading real source
# ---------------------------------------------------------------------------


def test_a_function_is_read_with_its_decorators() -> None:
    """A decorator changes how a function behaves, so it is part of the lesson."""
    snippet = read_symbol("config.py", "Settings.safe_summary")

    assert snippet.found
    assert "def safe_summary" in snippet.code
    assert snippet.first_line > 1
    assert "config.py:" in snippet.location


def test_a_missing_symbol_reports_a_useful_error_rather_than_raising() -> None:
    """One broken lesson must not take the whole screen down."""
    snippet = read_symbol("config.py", "a_function_that_was_deleted")

    assert not snippet.found
    assert "no longer in" in (snippet.error or "")
    assert "needs updating" in (snippet.error or "")


def test_a_missing_file_is_reported_too() -> None:
    snippet = read_symbol("does_not_exist.py", "anything")
    assert not snippet.found
    assert "does not exist" in (snippet.error or "")


def test_a_prompt_constant_can_be_read() -> None:
    """Prompts are the most important text in the project and the easiest to
    misquote, so the UI reads them from the file."""
    snippet = read_constant("graph/nodes.py", "COMPOSER_SYSTEM")

    assert snippet.found
    assert "COMPOSER_SYSTEM" in snippet.code
    assert "cite" in snippet.code.lower()


def test_a_module_docstring_is_the_lesson() -> None:
    snippet = read_module_docstring("guardrails.py")

    assert snippet.found
    # Whitespace-insensitive: a docstring wraps, so "retrieved document" is not a
    # literal substring. Asserting on prose has to allow for the line break, or the
    # test fails on formatting rather than on content.
    flattened = " ".join(snippet.code.split())
    assert "retrieved document" in flattened
    assert "attacker never talks to you" in flattened


# ---------------------------------------------------------------------------
# Curriculum shape
# ---------------------------------------------------------------------------


def test_the_tour_covers_every_part_of_the_system() -> None:
    paths = {item.path for item in TOUR}
    for essential in (
        "config.py",
        "llm/client.py",
        "ingest/pdf_extract.py",
        "rag/chunking.py",
        "rag/index.py",
        "tools/sql_safety.py",
        "graph/state.py",
        "graph/nodes.py",
        "graph/build.py",
        "guardrails.py",
        "mcp_server.py",
        "evaluation/dataset.py",
        "evaluation/runner.py",
        "observability.py",
        "api.py",
    ):
        assert essential in paths, f"{essential} is not in the code tour"


def test_every_tour_file_asks_a_question_and_answers_it() -> None:
    """A file with no question is a file you will skim and not absorb."""
    for item in TOUR:
        assert item.question, f"{item.path} has no question"
        assert item.answer, f"{item.path} has a question with no answer"
        assert len(item.answer) > 150, f"{item.path}'s answer is too thin to teach anything"


def test_the_guided_sessions_are_numbered_in_order() -> None:
    assert [s.number for s in SESSIONS] == list(range(1, len(SESSIONS) + 1))


def test_every_guided_step_asks_for_a_prediction() -> None:
    for session in SESSIONS:
        for step in session.steps:
            assert step.predict, f"session {session.number} '{step.title}' has no prediction"
            assert step.answer, f"session {session.number} '{step.title}' has no answer"


def test_every_drill_question_has_a_model_answer_and_a_project_example() -> None:
    """The example is the part that was missing from the last scored mock."""
    for item in DRILL:
        assert len(item.model_answer) > 200, f"{item.id}'s model answer is too thin"
        assert item.example, f"{item.id} has no concrete project example"


def test_the_drill_covers_the_four_areas_an_interview_probes() -> None:
    groups = set(drill_groups())
    assert groups == {"RAG mechanics", "Agents", "Safety", "Measurement"}


def test_tour_groups_are_returned_in_reading_order() -> None:
    groups = tour_groups()
    assert groups[0].startswith("1.")
    assert len(groups) == len({item.group for item in TOUR})


# ---------------------------------------------------------------------------
# Running commands
# ---------------------------------------------------------------------------


def test_commands_that_spend_money_are_marked() -> None:
    """So nobody clicks a button and is surprised by a charge."""
    assert costs_money("index")
    assert costs_money('agent "a question"')
    assert costs_money("eval --save")
    assert not costs_money("chunks --show 4")
    assert not costs_money("doctor")
    assert not costs_money("__pytest__ tests/test_sql_safety.py")


def test_a_real_command_runs_and_returns_its_exit_code() -> None:
    """The buttons shell out to the actual CLI, so what you see here is what you
    would see in a terminal — including the exit code."""
    code, output = run_cli("--help", timeout=60)

    assert code == 0
    assert "doctor" in output
    assert "agent" in output


def test_a_failing_command_reports_a_non_zero_code_rather_than_pretending() -> None:
    code, output = run_cli("not-a-real-command", timeout=60)

    assert code != 0
    assert output  # it says something useful


def test_a_quoted_question_survives_being_split() -> None:
    """`shlex` keeps a question in one piece; naive splitting would not."""
    from policydesk.study.app import _split

    parts = _split('agent "What is the hotel limit in Tokyo?" --trace')
    assert parts == ["agent", "What is the hotel limit in Tokyo?", "--trace"]


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------


@pytest.fixture
def study_settings(tmp_path: Path) -> Settings:
    return Settings(app_env="test", _env_file=None, data_dir=tmp_path / "data")


def test_progress_round_trips(study_settings: Settings) -> None:
    progress = StudyProgress()
    progress.record_step("s1-step0", prediction="my guess", done=True)
    progress.record_drill("d01", answer="my answer", score=4)
    save(progress, study_settings)

    loaded = load(study_settings)

    assert loaded.step("s1-step0").prediction == "my guess"
    assert loaded.step("s1-step0").done is True
    assert loaded.drill_entry("d01").score == 4
    assert loaded.mean_drill_score == 4.0


def test_progress_lives_outside_the_wipeable_data_directory(
    study_settings: Settings,
) -> None:
    """`data/processed/` is a directory the docs tell you to delete.

    Putting written answers there would mean one honest cleanup erased them — which
    is exactly the mistake that cost this project its evaluation history once.
    """
    path = progress_path(study_settings)

    assert "processed" not in str(path)
    assert path.name == "study-progress.json"


def test_a_corrupt_progress_file_starts_fresh_rather_than_crashing(
    study_settings: Settings,
) -> None:
    """Losing notes is bad; being unable to open the app at all is worse."""
    path = progress_path(study_settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid json", encoding="utf-8")

    loaded = load(study_settings)

    assert loaded.steps == {}
    assert loaded.started_at  # a usable, fresh object


def test_the_weakest_answers_are_identifiable(study_settings: Settings) -> None:
    """What to practise next, decided by evidence rather than by feel."""
    progress = StudyProgress()
    progress.record_drill("d01", score=5)
    progress.record_drill("d02", score=2)
    progress.record_drill("d03", score=3)

    weakest = progress.weakest_drill(limit=2)

    assert [key for key, _ in weakest] == ["d02", "d03"]


def test_unscored_questions_do_not_drag_the_mean_down(study_settings: Settings) -> None:
    """The same rule as the evaluation metrics: skip, do not score as zero."""
    progress = StudyProgress()
    progress.record_drill("d01", score=4)
    progress.record_drill("d02", answer="written but not scored")

    assert progress.mean_drill_score == 4.0
    assert len(progress.drill_scores) == 1


# ---------------------------------------------------------------------------
# The terminal tutor
# ---------------------------------------------------------------------------


def test_the_tutor_cites_a_file_and_a_line_for_every_snippet(capsys) -> None:  # noqa: ANN001
    """The thing the tutor exists for.

    Reading twenty lines in a terminal is fine; understanding them means having
    the file open. So every snippet prints the exact path, the exact line, and a
    command you can paste into an editor.
    """
    from policydesk.study.tutor import show_snippet

    show_snippet(read_symbol("graph/nodes.py", "verify_node"))
    output = capsys.readouterr().out

    assert "OPEN THIS FILE" in output
    assert "src/policydesk/graph/nodes.py:" in output
    assert "code -g" in output
    assert "def verify_node" in output


def test_the_line_numbers_shown_match_the_real_file() -> None:
    """A citation that points at the wrong line is worse than none."""
    from pathlib import Path as _Path

    snippet = read_symbol("graph/build.py", "route_after_grade")
    assert snippet.found

    source = _Path("src/policydesk/graph/build.py").read_text(encoding="utf-8").splitlines()
    assert source[snippet.first_line - 1].strip().startswith("def route_after_grade")


def test_a_missing_snippet_is_reported_in_the_terminal_not_raised(capsys) -> None:  # noqa: ANN001
    from policydesk.study.tutor import show_snippet

    show_snippet(read_symbol("config.py", "gone_away"))
    output = capsys.readouterr().out

    assert "no longer in" in output


def test_quitting_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl+C and a closed stdin both mean 'leave', not 'crash'.

    A tutor that prints a KeyboardInterrupt traceback feels broken, and handling
    EOF is what lets this be driven by a pipeline or a test without hanging.
    """
    import io

    from policydesk.study.tutor import Quit, ask

    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # stdin closed
    with pytest.raises(Quit):
        ask("anything")

    monkeypatch.setattr("sys.stdin", io.StringIO("q\n"))  # typed 'q'
    with pytest.raises(Quit):
        ask("anything")


def test_the_console_transliteration_table_covers_what_prose_uses() -> None:
    """A Windows console in its default code page cannot print an em-dash.

    Seen in testing: a dash came out as a replacement character mid-sentence,
    which reads as a bug in the lesson rather than a terminal limitation.
    """
    from policydesk.study.tutor import TRANSLITERATE

    for character in ("\u2014", "\u2019", "\u201c", "\u2026"):
        assert character in TRANSLITERATE


# ---------------------------------------------------------------------------
# The desktop icon
# ---------------------------------------------------------------------------


def test_the_icon_is_generated_at_every_size_windows_asks_for(tmp_path: Path) -> None:
    from PIL import Image

    from policydesk.study.shortcut import ICON_SIZES, make_icon

    path = make_icon(tmp_path / "test.ico")

    assert path.exists()
    image = Image.open(path)
    assert set(image.info["sizes"]) == {(n, n) for n in ICON_SIZES}


def test_the_launcher_points_at_this_project_and_keeps_its_window_open(
    tmp_path: Path,
) -> None:
    """A shortcut straight to python.exe closes the instant it ends, taking any
    error message with it. The launcher pauses so you can read it."""
    from policydesk.study.shortcut import make_launcher

    path = make_launcher(tmp_path / "launch.cmd")
    text = path.read_text(encoding="utf-8")

    assert "policydesk.cli learn" in text
    assert "pause" in text
    assert ".venv" in text
    assert "if not exist" in text  # explains a missing environment rather than failing silently


def test_the_desktop_is_found_rather_than_assumed() -> None:
    r"""On a machine with OneDrive the real Desktop is redirected, and writing to
    %USERPROFILE%\Desktop creates a shortcut nobody ever sees."""
    from policydesk.study.shortcut import desktop_directory

    found = desktop_directory()
    assert found is None or found.is_dir()


def test_a_step_shows_the_command_before_asking_you_to_predict(capsys) -> None:  # noqa: ANN001
    """A bug a reader found, and obvious in hindsight.

    The tutor asked "which document will it cite?" without ever showing the
    question being put to the system. That is not a prediction, it is a guessing
    game: you cannot reason about a command you have not seen.
    """
    from policydesk.study.content import SESSIONS
    from policydesk.study.tutor import show_upcoming

    step = SESSIONS[0].steps[0]
    show_upcoming(step)
    output = capsys.readouterr().out

    assert "WHAT WE ARE ABOUT TO RUN" in output
    assert "per-diem limit for international travel" in output


def test_the_explanation_comes_after_the_run_not_before(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """Revealing the answer before running the command spoils the run.

    The order has to be: see the command, commit to a prediction, watch what
    really happens, and only then read why. This asserts the source does them in
    that order, because it is the kind of thing that quietly gets reordered during
    a refactor.
    """
    import inspect

    from policydesk.study import tutor

    source = inspect.getsource(tutor.teach_step)

    predict_at = source.index("PREDICT FIRST")
    run_at = source.index("Now run it and watch")
    explain_at = source.index("Why that happened")

    assert predict_at < run_at < explain_at, (
        "the step must show the command, take a prediction, RUN, then explain"
    )


def test_an_empty_answer_is_allowed_rather_than_blocking(monkeypatch) -> None:  # noqa: ANN001
    """Nobody should be trapped by a prompt they do not understand."""
    import io

    from policydesk.study.tutor import ask_long

    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert ask_long("anything") == ""

    monkeypatch.setattr("sys.stdin", io.StringIO("skip\n"))
    assert ask_long("anything") == ""


# ---------------------------------------------------------------------------
# Rendering the lessons
# ---------------------------------------------------------------------------


def test_emphasis_markers_are_removed_but_the_words_are_kept() -> None:
    """A bug that was invisible until the output was read.

    The replacement was written as a backreference string and lost its backslash
    in transit, becoming a literal control byte. Bold words were being REPLACED by
    an invisible character rather than kept -- so the lesson silently lost text.
    """
    from policydesk.study.tutor import render_inline

    out = render_inline("the input is **not** the user's `question`")

    assert "not" in out
    assert "question" in out
    assert "**" not in out
    assert chr(1) not in out, "a backreference turned into a control byte"


def test_no_lesson_renders_a_control_character() -> None:
    """Checked across every module docstring, not just one example."""
    from policydesk.study.snippets import read_module_docstring
    from policydesk.study.tutor import render_inline

    for item in TOUR:
        doc = read_module_docstring(item.path)
        if not doc.found:
            continue
        rendered = render_inline(doc.code)
        for code in range(1, 9):
            assert chr(code) not in rendered, f"{item.path} renders a control byte"


def test_docstring_headings_survive_rendering(capsys) -> None:  # noqa: ANN001
    """Module docstrings use RST headings, and flowing them destroyed the structure.

    "Why this file exists -------------------- Settings come from..." is what a
    naive wrap produced, which reads as broken formatting.
    """
    from policydesk.study.snippets import read_module_docstring
    from policydesk.study.tutor import paragraph

    paragraph(read_module_docstring("guardrails.py").code, indent="  ")
    output = capsys.readouterr().out

    assert "Read this first" in output
    # The heading must be on its own line, not swallowed into the paragraph.
    for line in output.splitlines():
        if "Read this first" in line:
            assert line.strip() == "Read this first"
            break
    else:
        raise AssertionError("the heading was not rendered on its own line")


def test_a_numbered_list_keeps_its_items_separate(capsys) -> None:  # noqa: ANN001
    """Re-flowing a list into a paragraph destroys the list."""
    from policydesk.study.tutor import paragraph

    paragraph(
        "Ranked by worth:\n\n"
        "1. The model cannot do the dangerous thing, because it is code and not\n"
        "   a prompt.\n"
        "2. Tool allow-list and validated input.\n"
        "3. Output checks.\n",
        indent="  ",
    )
    output = capsys.readouterr().out

    assert "1. The model cannot do" in output
    assert "2. Tool allow-list" in output
    assert "3. Output checks" in output
    # The continuation of item 1 must stay with item 1, not start a new block.
    assert "a prompt." in output


def test_streamlit_entry_points_use_absolute_imports() -> None:
    """A bug found only by opening the page in a browser.

    Streamlit executes these files as TOP-LEVEL SCRIPTS, not as package modules,
    so a relative import has no parent package to resolve against and the app dies
    with `ImportError: attempted relative import with no known parent package`.

    The trap: the server still returns HTTP 200. The page it serves is the error.
    So an HTTP check is not evidence that a Streamlit app works, and this test
    exists because that check was trusted once.
    """
    import re

    for relative in ("ui.py", "study/app.py"):
        path = Path("src/policydesk") / relative
        source = path.read_text(encoding="utf-8")
        offenders = re.findall(r"^\s*from \.+\w*", source, flags=re.MULTILINE)
        assert not offenders, (
            f"{relative} is run by Streamlit as a script and must use absolute "
            f"imports, but has: {offenders}"
        )


def test_the_streamlit_entry_points_execute_as_scripts() -> None:
    """Compile and execute the module body the way Streamlit does.

    Stronger than the import check above: it would catch any other
    package-context assumption, not just the import syntax.
    """
    for relative in ("ui.py", "study/app.py"):
        path = Path("src/policydesk") / relative
        source = path.read_text(encoding="utf-8").split("if __name__")[0]
        namespace: dict = {"__name__": "__streamlit_script__", "__file__": str(path)}
        exec(compile(source, str(path), "exec"), namespace)  # noqa: S102
        assert "run" in namespace, f"{relative} does not define run()"


def test_the_answer_is_rendered_from_stored_state_not_from_the_click() -> None:
    """A bug found only by clicking Approve and looking at the page afterwards.

    Streamlit re-runs the whole script on every interaction. `_ask` is only
    entered when the Ask button is the thing that was pressed, so an answer drawn
    inside `_ask` is erased by the very next click -- including Approve. The page
    then showed the approval result and nothing else: the answer, its citations
    and its verification line all disappeared at the exact moment you needed to
    compare them against the action being approved.

    The fix is to draw from `st.session_state["last_run"]`, which survives the
    re-run. This test pins that down: `_render_run` must be called from `run`,
    never from `_ask`.
    """
    import ast

    tree = ast.parse(Path("src/policydesk/ui.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}

    def calls_in(name: str) -> set[str]:
        return {
            node.func.id
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

    assert "_render_run" not in calls_in("_ask"), (
        "_ask must not render. Streamlit only enters it on the Ask click, so "
        "anything drawn there vanishes on the next interaction."
    )
    assert "_render_run" in calls_in("run"), (
        "run() must render the stored last_run on every pass, or the answer "
        "survives only until the next button press."
    )


def test_the_approval_gate_is_removed_once_it_is_resolved() -> None:
    """Streamlit draws top to bottom, so the buttons are already on the page by
    the time the decision is handled. Without a placeholder to clear, the screen
    ends up showing a live Approve button underneath the word "Approved" -- an
    open gate that has already been closed. On the one screen whose entire
    purpose is to teach that a human decides, that is the worst thing to show.
    """
    source = Path("src/policydesk/ui.py").read_text(encoding="utf-8")
    gate = source[source.index("def _render_pending") : source.index("def _decide")]

    assert "st.empty()" in gate, "the gate must live in a placeholder it can clear"
    assert "gate.empty()" in gate, "the resolved gate must be taken off the screen"
    assert "_decide(st, settings, thread_id, decision)" in gate, (
        "the decision must be handled after the gate is cleared, not inside the "
        "button conditional that drew it"
    )


# ---------------------------------------------------------------------------
# The one-click launcher
# ---------------------------------------------------------------------------


def test_an_already_running_app_is_reused_and_never_stopped() -> None:
    """The rule that stops the launcher being destructive.

    Double-clicking the icon twice, or having run `policydesk study` in another
    terminal already, must not start a second copy and must not kill the first.
    An app we merely borrowed is still running after the tutor closes -- a learning
    tool terminating a server it did not start would be a genuinely bad surprise.
    """
    from policydesk.study.launcher import AppProcess, start_app, stop_app

    # A real listening socket, so `port_is_serving` is answering about something
    # true rather than about a mock.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        app = start_app("borrowed", Path("does-not-matter.py"), port)

        assert app.reused, "a port that answers must be adopted, not started again"
        assert app.process is None, "nothing should have been spawned"
        assert not app.ours, "we did not start it, so it is not ours to stop"

        # Must be a no-op rather than an error or a kill.
        stop_app(app)
        assert app.process is None
    finally:
        listener.close()

    # And the inverse: something we started IS ours.
    ours = AppProcess(name="x", port=1, url="http://localhost:1", process=object())  # type: ignore[arg-type]
    assert ours.ours


def test_a_web_app_that_fails_to_start_does_not_block_the_tutor() -> None:
    """The tutor is the point; the browser tabs are a convenience.

    So a Streamlit app that will not come up is reported with its log path and the
    tutor still opens. The failure is printed, never swallowed and never fatal --
    the alternative is an icon that silently does nothing because of a port clash.
    """
    from policydesk.study import launcher

    report = launcher.LaunchReport()
    report.failures.append("study-ui did not answer on port 8502")

    # The contract the CLI relies on: failures are data to print, not exceptions.
    assert report.failures
    assert report.apps == []
    launcher.stop_study_apps(report)  # must not raise with nothing started


def test_the_desktop_launcher_starts_everything_not_only_the_tutor() -> None:
    """The icon originally ran `policydesk learn`, which opened the tutor alone.

    That left the two web apps to be started by hand in two more terminals: three
    things to remember before studying, so three chances to not bother.
    """
    from policydesk.study.shortcut import LAUNCHER_TEMPLATE

    assert "learn --with-apps" in LAUNCHER_TEMPLATE, (
        "the desktop icon must start the web apps too, not just the tutor"
    )


def test_browser_tabs_are_opened_one_at_a_time_study_tool_first() -> None:
    """The reported failure: three screens, two of them the same app, no study tool.

    On Windows `webbrowser` is `WindowsDefault`, which is `os.startfile` -- it hands
    the URL to the shell and returns at once, knowing nothing about whether a browser
    exists yet. Two of those back to back, while the browser is still cold starting,
    and the second URL lands on a window that is not ready for it.

    So: a real gap between opens, and the study tool first, so that if the browser
    honours only one of them it is the one you came for.
    """
    from policydesk.study.launcher import AppProcess, open_in_browser

    # Two real listening sockets, so `port_is_serving` is answering about something
    # true rather than about a mock.
    listeners = []
    apps = []
    try:
        for name in ("study-ui", "app-ui"):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            listeners.append(sock)
            port = sock.getsockname()[1]
            apps.append(AppProcess(name=name, port=port, url=f"http://localhost:{port}"))

        opened: list[str] = []
        slept: list[float] = []
        open_in_browser(apps, gap=2.5, opener=opened.append, sleeper=slept.append)

        assert opened == [apps[0].url, apps[1].url], "study tool must be opened first"
        assert slept == [2.5], (
            "exactly one gap, between the two opens -- no gap before the first "
            "(pointless delay) and none after the last"
        )
    finally:
        for sock in listeners:
            sock.close()


def test_the_launcher_log_is_appended_not_overwritten(tmp_path: Path) -> None:
    """A log you overwrite before reading is not a log.

    It was opened "wb", which truncates. The next launch therefore destroyed the log
    of the launch that had gone wrong -- noticed while trying to diagnose exactly
    such a launch, with the evidence already gone.
    """
    from policydesk.study import launcher

    original = launcher.LOG_DIR
    launcher.LOG_DIR = tmp_path / "logs"
    started = []
    try:
        # A trivial script on ports nothing is using. We care about the log file's
        # mode, not about Streamlit -- so the servers are stopped, not waited for.
        script = tmp_path / "noop.py"
        script.write_text("", encoding="utf-8")

        for port in (59_401, 59_402):
            app = launcher.start_app("probe", script, port)
            started.append(app)

        log_path = started[0].log_path
        assert log_path is not None
        text = log_path.read_text(encoding="utf-8", errors="replace")
        assert text.count("===== launch") == 2, (
            "the second launch truncated the first one's log instead of appending"
        )
    finally:
        for app in started:
            launcher.stop_app(app)
        launcher.LOG_DIR = original


def test_only_the_study_tool_gets_a_tab_by_default() -> None:
    """The fix for "3 screens open with same ui, I dont see the guided code thing".

    Two servers still start -- that part was wanted. But two browser tabs is where it
    went wrong: `webbrowser` on Windows is `os.startfile`, which hands a URL to the
    shell and returns without knowing whether a browser exists yet. Fire two at a
    cold-starting browser and the second can land on a window that is not ready,
    which is how two tabs ended up showing the same app and the study tool appeared
    nowhere.

    A delay between opens makes that less likely. "Less likely" is not a fix for
    something met every time you sit down to study, so the default opens exactly ONE
    tab -- and one open cannot race with anything. The app itself is linked from
    inside the study tool.
    """
    from policydesk.study.launcher import AppProcess, browser_targets

    study = AppProcess(name="study-ui", port=8502, url="http://localhost:8502")
    app = AppProcess(name="app-ui", port=8501, url="http://localhost:8501")

    assert browser_targets([study, app]) == [study], (
        "only the study tool should be opened, and it must be the study tool"
    )
    assert browser_targets([study, app], open_all=True) == [study, app]


def test_the_study_page_links_to_the_app_so_one_tab_is_enough() -> None:
    """One tab is only enough if the app itself is reachable from it.

    The port is known only to the launcher, and only at launch, so it is passed down
    by environment variable. When it is absent -- someone running `policydesk study`
    on its own -- no link is shown, because a link to a port with nothing behind it
    is worse than no link at all.
    """
    from policydesk.study.launcher import APP_URL_ENV

    assert APP_URL_ENV == "POLICYDESK_APP_URL"

    source = Path("src/policydesk/study/app.py").read_text(encoding="utf-8")

    assert "os.environ.get(launcher.APP_URL_ENV)" in source, (
        "the study page must read the app URL from the launcher, not hardcode a port"
    )
    assert "The app itself" in source, "the link needs a label a human can recognise"
    assert "if app_url:" in source, "no link when the URL is unknown"
