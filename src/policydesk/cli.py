"""Command line entry point.

Commands
--------
doctor         check configuration and report readiness
generate-data  build the sample policy PDFs, spreadsheet and database
extract        read the sample files and show what came out, with locations
inspect-db     show the database tables and a couple of example queries
cost           estimate what the whole project will cost in model usage
chunks         show how the documents were split into chunks (free)
index          embed the chunks and build the vector index
ask            ask a question over the indexed documents
cache-clear    delete all cached model responses
agent          ask the full agent: it plans, uses tools, verifies, and may pause
approve        approve an action a paused agent proposed
reject         reject an action a paused agent proposed
queue          show the Finance Operations review queue
diagram        print the orchestration diagram as Mermaid
mcp-server     run the MCP server over stdin/stdout
mcp-demo       connect to the MCP server, discover its tools and call them
eval-cases     list the evaluation dataset and what each case checks (free)
eval           run the evaluation, score the system, optionally save the report
eval-compare   compare the two most recent saved runs, honestly about noise
langfuse-setup show how to run Langfuse locally and whether it is reachable

Every command is read-only with respect to your settings, prints what it did,
and returns a process exit code (0 = fine). That last part matters: a command
that prints "error" but exits 0 will quietly pass in a CI pipeline forever.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from pydantic import ValidationError

from .config import Settings, get_settings
from .ingest import ExtractionReport, extract_excel, extract_pdf
from .logging_setup import configure_logging, get_logger


def _load_settings_or_explain() -> Settings | None:
    """Load settings, or print a clear explanation and return None."""
    try:
        return get_settings()
    except ValidationError as exc:
        print("Configuration problem. The app did not start.\n", file=sys.stderr)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "(settings)"
            print(f"  - {location}: {error['msg']}", file=sys.stderr)
        print("\nHint: copy .env.example to .env and fill it in.", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def cmd_doctor() -> int:
    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    configure_logging(settings)
    log = get_logger("policydesk.doctor")

    print("PolicyDesk configuration")
    print("-" * 46)
    for key, value in settings.safe_summary().items():
        print(f"  {key:<20} {value}")
    print("-" * 46)

    ready = settings.is_test or settings.llm_provider != "openai" or bool(settings.openai_api_key)
    print("Status: READY" if ready else "Status: NOT READY - no model credentials")

    # Proof that redaction works: we deliberately try to log a fake secret.
    log.info("doctor_check_complete", ready=ready, demo_leak="sk-THIS_SHOULD_BE_HIDDEN_12345")
    return 0 if ready else 1


# ---------------------------------------------------------------------------
# generate-data
# ---------------------------------------------------------------------------


def cmd_generate_data() -> int:
    from .sample_data import generate_all  # imported lazily: heavy, rarely needed

    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    created = generate_all(settings.data_dir, settings.sqlite_path)

    print("Sample data created:\n")
    for label, path in created.items():
        size_kb = path.stat().st_size / 1024
        print(f"  {label:<22} {path.relative_to(Path.cwd()) if path.is_absolute() else path}")
        print(f"  {'':<22} {size_kb:,.1f} KB")
    print("\nNext: policydesk extract")
    return 0


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


def _print_report(report: ExtractionReport, show_blocks: int) -> None:
    print(f"\n{'=' * 78}")
    print(f"FILE     {report.source_id}")
    print(f"TYPE     {report.source_type}")
    if report.pages is not None:
        print(f"PAGES    {report.pages}")
    if report.sheets:
        print(f"SHEETS   {', '.join(report.sheets)}")
    print(f"BLOCKS   {report.block_count}  ({report.total_chars:,} characters)")

    for note in report.notes:
        print(f"  note     {note}")
    for warning in report.warnings:
        print(f"  WARNING  {warning}")

    for block in report.blocks[:show_blocks]:
        preview = block.text.replace("\n", " ")
        if len(preview) > 150:
            preview = preview[:150] + "..."
        print(f"\n  [{block.kind}] cite as: {block.citation}")
        print(f"      {preview}")

    hidden = report.block_count - min(show_blocks, report.block_count)
    if hidden > 0:
        print(f"\n  ... {hidden} more block(s) not shown (use --show to see more)")


def cmd_extract(show_blocks: int) -> int:
    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    raw = settings.data_dir / "raw"
    pdfs = sorted(raw.glob("**/*.pdf"))
    workbooks = sorted(raw.glob("**/*.xlsx"))

    if not pdfs and not workbooks:
        print(f"No source files under {raw}.", file=sys.stderr)
        print("Run this first:  policydesk generate-data", file=sys.stderr)
        return 1

    reports = [extract_pdf(path) for path in pdfs] + [extract_excel(path) for path in workbooks]
    for report in reports:
        _print_report(report, show_blocks)

    print(f"\n{'=' * 78}")
    print("SUMMARY")
    for report in reports:
        print(f"  {report.summary_line()}")

    needs_review = [r for r in reports if r.needs_human_review]
    print(f"\n  {len(reports)} file(s), {sum(r.block_count for r in reports)} block(s) total")
    if needs_review:
        print(f"  {len(needs_review)} file(s) need a human to look at them:")
        for report in needs_review:
            print(f"    - {report.source_id}")
        print("\n  This is the point of the exercise. A pipeline that hides these")
        print("  files indexes nothing and reports success.")

    # Exit code stays 0: files needing review is expected, not a crash.
    return 0


# ---------------------------------------------------------------------------
# inspect-db
# ---------------------------------------------------------------------------

EXAMPLE_QUERIES: list[tuple[str, str]] = [
    (
        "Rows per table",
        """SELECT 'departments' AS table_name, COUNT(*) AS rows FROM departments
           UNION ALL SELECT 'employees', COUNT(*) FROM employees
           UNION ALL SELECT 'expenses', COUNT(*) FROM expenses
           UNION ALL SELECT 'budgets', COUNT(*) FROM budgets""",
    ),
    (
        "Q3 2026 travel-related spend by department",
        """SELECT d.name AS department, ROUND(SUM(e.amount_usd), 2) AS spend_usd
           FROM expenses e
           JOIN employees   em ON em.id = e.employee_id
           JOIN departments d  ON d.id  = em.department_id
           WHERE e.expense_date BETWEEN '2026-07-01' AND '2026-09-30'
           GROUP BY d.name
           ORDER BY spend_usd DESC""",
    ),
    (
        "Priya Raman's October 2026 expenses",
        """SELECT e.id, e.expense_date, e.category, e.amount_usd, e.country,
                  e.receipt_attached, e.approval_ref
           FROM expenses e
           JOIN employees em ON em.id = e.employee_id
           WHERE em.name = 'Priya Raman'
             AND e.expense_date BETWEEN '2026-10-01' AND '2026-10-31'
           ORDER BY e.expense_date""",
    ),
]


def cmd_inspect_db() -> int:
    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    if not settings.sqlite_path.exists():
        print(f"No database at {settings.sqlite_path}.", file=sys.stderr)
        print("Run this first:  policydesk generate-data", file=sys.stderr)
        return 1

    # Opening read-only, because nothing in this command should ever write.
    uri = f"file:{settings.sqlite_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        for title, sql in EXAMPLE_QUERIES:
            print(f"\n{title}")
            print("-" * len(title))
            cursor = connection.execute(sql)
            columns = [description[0] for description in cursor.description]
            rows = cursor.fetchall()
            widths = [
                max(len(columns[i]), *(len(str(row[i])) for row in rows))
                if rows
                else len(columns[i])
                for i in range(len(columns))
            ]
            print("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns)))
            print("  " + "  ".join("-" * widths[i] for i in range(len(columns))))
            for row in rows:
                print("  " + "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(row)))
    finally:
        connection.close()

    print("\nNote: the policy LIMITS are not in this database on purpose.")
    print("They live only in the PDF. That is what forces the agent to use both.")
    return 0


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------


def cmd_cost() -> int:
    """Estimate what the whole project will cost in model usage."""
    from .llm.estimate import (
        CORPUS_TOKENS,
        PIPELINE,
        QUESTION_VOLUME,
        SCENARIOS,
        cost_per_question,
        per_milestone,
        project_total,
        unknown_models,
    )
    from .llm.pricing import PRICES_SOURCE_URL, PRICES_VERIFIED_ON, price_age_warning

    missing = unknown_models()
    if missing:
        print(f"No price recorded for: {', '.join(missing)}", file=sys.stderr)
        return 1

    print("PolicyDesk - project cost estimate")
    print("=" * 72)
    print(f"Prices verified {PRICES_VERIFIED_ON.isoformat()} from {PRICES_SOURCE_URL}")
    warning = price_age_warning()
    if warning:
        print(f"WARNING: {warning}")
    print()
    print("Assumptions (all editable in src/policydesk/llm/estimate.py):")
    print(f"  corpus size            ~{CORPUS_TOKENS:,} tokens (measured from our files)")
    print(f"  question-runs total     {sum(QUESTION_VOLUME.values()):,} across milestones 2-9")
    print(f"  pipeline steps          {len(PIPELINE)} model calls per question")
    print()

    for scenario in SCENARIOS:
        totals = project_total(scenario)
        grand = sum(totals.values())
        per_q = sum(cost_per_question(scenario).values())

        print("-" * 72)
        print(f"{scenario.name}")
        print(
            f"  models: cheap={scenario.cheap_model}  strong={scenario.strong_model}  "
            f"judge={scenario.judge_model}"
        )
        print(f"  {scenario.note}")
        print()
        print(f"  cost per question     ${per_q:.5f}   ({per_q * 100:.3f} cents)")
        for label, amount in totals.items():
            print(f"  {label:<22} ${amount:>8.2f}")
        print(f"  {'PROJECT TOTAL':<22} ${grand:>8.2f}")
        print()

    recommended = SCENARIOS[0]
    print("=" * 72)
    print(f"Per-milestone breakdown for {recommended.name}:")
    for milestone, amount in per_milestone(recommended).items():
        print(f"  {milestone:<42} ${amount:>7.2f}")
    print()
    print("These are estimates from stated assumptions, not measurements. From")
    print("Milestone 2 the CostLedger records real spend, which overrides all of this.")
    print()
    print("Set a hard monthly usage limit in your OpenAI dashboard as well. That")
    print("limit cannot be bypassed by a bug in our code; ours can.")
    return 0


# ---------------------------------------------------------------------------
# chunks, index, ask, cache-clear  (Milestone 2)
# ---------------------------------------------------------------------------


def cmd_chunks(show: int) -> int:
    """Show how the documents were cut up. Makes no model calls, so it is free."""
    from .pipeline import build_chunks
    from .rag import chunk_stats

    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    chunks, reports = build_chunks(settings)
    if not chunks:
        print("No chunks. Run 'policydesk generate-data' first.", file=sys.stderr)
        return 1

    stats = chunk_stats(chunks)
    print(
        f"Chunking strategy: {settings.chunk_strategy} "
        f"(size={settings.chunk_size_chars}, overlap={settings.chunk_overlap_chars})"
    )
    print("-" * 78)
    print(f"  chunks                      {stats['count']}")
    print(f"  total characters            {stats['total_chars']:,}")
    print(
        f"  smallest / median / largest {stats['smallest']} / {stats['median']} / "
        f"{stats['largest']} chars"
    )
    print(f"  with a section heading      {stats['with_section']} of {stats['count']}")
    print()

    for chunk in chunks[:show]:
        preview = " ".join(chunk.text.split())
        if len(preview) > 110:
            preview = preview[:110] + "..."
        print(f"  {chunk.char_count:>4}ch  [{chunk.kind}]  {chunk.citation}")
        print(f"          {preview}")

    hidden = len(chunks) - min(show, len(chunks))
    if hidden > 0:
        print(f"\n  ... {hidden} more (use --show {len(chunks)} to see all)")

    flagged = [report for report in reports if report.needs_human_review]
    if flagged:
        print(
            f"\n  note: {len(flagged)} source file(s) still need human review "
            "(run 'policydesk extract' for the detail)"
        )
    return 0


def cmd_index() -> int:
    """Embed every chunk and write the vector index. This one spends money."""
    from .pipeline import make_client, reindex

    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    configure_logging(settings)
    try:
        llm = make_client(settings)
    except Exception as exc:  # noqa: BLE001 - show the reason, not a traceback
        print(f"Could not start the model client: {exc}", file=sys.stderr)
        return 1

    print(
        f"Building the index with {settings.embedding_model} "
        f"(strategy={settings.chunk_strategy})..."
    )
    try:
        count, _ = reindex(settings, llm)
    except Exception as exc:  # noqa: BLE001
        print(f"Index build failed: {exc}", file=sys.stderr)
        return 1

    print(f"Indexed {count} chunks.")
    print(f"Usage: {llm.usage_summary()}")
    print(f"Index written to {settings.vector_store_dir}")
    print()
    print('Next:  policydesk ask "What is the per-diem limit for international travel?"')
    return 0


def cmd_ask(question: str, show_chunks: bool) -> int:
    """Ask one question over the indexed documents."""
    from .pipeline import ask, make_client
    from .rag import IndexMismatch

    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    configure_logging(settings)
    if len(question) > settings.max_input_chars:
        print(
            f"Question is {len(question)} characters, over the {settings.max_input_chars} limit.",
            file=sys.stderr,
        )
        return 1

    try:
        llm = make_client(settings)
        result = ask(question, settings, llm)
    except IndexMismatch as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    answer = result.answer

    print()
    print(f"Q: {question}")
    print("=" * 78)
    if answer.refused:
        print("ANSWER: I do not know.")
        print(f"        Missing: {answer.missing}")
    else:
        print(answer.text)

    if answer.citations:
        print()
        print("Sources used:")
        for number, citation in zip(answer.cited_numbers, answer.citations, strict=False):
            print(f"  [{number}] {citation}")
    elif not answer.refused:
        print()
        print("  WARNING: the answer cited nothing. Treat it as unverified.")

    if answer.invalid_citation_numbers:
        print()
        print(
            f"  WARNING: cited chunks that do not exist: {answer.invalid_citation_numbers}. "
            "The model invented a source."
        )

    if show_chunks:
        print()
        print("-" * 78)
        print("RETRIEVED CHUNKS (everything the model was allowed to read)")
        for chunk in answer.chunks:
            used = "USED" if chunk.rank in answer.cited_numbers else "    "
            body = " ".join(chunk.text.split())
            print()
            print(f"  [{chunk.rank}] {used}  {chunk.citation}")
            print(f"       {chunk.why_selected}")
            print(f"       {body[:150]}{'...' if len(body) > 150 else ''}")

    print()
    print("-" * 78)
    print(f"Usage: {result.usage}")
    return 0


def cmd_cache_clear() -> int:
    """Delete every cached model response."""
    from .llm.cache import ResponseCache

    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    cache = ResponseCache(settings.data_dir / "processed" / "llm-cache", enabled=True)
    removed = cache.clear()
    print(f"Removed {removed} cached response(s).")
    print("The next run will call the API again and cost real money.")
    return 0


# ---------------------------------------------------------------------------
# agent, approve, reject, queue, diagram  (Milestone 4)
# ---------------------------------------------------------------------------


def _print_run(run, show_trace: bool) -> None:
    """Render one agent run: the answer, its sources, and optionally every step."""
    state = run.state

    print()
    print("=" * 78)
    if state.get("refused"):
        print("ANSWER: I do not know.")
        print(f"        Missing: {state.get('missing')}")
    else:
        print(state.get("answer") or "(no answer produced)")

    citations = state.get("citations") or []
    if citations:
        print()
        print("Sources used:")
        for number, citation in zip(state.get("cited_numbers") or [], citations, strict=False):
            print(f"  [{number}] {citation}")

    evidence = state.get("evidence") or []
    queries = [item.sql for item in evidence if getattr(item, "sql", None)]
    if queries:
        print()
        print("Queries run (check these: they show how the question was understood):")
        for sql in queries:
            for line in sql.splitlines():
                print(f"    {line}")

    if state.get("verify_problem"):
        print()
        print(f"  Verification: {state['verify_problem']}")
    elif state.get("verified") and not state.get("refused"):
        print()
        print("  Verification: passed (citations exist, amounts appear in the evidence)")

    if state.get("action_result"):
        print()
        print(f"  Action: {state['action_result']}")

    errors = state.get("errors") or []
    if errors:
        print()
        print("Notes and failures along the way:")
        for error in errors:
            print(f"  - {error}")

    if show_trace:
        print()
        print("-" * 78)
        print("TRACE (every step, in order)")
        for step, line in enumerate(state.get("trace") or [], start=1):
            print(f"  {step:>2}. {line}")

        tracer = getattr(run, "tracer", None)
        if tracer is not None and tracer.spans:
            print()
            print("TIMING (where the wall-clock time went)")
            for line in tracer.summary_lines():
                print(line)
            print(f"  {'total':<16} {tracer.total_ms:>6}ms")
            slowest = tracer.slowest(1)
            if slowest:
                print(f"  slowest step: {slowest[0].name} ({slowest[0].duration_ms}ms)")
            print(f"  {tracer.status_line()}")

    print()
    print("-" * 78)
    print(f"Thread: {run.thread_id}")
    print(f"Usage:  {run.usage}")


def _print_pending(run) -> None:
    """Show a proposed write and how to approve or reject it."""
    action = run.state.get("pending_action")
    print()
    print("!" * 78)
    print("THE AGENT HAS STOPPED AND IS WAITING FOR YOU")
    print("!" * 78)
    print()
    print("It wants to perform an action that changes data. It has NOT done it.")
    print()
    if action is not None:
        print(f"  Tool      : {action.tool}")
        print(f"  Rationale : {action.rationale}")
        print("  Arguments :")
        for key, value in action.arguments.items():
            print(f"      {key} = {value!r}")
    print()
    print("The whole run is saved to disk. Decide whenever you like:")
    print(f"    policydesk approve {run.thread_id}")
    print(f"    policydesk reject  {run.thread_id}")


def cmd_agent(question: str, show_trace: bool, allow_actions: bool) -> int:
    from .pipeline import run_agent
    from .rag import IndexMismatch

    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    configure_logging(settings)
    try:
        run = run_agent(question, settings, allow_actions=allow_actions)
    except IndexMismatch as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"The agent failed: {exc}", file=sys.stderr)
        return 1

    print(f"Q: {question}")
    _print_run(run, show_trace)
    if run.waiting_for_approval:
        _print_pending(run)
    return 0


def cmd_decide(thread_id: str, approval: str, show_trace: bool) -> int:
    from .pipeline import resume_agent

    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    configure_logging(settings)
    try:
        run = resume_agent(thread_id, approval, settings)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Could not resume: {exc}", file=sys.stderr)
        return 1

    print(f"Decision recorded: {approval}")
    _print_run(run, show_trace)
    return 0


def cmd_queue() -> int:
    """Show the Finance Operations review queue."""
    import json

    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    path = settings.data_dir / "processed" / "review-queue.jsonl"
    if not path.exists():
        print("The review queue is empty (no file yet).")
        return 0

    entries = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not entries:
        print("The review queue is empty.")
        return 0

    print(f"Review queue: {len(entries)} entry/entries  ({path})")
    for entry in entries:
        print()
        print(
            f"  expense {entry['expense_id']}  {entry['employee_name']}  "
            f"{entry['category']}  USD {entry['amount_usd']}"
        )
        print(f"    reason   : {entry['reason']}")
        print(f"    citation : {entry['policy_citation']}")
        print(f"    flagged  : {entry['flagged_at']} by {entry['flagged_by']}")
        print(f"    decision : {entry['decision']}")
    return 0


def cmd_diagram() -> int:
    """Print the orchestration diagram as Mermaid."""
    from .graph import mermaid_diagram

    print("```mermaid")
    print(mermaid_diagram().rstrip())
    print("```")
    return 0


# ---------------------------------------------------------------------------
# mcp-server, mcp-demo  (Milestone 6)
# ---------------------------------------------------------------------------


def cmd_mcp_server() -> int:
    """Run the MCP server over stdin/stdout. A client launches this."""
    settings = _load_settings_or_explain()
    if settings is None:
        return 1
    from .mcp_server import run_stdio

    run_stdio()
    return 0


def cmd_mcp_demo() -> int:
    """Start the server as a subprocess, discover its tools, call them."""
    import asyncio

    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    from .mcp_client import demo

    try:
        return asyncio.run(demo())
    except BaseException as exc:  # noqa: BLE001
        # An MCP session runs its transport and protocol in a task group, so a
        # failure arrives wrapped in an ExceptionGroup. Printing only the group
        # says "unhandled errors in a TaskGroup" and tells you nothing at all --
        # the real cause is nested inside, sometimes two levels deep.
        print("MCP demo failed:", file=sys.stderr)
        for line in _flatten_exception(exc):
            print(f"  {line}", file=sys.stderr)
        return 1


def _flatten_exception(exc: BaseException, depth: int = 0) -> list[str]:
    """Walk an ExceptionGroup tree and return one readable line per real error."""
    indent = "  " * depth
    inner = getattr(exc, "exceptions", None)
    if inner:
        lines = [f"{indent}{type(exc).__name__}:"]
        for child in inner:
            lines += _flatten_exception(child, depth + 1)
        return lines
    return [f"{indent}{type(exc).__name__}: {exc}"]


# ---------------------------------------------------------------------------
# eval, eval-compare, eval-cases  (Milestone 7)
# ---------------------------------------------------------------------------


def _fmt(value: float | None, width: int = 5) -> str:
    return f"{value:.3f}".rjust(width) if value is not None else "  -  "


def cmd_eval_cases() -> int:
    """List the evaluation set. Free: no model calls."""
    from .evaluation import CASES, dataset_summary

    print("Evaluation dataset")
    print("-" * 78)
    for name, count in dataset_summary().items():
        print(f"  {name:<16} {count}")
    print()

    for case in CASES:
        print(f"  {case.id:<10} [{case.category}]")
        print(f"      Q: {case.question}")
        if case.must_contain:
            print(f"      must contain     : {', '.join(case.must_contain)}")
        if case.must_not_contain:
            print(f"      must NOT contain : {', '.join(case.must_not_contain)}")
        if case.should_refuse:
            print("      must refuse      : yes")
        if case.note:
            print(f"      why              : {case.note}")
        print()
    return 0


def cmd_eval(label: str, category: str | None, limit: int | None, save: bool) -> int:
    """Run the evaluation and print a scored report."""
    from .evaluation import run_evaluation, save_report

    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    configure_logging(settings)

    print(f"Running evaluation: {label or 'unlabelled'}")
    print(
        f"Configuration: chunk={settings.chunk_strategy}/{settings.chunk_size_chars}, "
        f"embed={settings.embedding_model}, answer={settings.answer_model}, "
        f"k={settings.retrieval_top_k}"
    )
    print("-" * 78)

    def show(record) -> None:  # noqa: ANN001
        mark = "PASS" if record.passed else "FAIL"
        print(
            f"  {mark}  {record.id:<10} {record.duration_s:>5.1f}s  ${record.cost_usd:.4f}"
            + (f"  {record.reason}" if record.reason else "")
        )

    try:
        report = run_evaluation(
            settings,
            label=label,
            category=category,  # type: ignore[arg-type]
            limit=limit,
            on_case=show,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 1

    print("-" * 78)
    print("RESULTS")
    print(f"  pass rate            {report.passed}/{report.total} = {report.pass_rate:.1%}")
    print(f"  retrieval recall@k   {_fmt(report.mean('recall'))}")
    print(f"  retrieval MRR        {_fmt(report.mean('mrr'))}")
    print(f"  tool choice          {_fmt(report.mean('tool_score'))}")
    print(f"  total cost           ${report.total_cost_usd:.4f}")
    print()
    print("  By category:")
    for name, (passed, total) in report.by_category().items():
        print(f"    {name:<16} {passed}/{total}")

    failures = report.failures()
    if failures:
        print()
        print(f"  {len(failures)} failure(s):")
        for record in failures:
            print(f"    {record.id:<10} {record.reason}")
            if record.answer_preview:
                print(f"               answered: {record.answer_preview[:110]}")

    print()
    print("  What these numbers do NOT tell you:")
    print("    - recall says nothing about whether the answer USED what was found")
    print("    - a pass rate on 22 cases cannot estimate a true accuracy; it can")
    print("      catch a regression and compare two configurations, which is enough")

    if save:
        path = save_report(report, settings)
        print()
        print(f"  Saved to {path}")
        print("  Compare two runs with:  policydesk eval-compare")
    return 0


def cmd_eval_compare() -> int:
    """Compare the two most recent saved runs."""
    from .evaluation import compare, latest_reports

    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    reports = latest_reports(settings, count=2)
    if len(reports) < 2:
        print("Need at least two saved runs. Run 'policydesk eval --save' twice.", file=sys.stderr)
        return 1

    candidate, baseline = reports[0], reports[1]
    result = compare(baseline, candidate)

    print("Comparison")
    print("-" * 78)
    print(f"  baseline  {result.baseline_label}")
    print(f"            {result.baseline_pass_rate:.1%}")
    print(f"  candidate {result.candidate_label}")
    print(f"            {result.candidate_pass_rate:.1%}")
    print()
    print(f"  delta           {result.delta:+.1%}")
    print(f"  newly passing   {', '.join(result.newly_passing) or 'none'}")
    print(f"  newly failing   {', '.join(result.newly_failing) or 'none'}")
    print(f"  unchanged       {result.unchanged}")
    print(f"  cases moved     {result.churn}")
    print()
    print(f"  Reading: {result.verdict()}")
    return 0


# ---------------------------------------------------------------------------
# langfuse-setup  (Milestone 8)
# ---------------------------------------------------------------------------


def cmd_langfuse_setup() -> int:
    """Print what is needed to run Langfuse locally, and the current status."""
    from .observability import Tracer, docker_compose_snippet

    settings = _load_settings_or_explain()
    if settings is None:
        return 1

    tracer = Tracer(settings)
    print("Langfuse status")
    print("-" * 78)
    print(f"  {tracer.status_line()}")
    print()
    print("Tracing is OPTIONAL. Everything works without it: `agent --trace` already")
    print("prints the step-by-step trace and a timing breakdown, with no account and")
    print("no network. Langfuse adds history across runs and a view somebody else can")
    print("look at.")
    print()
    print("To run it locally, free and self-hosted:")
    print()
    print(docker_compose_snippet())
    print("Then set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in .env.")
    print()
    print("What we send: span names, durations, attempt numbers. NOT prompt text and")
    print("NOT document content -- a retrieved chunk contains names and email")
    print("addresses, and shipping those to any service copies personal data out of")
    print("your system, where it is kept and is subject to deletion requests.")
    return 0


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="policydesk", description="PolicyDesk tools")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check configuration and report readiness")
    sub.add_parser("generate-data", help="build the sample PDFs, spreadsheet and database")
    extract_parser = sub.add_parser("extract", help="read source files and show what came out")
    extract_parser.add_argument(
        "--show",
        type=int,
        default=3,
        metavar="N",
        help="how many extracted blocks to preview per file (default 3)",
    )
    sub.add_parser("inspect-db", help="show the database contents and example queries")
    sub.add_parser("cost", help="estimate the model cost of the whole project")

    chunks_parser = sub.add_parser("chunks", help="show how documents were split (free)")
    chunks_parser.add_argument(
        "--show",
        type=int,
        default=8,
        metavar="N",
        help="how many chunks to preview (default 8)",
    )
    sub.add_parser("index", help="embed the chunks and build the vector index")
    ask_parser = sub.add_parser("ask", help="ask a question over the indexed documents")
    ask_parser.add_argument("question", help="the question, in quotes")
    ask_parser.add_argument(
        "--show-chunks",
        action="store_true",
        help="also print the retrieved chunks and why each one matched",
    )
    sub.add_parser("cache-clear", help="delete all cached model responses")

    agent_parser = sub.add_parser("agent", help="ask the full agent (plans, uses tools, verifies)")
    agent_parser.add_argument("question", help="the question, in quotes")
    agent_parser.add_argument(
        "--trace", action="store_true", help="print every step the agent took"
    )
    agent_parser.add_argument(
        "--no-actions",
        action="store_true",
        help="read-only run: never propose an action that changes data",
    )
    approve_parser = sub.add_parser("approve", help="approve a paused agent action")
    approve_parser.add_argument("thread_id", help="the thread id the agent printed")
    approve_parser.add_argument("--trace", action="store_true")
    reject_parser = sub.add_parser("reject", help="reject a paused agent action")
    reject_parser.add_argument("thread_id", help="the thread id the agent printed")
    reject_parser.add_argument("--trace", action="store_true")
    sub.add_parser("queue", help="show the Finance Operations review queue")
    sub.add_parser("diagram", help="print the orchestration diagram as Mermaid")
    sub.add_parser("mcp-server", help="run the MCP server over stdin/stdout")
    sub.add_parser("mcp-demo", help="connect to the MCP server and call its tools")

    sub.add_parser("eval-cases", help="list the evaluation dataset (free)")
    eval_parser = sub.add_parser("eval", help="run the evaluation and score the system")
    eval_parser.add_argument("--label", default="", help="a name for this run, e.g. 'baseline'")
    eval_parser.add_argument(
        "--category",
        choices=["policy_lookup", "aggregate", "cross_source", "refusal", "adversarial"],
        help="run only one category",
    )
    eval_parser.add_argument("--limit", type=int, help="run only the first N cases")
    eval_parser.add_argument("--save", action="store_true", help="save the report for comparison")
    sub.add_parser("eval-compare", help="compare the two most recent saved runs")
    sub.add_parser("langfuse-setup", help="show how to run Langfuse locally, and its status")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "generate-data":
        return cmd_generate_data()
    if args.command == "extract":
        return cmd_extract(show_blocks=args.show)
    if args.command == "inspect-db":
        return cmd_inspect_db()
    if args.command == "cost":
        return cmd_cost()
    if args.command == "chunks":
        return cmd_chunks(show=args.show)
    if args.command == "index":
        return cmd_index()
    if args.command == "ask":
        return cmd_ask(args.question, show_chunks=args.show_chunks)
    if args.command == "cache-clear":
        return cmd_cache_clear()
    if args.command == "agent":
        return cmd_agent(args.question, args.trace, allow_actions=not args.no_actions)
    if args.command == "approve":
        return cmd_decide(args.thread_id, "approved", args.trace)
    if args.command == "reject":
        return cmd_decide(args.thread_id, "rejected", args.trace)
    if args.command == "queue":
        return cmd_queue()
    if args.command == "diagram":
        return cmd_diagram()
    if args.command == "mcp-server":
        return cmd_mcp_server()
    if args.command == "mcp-demo":
        return cmd_mcp_demo()
    if args.command == "eval-cases":
        return cmd_eval_cases()
    if args.command == "eval":
        return cmd_eval(args.label, args.category, args.limit, args.save)
    if args.command == "eval-compare":
        return cmd_eval_compare()
    if args.command == "langfuse-setup":
        return cmd_langfuse_setup()

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
