"""A terminal tutor: step by step, with the real code and where to find it.

Why a terminal version when there is already a web one
------------------------------------------------------
Because the terminal is where you will actually work on this project. Alt-tabbing
to a browser to read a lesson, then back to run a command, then back again, is
friction — and friction is what stops people finishing a study plan.

This runs in the same window you run everything else in. It prints the real code,
tells you **exactly which file and line** to open, waits for you to commit to an
answer, runs the real command, and remembers where you got to.

What it shares with the web version
-----------------------------------
All of it. The same `content.py` curriculum, the same `snippets.py` that reads real
source by name, the same `progress.py` file. Finish a step here and the web UI shows
it done, and the other way round. Two faces, one set of facts — which is the same
reason `pipeline.py` exists.

Keyboard, not mouse
-------------------
Every prompt accepts a short word or a number. `?` shows what is available, `q`
leaves, and Ctrl+C is handled rather than printing a traceback at you.
"""

from __future__ import annotations

import os
import re
import sys
import textwrap
from dataclasses import dataclass

from ..config import Settings
from . import progress as progress_store
from .content import DRILL, SESSIONS, TOUR, DrillQuestion, Session, Step, TourFile
from .progress import StudyProgress
from .snippets import Snippet, read_constant, read_module_docstring, read_symbol

# ---------------------------------------------------------------------------
# Terminal presentation
# ---------------------------------------------------------------------------

WIDTH = 86


@dataclass(frozen=True)
class Style:
    """ANSI codes, or empty strings when the terminal cannot show them.

    Checked once rather than guessed: a dumb terminal or a redirected pipe gets
    plain text instead of escape sequences, because a lesson full of `\\x1b[1m` is
    unreadable and looks broken.
    """

    bold: str = ""
    dim: str = ""
    reset: str = ""
    cyan: str = ""
    green: str = ""
    yellow: str = ""
    red: str = ""
    magenta: str = ""


def detect_style() -> Style:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return Style()
    # Windows terminals need this to interpret ANSI; harmless elsewhere. colorama
    # arrives as a dependency of other packages, so this is not a new requirement.
    try:
        import colorama

        colorama.just_fix_windows_console()
    except Exception:  # noqa: BLE001 - colour is a nicety, never a requirement
        pass
    return Style(
        bold="\x1b[1m",
        dim="\x1b[2m",
        reset="\x1b[0m",
        cyan="\x1b[36m",
        green="\x1b[32m",
        yellow="\x1b[33m",
        red="\x1b[31m",
        magenta="\x1b[35m",
    )


S = detect_style()


# Characters a Windows console in its default code page cannot print. Seen as
# literal mojibake in testing: an em-dash came out as a replacement character in
# the middle of a sentence, which looks like a bug in the lesson.
TRANSLITERATE = {
    "—": "--",  # em dash
    "–": "-",  # en dash
    "’": "'",  # right single quote
    "‘": "'",
    "“": '"',
    "”": '"',
    "…": "...",
    " ": " ",  # non-breaking space
    "→": "->",
    "✓": "+",
}


def enable_utf8_output() -> bool:
    """Ask the console for UTF-8. Returns whether it worked.

    Tried first because it is the right fix: the text is correct and the terminal
    simply needs to be told. `safe()` below is the fallback for consoles that
    refuse, so a lesson is never rendered with replacement characters.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        return True
    except Exception:  # noqa: BLE001
        return False


_UTF8_OK = True


def safe(text: str) -> str:
    """Replace characters this console cannot print, rather than mangling them."""
    if _UTF8_OK:
        return text
    for fancy, plain in TRANSLITERATE.items():
        text = text.replace(fancy, plain)
    return text


def rule(character: str = "-") -> None:
    print(S.dim + character * WIDTH + S.reset)


def heading(text: str) -> None:
    print()
    rule("=")
    print(f"{S.bold}{S.cyan}{safe(text)}{S.reset}")
    rule("=")


def subheading(text: str) -> None:
    print()
    print(f"{S.bold}{safe(text)}{S.reset}")
    rule()


BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
CODE_RE = re.compile(r"`([^`]+)`")


def render_inline(text: str) -> str:
    """Turn **bold** and `code` into terminal styling rather than printing markers.

    These docstrings are written in Markdown because they are also read on GitHub
    and in an editor. In a terminal the raw asterisks are noise -- and there are
    33 files of this to read, so the noise adds up.
    """
    # Every replacement below is a lambda returning group(1) explicitly, never a
    # backreference STRING. A backreference that loses its backslash in transit
    # becomes a literal control byte, so the matched text is replaced by an
    # invisible character rather than kept -- which is exactly what happened here,
    # silently, until the rendered output was actually read and bold words were
    # found to be vanishing from the lesson.
    if not S.bold:  # no colour support: strip the markers, keep the words
        text = BOLD_RE.sub(lambda m: m.group(1), text)
        return CODE_RE.sub(lambda m: m.group(1), text)
    text = BOLD_RE.sub(lambda m: f"{S.bold}{m.group(1)}{S.reset}", text)
    return CODE_RE.sub(lambda m: f"{S.cyan}{m.group(1)}{S.reset}", text)


def visible_length(text: str) -> int:
    """Length ignoring ANSI codes, so wrapping is not thrown off by styling."""
    return len(re.sub(r"\[[0-9;]*m", "", text))


def is_underline(line: str) -> bool:
    """True for an RST-style underline: a run of - or = under a heading."""
    stripped = line.strip()
    return len(stripped) >= 3 and set(stripped) <= {"-", "="}


def wrap_styled(text: str, *, first_indent: str, rest_indent: str) -> None:
    """Style the text, then wrap it by VISIBLE width.

    Two orderings are both wrong and I tried one of them. Wrapping first and
    styling each finished line leaves any emphasis that straddles a line break
    unmatched, so `**retrieved document**` kept its asterisks whenever the wrap
    happened to fall between the two words. Styling first and wrapping with the
    standard library counts the invisible escape codes as characters, so lines
    come out short and ragged.

    So: style the whole string first, then wrap it ourselves, measuring each word
    by how much of it a reader can actually see.
    """
    styled = render_inline(safe(" ".join(text.split())))
    if not styled.strip():
        return

    limit = WIDTH
    indent = first_indent
    current: list[str] = []
    used = len(indent)

    def emit() -> None:
        nonlocal current, used, indent
        if current:
            print(indent + " ".join(current))
        current = []
        indent = rest_indent
        used = len(indent)

    for word in styled.split(" "):
        if not word:
            continue
        width = visible_length(word)
        extra = width if not current else width + 1
        if current and used + extra > limit:
            emit()
            extra = width
        current.append(word)
        used += extra

    emit()


def is_list_item(line: str) -> bool:
    stripped = line.lstrip()
    return bool(re.match(r"^([-*]\s|\d+\.\s)", stripped))


def paragraph(text: str, indent: str = "") -> None:
    """Print prose wrapped to the terminal, keeping headings as headings.

    Every module in this project opens with an RST-style docstring:

        Why this file exists
        --------------------
        Settings come from environment variables...

    Flowing that naively produced "Why this file exists -------------------- Settings
    come from environment variables", which reads as broken formatting and buries the
    structure the author put there deliberately. So an underlined line is recognised
    and rendered as a heading instead of being swallowed into the paragraph.
    """
    lines = text.split("\n")
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        joined = " ".join(" ".join(buffer).split())
        buffer.clear()
        if not joined:
            return
        wrap_styled(joined, first_indent=indent, rest_indent=indent)
        print()

    index = 0
    while index < len(lines):
        line = lines[index]
        following = lines[index + 1] if index + 1 < len(lines) else ""

        if line.strip() and is_underline(following):
            flush()
            print(f"{indent}{S.bold}{safe(line.strip())}{S.reset}")
            print(f"{indent}{S.dim}{'-' * min(len(line.strip()), WIDTH - len(indent))}{S.reset}")
            index += 2
            continue

        if is_underline(line):  # an underline with no heading above it
            index += 1
            continue

        if not line.strip():
            flush()
            index += 1
            continue

        # A list item, with its continuation lines absorbed so the hanging indent
        # survives. Without this the second line of an item wraps back to the base
        # indent and the list stops looking like a list.
        if is_list_item(line):
            flush()
            item = [line.strip()]
            index += 1
            while index < len(lines):
                nxt = lines[index]
                if not nxt.strip() or is_list_item(nxt) or is_underline(nxt):
                    break
                item.append(nxt.strip())
                index += 1
            wrap_styled(" ".join(item), first_indent=indent + "  ", rest_indent=indent + "     ")
            continue

        buffer.append(line)
        index += 1

    flush()


def bullet(text: str) -> None:
    print(
        textwrap.fill(
            safe(" ".join(text.split())),
            width=WIDTH,
            initial_indent="  - ",
            subsequent_indent="    ",
        )
    )


# ---------------------------------------------------------------------------
# Showing code, and saying where it lives
# ---------------------------------------------------------------------------


def show_snippet(snippet: Snippet, max_lines: int = 60) -> None:
    """Print real source with a precise citation and a way to open it.

    The citation is the point of this whole function. Reading code in a terminal
    pane is fine for twenty lines; understanding it means having the file open. So
    every snippet prints the exact path and line, plus an editor command you can
    paste.
    """
    if not snippet.found:
        print(f"{S.red}  ! {snippet.symbol}: {snippet.error}{S.reset}")
        return

    full = f"src/policydesk/{snippet.path}"
    print()
    print(f"{S.magenta}{S.bold}  OPEN THIS FILE{S.reset}")
    print(
        f"{S.magenta}    {full}:{snippet.first_line}{S.reset}   {S.dim}({snippet.symbol}){S.reset}"
    )
    print(f'{S.dim}    code -g "{full}:{snippet.first_line}"{S.reset}')
    print()

    lines = snippet.code.splitlines()
    shown = lines[:max_lines]
    number = snippet.first_line
    for line in shown:
        print(f"{S.dim}{number:>5} |{S.reset} {line}")
        number += 1
    if len(lines) > max_lines:
        remaining = len(lines) - max_lines
        print(
            f"{S.dim}      | ... {remaining} more line(s) -- open the file to read the rest"
            f"{S.reset}"
        )
    print()


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


class Quit(Exception):
    """The user asked to leave. Not an error."""


def ask(prompt: str, *, default: str = "") -> str:
    """Read one line, treating Ctrl+C and a closed stdin as 'leave'.

    A tutor that prints a KeyboardInterrupt traceback when you press Ctrl+C is a
    tutor that feels broken. And handling EOF means this can be driven by a
    pipeline or a test without hanging forever.
    """
    suffix = f" {S.dim}[{default}]{S.reset}" if default else ""
    try:
        answer = input(f"{S.yellow}{prompt}{suffix}{S.reset} ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        raise Quit from None
    if answer.lower() in ("q", "quit", "exit"):
        raise Quit
    return answer or default


def ask_long(prompt: str, existing: str = "") -> str:
    """Collect an answer, finished with a blank line.

    Deliberately asks for writing rather than a keypress. The single habit this
    curriculum rests on is committing to an answer *before* seeing the right one,
    and pressing Enter is not a commitment.

    The instructions below are more explicit than they look like they need to be,
    because a bare `>` prompt after a question is genuinely ambiguous: a reader
    asked what this was and where the lesson had gone.
    """
    print()
    rule()
    print(f"{S.yellow}{S.bold}  {prompt}{S.reset}")
    print()
    if existing:
        print(f"{S.dim}  You wrote before: {existing[:150]}{S.reset}")
    print(f"{S.dim}  Type your guess and press Enter. One line is plenty.{S.reset}")
    print(f"{S.dim}  Then press Enter AGAIN on the empty line to continue.{S.reset}")
    print(f"{S.dim}  Nothing to say? Just press Enter twice -- or type 'skip'.{S.reset}")
    rule()

    lines: list[str] = []
    while True:
        try:
            line = input(f"{S.yellow}  your answer > {S.reset}")
        except (KeyboardInterrupt, EOFError):
            print()
            raise Quit from None
        if not line.strip():
            break
        if line.strip().lower() in ("skip", "s"):
            return ""
        lines.append(line.strip())
    return " ".join(lines).strip()


def confirm(prompt: str, default: bool = True) -> bool:
    answer = ask(f"{prompt} (y/n)", default="y" if default else "n")
    return answer.lower().startswith("y")


# ---------------------------------------------------------------------------
# Running real commands
# ---------------------------------------------------------------------------


def offer_to_run(label: str, arguments: str) -> None:
    """Offer to run the real command, and stream its output live.

    Streaming rather than capturing: you see it work, including the pauses while it
    waits on the API, which is itself informative about where the time goes.
    """
    from .app import costs_money

    display = (
        f"pytest {arguments.split(' ', 1)[1]}"
        if arguments.startswith("__pytest__")
        else f"policydesk {arguments}"
    )
    cost = (
        f"{S.yellow}costs a fraction of a cent{S.reset}"
        if costs_money(arguments)
        else f"{S.green}free{S.reset}"
    )

    print()
    print(f"{S.bold}  {label}{S.reset}  ({cost})")
    print(f"{S.cyan}    {display}{S.reset}")

    if not confirm("  Run it now?"):
        print(f"{S.dim}    Skipped. Run it yourself whenever you like.{S.reset}")
        return

    import subprocess

    if arguments.startswith("__pytest__"):
        command = [sys.executable, "-m", "pytest", *arguments.split()[1:]]
    else:
        import shlex

        command = [sys.executable, "-m", "policydesk.cli", *shlex.split(arguments)]

    print()
    rule()
    try:
        code = subprocess.call(command)
    except KeyboardInterrupt:
        print()
        print(f"{S.dim}    Stopped.{S.reset}")
        return
    rule()
    colour = S.green if code == 0 else S.yellow
    print(f"{colour}  exit code {code}{S.reset}")
    if code != 0:
        print(f"{S.dim}  A non-zero code may be exactly what the exercise wanted.{S.reset}")


# ---------------------------------------------------------------------------
# One guided step
# ---------------------------------------------------------------------------


def show_upcoming(step: Step) -> None:
    """Show WHAT is about to happen, before asking anyone to predict it.

    A bug found by a reader, and an obvious one in hindsight: the tutor asked
    "which document will it cite?" without ever showing the question being put to
    the system. That is not a prediction, it is a guessing game -- you cannot
    reason about a command you have not seen.
    """
    if not step.run:
        return
    command, extra = step.run
    display = f"policydesk {command} {extra}".strip()
    print(f"{S.bold}  WHAT WE ARE ABOUT TO RUN{S.reset}")
    for line in textwrap.wrap(display, width=WIDTH - 6):
        print(f"{S.cyan}    {line}{S.reset}")
    print()


def explain_the_rule_once(progress: StudyProgress) -> None:
    """Explain what a prediction is for -- but only the first time.

    Repeating it on every step would train you to skip the paragraph, and then
    the one time it matters you would skip that too.
    """
    if progress.steps or progress.tour:
        return
    rule()
    paragraph(
        f"{S.bold}How this works.{S.reset} You will be asked what you think will "
        "happen BEFORE anything runs. Write a guess -- one line is plenty, and being "
        "wrong is the whole point. Then the command runs for real, and only then do "
        "you get the explanation. Reading an answer first feels like learning and "
        "is not.",
        indent="  ",
    )
    rule()


def teach_step(
    step: Step, key: str, progress: StudyProgress, settings: Settings, position: str
) -> None:
    record = progress.step(key)
    first_time = not (progress.steps or progress.tour)

    heading(f"{position}  ::  {step.title}")
    paragraph(step.instruction, indent="  ")

    if first_time:
        explain_the_rule_once(progress)

    # 1. Show what is about to happen, so a prediction is possible at all.
    show_upcoming(step)

    # 2. Any code this step wants you to read.
    for path, symbol in step.read:
        show_snippet(read_symbol(path, symbol))

    # 3. Commit to an answer.
    if step.predict:
        written = ask_long(f"PREDICT FIRST: {step.predict}", record.prediction)
        progress.record_step(key, prediction=written)
        progress_store.save(progress, settings)
        if written:
            print(f"{S.dim}  Saved. Now let us see what actually happens.{S.reset}")

    # 4. Run it and watch reality -- BEFORE the explanation, or the run is spoiled.
    if step.run:
        command, extra = step.run
        offer_to_run("Now run it and watch", f"{command} {extra}".strip())

    # 5. Only now, the explanation.
    if step.predict:
        subheading("Why that happened")
        paragraph(step.answer, indent="  ")
        progress.record_step(key, revealed=True)

        if record.prediction or written:
            print(f"{S.dim}  You predicted: {(written or record.prediction)[:200]}{S.reset}")
            print()

    print()
    if confirm("  Mark this step as understood?"):
        progress.record_step(key, done=True)
        progress_store.save(progress, settings)
        print(f"{S.green}  Done.{S.reset}")


# ---------------------------------------------------------------------------
# One file from the tour
# ---------------------------------------------------------------------------


def teach_file(item: TourFile, progress: StudyProgress, settings: Settings, position: str) -> None:
    record = progress.tour_entry(item.key)

    heading(f"{position}  {item.path}")
    print(f"{S.bold}  {item.title}{S.reset}")
    print()
    paragraph(item.why, indent="  ")

    if item.show_module_doc:
        doc = read_module_docstring(item.path)
        if doc.found:
            subheading("Why this file exists, in its own words")
            print(f"{S.magenta}  src/policydesk/{item.path}:1{S.reset}")
            print()
            paragraph(doc.code, indent="  ")

    if item.symbols or item.constants:
        subheading("The parts worth reading")
        for symbol in item.symbols:
            show_snippet(read_symbol(item.path, symbol))
        for constant in item.constants:
            show_snippet(read_constant(item.path, constant))

    if item.question:
        written = ask_long(f"PREDICT FIRST: {item.question}", record.prediction)
        progress.record_tour(item.key, prediction=written, revealed=True)
        progress_store.save(progress, settings)

        subheading("The answer")
        paragraph(item.answer)

    for label, arguments in item.runs:
        offer_to_run(label, arguments)

    print()
    if confirm("  Mark this file as understood?"):
        progress.record_tour(item.key, done=True)
        progress_store.save(progress, settings)
        print(f"{S.green}  Done.{S.reset}")


# ---------------------------------------------------------------------------
# One drill question
# ---------------------------------------------------------------------------

ANSWER_SHAPE = (
    "define or confirm -> why it happens -> the cheapest fix first -> one trade-off "
    "-> one concrete example from this project"
)


def drill_question(
    item: DrillQuestion, progress: StudyProgress, settings: Settings, position: str
) -> None:
    record = progress.drill_entry(item.id)

    heading(f"{position}  {item.group}")
    paragraph(item.question, indent="  ")
    print(f"{S.dim}  Shape: {ANSWER_SHAPE}{S.reset}")
    print(
        f"{S.dim}  Say it OUT LOUD first, in four to six sentences. Then summarise it here."
        f"{S.reset}"
    )

    written = ask_long("Your answer", record.answer)
    progress.record_drill(item.id, answer=written, revealed=True, attempts=record.attempts + 1)
    progress_store.save(progress, settings)

    subheading("A model answer")
    paragraph(item.model_answer)

    if item.example:
        subheading("The example to use -- from your own code")
        paragraph(item.example)

    print()
    print(
        f"{S.dim}  5 = structured, specific, owns the decision, names the trade-off, has the"
        f" example{S.reset}"
    )
    print(
        f"{S.dim}  4 = one element missing   3 = correct but generic   2 = vague   1 = wrong"
        f"{S.reset}"
    )
    score = ask("  Score your own answer 1-5 (honestly)", default=str(record.score or ""))
    if score.isdigit() and 1 <= int(score) <= 5:
        progress.record_drill(item.id, score=int(score))
        progress_store.save(progress, settings)
        print(f"{S.green}  Recorded {score}/5.{S.reset}")


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------


def session_keys(session: Session) -> list[str]:
    return [f"s{session.number}-step{i}" for i in range(len(session.steps))]


def next_unfinished_step(progress: StudyProgress) -> tuple[Session, int] | None:
    for session in SESSIONS:
        for index in range(len(session.steps)):
            if not progress.step(f"s{session.number}-step{index}").done:
                return session, index
    return None


def next_unfinished_file(progress: StudyProgress) -> TourFile | None:
    for item in TOUR:
        if not progress.tour_entry(item.key).done:
            return item
    return None


def show_status(progress: StudyProgress, settings: Settings) -> None:
    heading("Where things stand")

    step_keys = [key for session in SESSIONS for key in session_keys(session)]
    tour_keys = [item.key for item in TOUR]
    steps_done = progress.completed_steps(step_keys)
    files_done = progress.completed_tour(tour_keys)
    mean = progress.mean_drill_score

    def bar(done: int, total: int) -> str:
        filled = round(24 * done / total) if total else 0
        return f"[{'#' * filled}{'.' * (24 - filled)}] {done}/{total}"

    print(f"  Guided steps     {bar(steps_done, len(step_keys))}")
    print(f"  Files understood {bar(files_done, len(tour_keys))}")
    print(
        f"  Drill scored     {bar(len(progress.drill_scores), len(DRILL))}"
        + (f"   mean {mean:.2f}/5" if mean is not None else "")
    )

    if mean is not None and mean < 3.5:
        print()
        paragraph(
            f"{S.yellow}A mean under 3.5 usually means delivery, not knowledge: an answer "
            f"arriving as a keyword list with no 'why' and no example. Re-answer the "
            f"lowest-scored ones out loud, in the shape above.{S.reset}",
            indent="  ",
        )

    weakest = progress.weakest_drill()
    if weakest:
        print()
        print(f"{S.bold}  Weakest answers so far -- practise these first:{S.reset}")
        for key, score in weakest:
            question = next((q for q in DRILL if q.id == key), None)
            if question:
                bullet(f"{key} ({score}/5) {question.question}")

    print()
    print(f"{S.dim}  Saved to {progress_store.progress_path(settings)}{S.reset}")


def run_guided(progress: StudyProgress, settings: Settings) -> None:
    while True:
        heading("Guided path")
        for session in SESSIONS:
            keys = session_keys(session)
            done = progress.completed_steps(keys)
            mark = f"{S.green}done{S.reset}" if done == len(keys) else f"{done}/{len(keys)}"
            minutes = f"{S.dim}{session.minutes}min{S.reset}"
            print(f"  {session.number}. {session.title:<42} {mark}  {minutes}")

        choice = ask("\n  Session number, or 'b' to go back", default="b")
        if choice.lower().startswith("b"):
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(SESSIONS):
            print(f"{S.red}  Not a session number.{S.reset}")
            continue

        session = SESSIONS[int(choice) - 1]
        heading(f"Session {session.number} -- {session.title}")
        paragraph(f"{S.bold}Aim:{S.reset} {session.aim}", indent="  ")

        for index, step in enumerate(session.steps):
            key = f"s{session.number}-step{index}"
            if progress.step(key).done and not confirm(
                f"  Step {index + 1} '{step.title}' is already done. Redo it?", default=False
            ):
                continue
            teach_step(step, key, progress, settings, f"Session {session.number}, step {index + 1}")
            if index < len(session.steps) - 1 and not confirm("\n  Continue to the next step?"):
                break


def run_tour(progress: StudyProgress, settings: Settings) -> None:
    from .content import tour_groups

    while True:
        heading("Code tour -- every file in the project")
        groups = tour_groups()
        for number, group in enumerate(groups, start=1):
            files = [item for item in TOUR if item.group == group]
            done = progress.completed_tour([f.key for f in files])
            print(f"  {number}. {group:<36} {done}/{len(files)} understood")

        choice = ask(
            "\n  Group number, 'n' for the next unfinished file, or 'b' to go back", default="n"
        )
        if choice.lower().startswith("b"):
            return
        if choice.lower().startswith("n"):
            item = next_unfinished_file(progress)
            if item is None:
                print(f"{S.green}  Every file is marked understood.{S.reset}")
                continue
            teach_file(item, progress, settings, "Next unfinished")
            continue
        if not choice.isdigit() or not 1 <= int(choice) <= len(groups):
            print(f"{S.red}  Not a group number.{S.reset}")
            continue

        group = groups[int(choice) - 1]
        files = [item for item in TOUR if item.group == group]
        for position, item in enumerate(files, start=1):
            if progress.tour_entry(item.key).done and not confirm(
                f"  {item.path} is already done. Redo it?", default=False
            ):
                continue
            teach_file(item, progress, settings, f"{group}, file {position} of {len(files)}")
            if position < len(files) and not confirm("\n  Continue to the next file?"):
                break


def run_drill(progress: StudyProgress, settings: Settings) -> None:
    from .content import drill_groups

    while True:
        heading("Interview drill")
        mean = progress.mean_drill_score
        print(
            f"  {len(progress.drill_scores)} of {len(DRILL)} scored"
            + (f", mean {mean:.2f}/5" if mean is not None else "")
        )
        print()
        groups = drill_groups()
        for number, group in enumerate(groups, start=1):
            questions = [q for q in DRILL if q.group == group]
            scored = sum(1 for q in questions if progress.drill_entry(q.id).score)
            print(f"  {number}. {group:<24} {scored}/{len(questions)} scored")
        print(f"  {len(groups) + 1}. Weakest first")

        choice = ask("\n  Area number, or 'b' to go back", default="b")
        if choice.lower().startswith("b"):
            return
        if not choice.isdigit():
            continue

        index = int(choice)
        if index == len(groups) + 1:
            weakest = progress.weakest_drill(limit=len(DRILL))
            ordered = [q for key, _ in weakest for q in DRILL if q.id == key]
            unscored = [q for q in DRILL if progress.drill_entry(q.id).score is None]
            questions = ordered + unscored
        elif 1 <= index <= len(groups):
            questions = [q for q in DRILL if q.group == groups[index - 1]]
        else:
            continue

        for position, item in enumerate(questions, start=1):
            drill_question(item, progress, settings, f"Question {position} of {len(questions)}")
            if position < len(questions) and not confirm("\n  Next question?"):
                break


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

BANNER = r"""
  ____       _ _            ____            _
 |  _ \ ___ | (_) ___ _   _|  _ \  ___  ___| | __
 | |_) / _ \| | |/ __| | | | | | |/ _ \/ __| |/ /
 |  __/ (_) | | | (_| |_| | |_| |  __/\__ \   <
 |_|   \___/|_|_|\___\__, |____/ \___||___/_|\_\
                     |___/      learn it, step by step
"""


def welcome(progress: StudyProgress) -> None:
    print(f"{S.cyan}{BANNER}{S.reset}")
    paragraph(
        "This walks you through an agentic RAG system you can run: the real code, "
        "the real commands, and exactly which file to open for each part.",
        indent="  ",
    )
    paragraph(
        f"{S.bold}One rule.{S.reset} Every step asks you to write an answer BEFORE it shows "
        "you the right one. Being wrong in writing is what makes this stick; being wrong in "
        "your head does not.",
        indent="  ",
    )

    nxt = next_unfinished_step(progress)
    if nxt is not None:
        session, index = nxt
        print(
            f"{S.green}  Next up:{S.reset} Session {session.number}, step {index + 1} -- "
            f"{session.steps[index].title}"
        )
    else:
        print(f"{S.green}  Every guided step is done. Try the code tour or the drill.{S.reset}")
    print()


def main_menu(progress: StudyProgress, settings: Settings) -> None:
    while True:
        print()
        rule("=")
        print(f"{S.bold}  What would you like to do?{S.reset}")
        rule("=")
        print("  1. Continue where I left off")
        print("  2. Guided path          -- 7 sessions, predict then run")
        print(f"  3. Code tour            -- all {len(TOUR)} files, one at a time")
        print(f"  4. Interview drill      -- {len(DRILL)} questions, scored")
        print("  5. Where things stand")
        print("  q. Quit")

        choice = ask("\n  Choose", default="1")

        if choice == "1":
            nxt = next_unfinished_step(progress)
            if nxt is None:
                item = next_unfinished_file(progress)
                if item is None:
                    print(f"{S.green}  Nothing unfinished. Try the drill.{S.reset}")
                    continue
                teach_file(item, progress, settings, "Next unfinished file")
            else:
                session, index = nxt
                teach_step(
                    session.steps[index],
                    f"s{session.number}-step{index}",
                    progress,
                    settings,
                    f"Session {session.number}, step {index + 1}",
                )
        elif choice == "2":
            run_guided(progress, settings)
        elif choice == "3":
            run_tour(progress, settings)
        elif choice == "4":
            run_drill(progress, settings)
        elif choice == "5":
            show_status(progress, settings)
        else:
            print(f"{S.dim}  Not an option. Enter 1-5, or q to quit.{S.reset}")


def run(settings: Settings) -> int:
    """Start the tutor. Returns a process exit code."""
    global _UTF8_OK
    _UTF8_OK = enable_utf8_output()

    progress = progress_store.load(settings)
    try:
        welcome(progress)
        main_menu(progress, settings)
    except Quit:
        pass
    finally:
        progress_store.save(progress, settings)

    print()
    rule("=")
    steps = sum(len(s.steps) for s in SESSIONS)
    done = progress.completed_steps([key for session in SESSIONS for key in session_keys(session)])
    print(
        f"{S.green}  Progress saved: {done}/{steps} guided steps, "
        f"{progress.completed_tour([i.key for i in TOUR])}/{len(TOUR)} files.{S.reset}"
    )
    print(f"{S.dim}  Come back any time with:  policydesk learn{S.reset}")
    rule("=")
    return 0
