"""Tests for the study interface.

The first test is the one that matters: **every reference in the curriculum must
resolve against the real source.** A study tool whose lessons point at code that
has moved is worse than no study tool, because it teaches something that is no
longer true — and it does so confidently.

That test is what makes it safe to refactor this project later. Rename a function
and the suite tells you which lesson to update.
"""

from __future__ import annotations

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
