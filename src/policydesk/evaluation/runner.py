"""Running the evaluation, and comparing one run against another.

The two things this must get right
----------------------------------
**1. Record the configuration with the scores.** A score of 0.72 means nothing on
its own. 0.72 with `chunk_strategy=section, answer_model=gpt-4o-mini, top_k=6`
means something, and can be compared with tomorrow's. A results file without its
configuration is a number you will misinterpret within a week.

**2. Make a comparison honest about noise.** Two runs of the same configuration do
not give identical scores -- a retrieval tie breaks differently, a model varies even
at temperature zero. So `compare` reports how many cases CHANGED, not just the
difference in totals. Two runs where 4 cases flipped each way show "no change" in
the total and plenty of movement underneath, and that is exactly when a "+2% win"
is really noise.

Cost
----
Running the full set is roughly 22 questions through the whole agent. With the
response cache warm a re-run is nearly free, which is the point: you can run it
after every change.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config import Settings
from ..tools.policy_tool import MINIMUM_TOP_K
from .dataset import CASES, Category, EvalCase
from .metrics import CaseResult

# One case moving is this set's MEASURED noise floor, not a guess: two runs of an
# identical configuration came back 18/22 and 19/22, recorded in
# eval-runs/20261004T081622+0000-final.json and ...081943+0000-noise-check.json.
#
# So a change that moves a single case has demonstrated nothing, however flattering
# the percentage looks -- and that applies just as much when the configuration DID
# change, which is the case the verdict below originally waved through. Re-measure
# this if the dataset grows.
MEASURED_NOISE_FLOOR_CASES = 1


class RunConfiguration(BaseModel):
    """Everything that could change a score. Saved beside the scores."""

    chunk_strategy: str
    chunk_size_chars: int
    chunk_overlap_chars: int
    embedding_model: str
    chat_model: str
    answer_model: str
    retrieval_top_k: int
    max_graph_loops: int

    # The floor the agent's own top_k is clamped to. It lives in code, not in
    # settings, and leaving it out of this fingerprint caused a real misreading:
    # adding the floor moved two cases, and `compare()` reported "IDENTICAL
    # CONFIGURATION -- nothing was changed", filing a genuine improvement as
    # noise. A fingerprint has to cover everything that can change behaviour,
    # not merely everything that happens to live in `.env`.
    #
    # The default is 1 because that is what the runs recorded before the floor
    # existed, so those files still load and still compare honestly.
    retrieval_floor: int = 1

    @classmethod
    def from_settings(cls, settings: Settings) -> RunConfiguration:
        return cls(
            chunk_strategy=settings.chunk_strategy,
            chunk_size_chars=settings.chunk_size_chars,
            chunk_overlap_chars=settings.chunk_overlap_chars,
            embedding_model=settings.embedding_model,
            chat_model=settings.chat_model,
            answer_model=settings.answer_model,
            retrieval_top_k=settings.retrieval_top_k,
            max_graph_loops=settings.max_graph_loops,
            retrieval_floor=MINIMUM_TOP_K,
        )

    def label(self) -> str:
        return (
            f"{self.chunk_strategy}/{self.chunk_size_chars} | {self.embedding_model} | "
            f"{self.answer_model} | k={self.retrieval_top_k} | floor={self.retrieval_floor}"
        )


class CaseRecord(BaseModel):
    """One case's outcome, in a form that can be written to disk and compared."""

    id: str
    category: str
    passed: bool
    refused: bool
    reason: str = ""
    recall: float | None = None
    mrr: float | None = None
    tool_score: float | None = None
    grounded: bool = True
    cost_usd: float = 0.0
    duration_s: float = 0.0
    answer_preview: str = ""


class EvalReport(BaseModel):
    """A complete run: when, how it was configured, and every case's outcome."""

    run_at: str
    label: str
    configuration: RunConfiguration
    cases: list[CaseRecord] = Field(default_factory=list)

    # -- aggregates ---------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for case in self.cases if case.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def total_cost_usd(self) -> float:
        return sum(case.cost_usd for case in self.cases)

    @property
    def is_replay(self) -> bool:
        """True when this run cost nothing, i.e. every response came from cache.

        A trap this project walked into. The comparison tool said "re-run before
        believing it", the eval was re-run, and it returned exactly the same score
        -- which looked like confirmation and was nothing of the kind. The response
        cache keys on the complete request, so an unchanged configuration replays
        byte-identical answers. A replay can never disagree with the run it is
        replaying, so it cannot confirm anything about noise.

        To take a genuine second sample, run with `LLM_CACHE_ENABLED=false`.
        """
        return self.total > 0 and self.total_cost_usd == 0.0

    def mean(self, field_name: str) -> float | None:
        """Average a metric, skipping cases where it does not apply.

        Skipping rather than treating None as zero. A policy-lookup case has no
        tool expectation; scoring it zero would drag the tool average down and
        invent a problem that does not exist.
        """
        values = [
            value
            for value in (getattr(case, field_name) for case in self.cases)
            if value is not None
        ]
        return sum(values) / len(values) if values else None

    def by_category(self) -> dict[str, tuple[int, int]]:
        """Passed and total per category -- where the weakness actually is."""
        out: dict[str, list[int]] = {}
        for case in self.cases:
            bucket = out.setdefault(case.category, [0, 0])
            bucket[0] += int(case.passed)
            bucket[1] += 1
        return {name: (counts[0], counts[1]) for name, counts in sorted(out.items())}

    def failures(self) -> list[CaseRecord]:
        return [case for case in self.cases if not case.passed]


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def evaluate_case(case: EvalCase, settings: Settings, allow_actions: bool = False) -> CaseResult:
    """Run one case through the full agent and score it.

    `allow_actions=False`: an evaluation must never write anything. Scoring runs
    happen dozens of times, and a run that files review requests each time would
    bury a real reviewer in noise.
    """
    from ..pipeline import make_client, run_agent

    started = time.monotonic()
    llm = make_client(settings)

    try:
        run = run_agent(case.question, settings, llm, allow_actions=allow_actions)
    except Exception as exc:  # noqa: BLE001 - one bad case must not end the run
        return CaseResult(
            case=case,
            answer="",
            refused=False,
            error=f"{type(exc).__name__}: {exc}",
            duration_s=time.monotonic() - started,
        )

    state = run.state
    evidence = state.get("evidence") or []
    # Which tools were actually used, read from the plan the agent executed.
    tools_used = {step.tool for step in (state.get("plan") or [])}

    return CaseResult(
        case=case,
        answer=state.get("answer") or "",
        refused=bool(state.get("refused")),
        retrieved_citations=[item.citation for item in evidence],
        cited_numbers=list(state.get("cited_numbers") or []),
        evidence_count=len(evidence),
        tools_used=tools_used,
        cost_usd=llm.ledger.spent_usd,
        duration_s=time.monotonic() - started,
    )


def run_evaluation(
    settings: Settings,
    *,
    label: str = "",
    category: Category | None = None,
    limit: int | None = None,
    on_case: Any = None,
) -> EvalReport:
    """Run the whole set (or one category) and return a scored report."""
    cases = [case for case in CASES if category is None or case.category == category]
    if limit is not None:
        cases = cases[:limit]

    report = EvalReport(
        run_at=datetime.now(UTC).isoformat(timespec="seconds"),
        label=label or "unlabelled",
        configuration=RunConfiguration.from_settings(settings),
    )

    for case in cases:
        result = evaluate_case(case, settings)
        record = CaseRecord(
            id=case.id,
            category=case.category,
            passed=result.passed,
            refused=result.refused,
            reason=result.failure_reason(),
            recall=result.recall,
            mrr=result.mrr,
            tool_score=result.tool_score,
            grounded=result.grounded,
            cost_usd=result.cost_usd,
            duration_s=result.duration_s,
            answer_preview=" ".join(result.answer.split())[:160],
        )
        report.cases.append(record)
        if on_case is not None:
            on_case(record)

    return report


# ---------------------------------------------------------------------------
# Saving and comparing
# ---------------------------------------------------------------------------


def results_dir(settings: Settings) -> Path:
    """Where saved evaluation runs live.

    NOT under `data/processed/`, and that is a correction, not a preference.
    They were there originally, and a clean rebuild -- `rm -rf data/processed`,
    which is a documented, encouraged thing to do -- deleted the entire run
    history. Three measurements, gone, with nothing to compare against.

    Evaluation results are a RECORD, not a cache. The distinction is whether you
    can regenerate them: an index you can rebuild in seconds, a score from a
    configuration you have since changed you cannot. Records belong somewhere
    deliberate, and this directory is committed to git for exactly that reason.
    """
    return settings.data_dir.parent / "eval-runs"


def save_report(report: EvalReport, settings: Settings) -> Path:
    directory = results_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(c if c.isalnum() or c in "-_" else "-" for c in report.label)[:40]
    stamp = report.run_at.replace(":", "").replace("-", "")
    path = directory / f"{stamp}-{safe_label}.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_report(path: Path) -> EvalReport:
    return EvalReport.model_validate_json(path.read_text(encoding="utf-8"))


def latest_reports(settings: Settings, count: int = 2) -> list[EvalReport]:
    directory = results_dir(settings)
    if not directory.exists():
        return []
    paths = sorted(directory.glob("*.json"), reverse=True)[:count]
    return [load_report(path) for path in paths]


class Comparison(BaseModel):
    """A before-and-after, honest about how much moved underneath the totals."""

    baseline_label: str
    candidate_label: str
    baseline_pass_rate: float
    candidate_pass_rate: float
    newly_passing: list[str] = Field(default_factory=list)
    newly_failing: list[str] = Field(default_factory=list)
    unchanged: int = 0
    # True when both runs used the SAME configuration. Then the delta is not a
    # result at all -- it is a direct measurement of the noise floor.
    same_configuration: bool = False

    @property
    def delta(self) -> float:
        return self.candidate_pass_rate - self.baseline_pass_rate

    @property
    def churn(self) -> int:
        """How many cases changed verdict at all.

        The number to look at before believing a small delta. Four cases flipping
        each way nets to zero and means the system is unstable, not unchanged.
        """
        return len(self.newly_passing) + len(self.newly_failing)

    def verdict(self) -> str:
        """A plain-language reading, deliberately cautious about small deltas.

        The first branch below exists because of a real result. Two runs of the
        SAME configuration came back at 81.8% and 86.4%, and this function
        cheerfully reported "better by +4.5% with 1 case changed" -- a flattering,
        completely meaningless statement, since nothing had been changed.

        A comparison tool that congratulates you on noise is worse than no tool,
        because it manufactures confidence. So when the configurations match, the
        delta is reported as what it actually is: the noise floor.
        """
        if self.same_configuration:
            if self.churn == 0:
                return (
                    "identical configuration AND identical results. This set is "
                    "stable on these cases"
                )
            return (
                f"IDENTICAL CONFIGURATION -- nothing was changed. The {self.delta:+.1%} "
                f"difference and the {self.churn} case(s) that moved are the NOISE "
                f"FLOOR of this set, not a result. Treat any future change smaller "
                f"than {abs(self.delta):.1%} as undemonstrated"
            )

        if self.churn == 0:
            return "identical: no case changed verdict"
        if abs(self.delta) < 1e-9:
            return (
                f"no net change, but {self.churn} case(s) flipped. The system is "
                "unstable here rather than unchanged -- do not read this as 'no effect'"
            )
        direction = "better" if self.delta > 0 else "worse"
        if self.churn > abs(len(self.newly_passing) - len(self.newly_failing)) * 2:
            return (
                f"{direction} by {self.delta:+.1%}, but {self.churn} case(s) moved in "
                "total. With a set this small that is within noise: confirm by "
                "re-running before acting on it"
            )
        if self.churn <= MEASURED_NOISE_FLOOR_CASES:
            return (
                f"{direction} by {self.delta:+.1%}, but only {self.churn} case(s) "
                f"moved and {MEASURED_NOISE_FLOOR_CASES} case moving is this set's "
                f"MEASURED noise floor. UNDEMONSTRATED -- re-run before believing it"
            )
        return f"{direction} by {self.delta:+.1%} with {self.churn} case(s) changed"


def compare(baseline: EvalReport, candidate: EvalReport) -> Comparison:
    """Compare two runs case by case, not just total against total."""
    before = {case.id: case.passed for case in baseline.cases}
    after = {case.id: case.passed for case in candidate.cases}
    shared = sorted(set(before) & set(after))

    return Comparison(
        baseline_label=f"{baseline.label} ({baseline.configuration.label()})",
        candidate_label=f"{candidate.label} ({candidate.configuration.label()})",
        same_configuration=baseline.configuration == candidate.configuration,
        baseline_pass_rate=baseline.pass_rate,
        candidate_pass_rate=candidate.pass_rate,
        newly_passing=[cid for cid in shared if after[cid] and not before[cid]],
        newly_failing=[cid for cid in shared if before[cid] and not after[cid]],
        unchanged=sum(1 for cid in shared if before[cid] == after[cid]),
    )
