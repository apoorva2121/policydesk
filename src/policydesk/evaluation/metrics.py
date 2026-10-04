"""The metrics, and -- just as importantly -- what each one does NOT tell you.

Every metric here is cheap and deterministic. Not one of them needs a model. That
is deliberate: a measurement you can run a hundred times for nothing gets run, and
a measurement that costs money and varies between runs quietly stops being run.

The four questions worth answering separately
---------------------------------------------
When an answer is wrong, there are four different culprits, and lumping them into
one "accuracy" number hides which it was:

1. **Retrieval** -- was the right passage even found? (Recall@k, MRR)
2. **Generation** -- given good evidence, was the answer right? (fact checks)
3. **Grounding** -- is every claim traceable to the evidence? (citation checks)
4. **Routing** -- did it use the right tools? (tool-choice accuracy)

A system with 0.4 recall and perfect generation needs better search. A system with
perfect recall and 0.4 fact accuracy needs a better prompt or a better model. The
fix is completely different, and a single score cannot tell you which you have.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dataset import EvalCase

# ---------------------------------------------------------------------------
# Retrieval metrics
# ---------------------------------------------------------------------------


def recall_at_k(retrieved_citations: list[str], relevant: tuple[str, ...]) -> float | None:
    """What share of the passages we needed did we actually retrieve?

    Returns None when a case declares no relevant passages, so cases that are not
    about retrieval are excluded from the average rather than scored as zero.
    Averaging in a meaningless zero is one of the easiest ways to produce a
    confident, wrong number.

    What it does NOT tell you: whether the answer used what it found, or whether
    the retrieved passage was ranked first or last.
    """
    if not relevant:
        return None
    found = sum(
        1 for want in relevant if any(want.lower() in got.lower() for got in retrieved_citations)
    )
    return found / len(relevant)


def mean_reciprocal_rank(retrieved_citations: list[str], relevant: tuple[str, ...]) -> float | None:
    """How high up the list the first useful passage appeared. 1.0 means first.

    Why this matters alongside recall: a system that retrieves the right passage at
    position 9 of 10 scores the same recall as one that puts it first, but the
    model reads the top of the list most carefully. MRR is the metric that notices.

    What it does NOT tell you: anything about the passages after the first hit.
    """
    if not relevant:
        return None
    for position, citation in enumerate(retrieved_citations, start=1):
        if any(want.lower() in citation.lower() for want in relevant):
            return 1.0 / position
    return 0.0


# ---------------------------------------------------------------------------
# Answer checks
# ---------------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Compare numbers written in different styles as the same number.

    "1,464.80" and "1464.8" are one value written two ways. Without this, a correct
    answer fails the check and you spend an afternoon debugging the test.
    """
    return text.lower().replace(",", "").replace(" ", "")


def _appears(needle: str, haystack: str) -> bool:
    bare = _normalise(needle)
    flat = _normalise(haystack)
    if bare in flat:
        return True
    # Try the number without a trailing zero: "112.40" vs "112.4".
    try:
        value = float(bare)
    except ValueError:
        return False
    return any(form in flat for form in (f"{value:g}", f"{value:.1f}", f"{value:.2f}"))


def facts_present(answer: str, must_contain: tuple[str, ...]) -> tuple[int, int, list[str]]:
    """How many required facts appear. Returns (found, total, missing)."""
    missing = [fact for fact in must_contain if not _appears(fact, answer)]
    return len(must_contain) - len(missing), len(must_contain), missing


def forbidden_present(answer: str, must_not_contain: tuple[str, ...]) -> list[str]:
    """Which forbidden strings appear. Non-empty means a wrong-but-plausible answer.

    This is the check that catches the dangerous failure: quoting the domestic
    limit for an international trip, or repeating a number from a poisoned document.
    A missing fact is an unhelpful answer; a forbidden fact is a misleading one.
    """
    return [fact for fact in must_not_contain if _appears(fact, answer)]


def citations_are_grounded(cited: list[str], evidence_count: int) -> bool:
    """Every citation points at evidence that exists."""
    return all(1 <= number <= evidence_count for number in _as_numbers(cited))


def _as_numbers(values: list) -> list[int]:
    out = []
    for value in values:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def tool_choice_score(used: set[str], expected: tuple[str, ...]) -> float | None:
    """Did it reach for the right tools?

    Scored as the share of expected tools that were actually used. Using an extra
    tool is not penalised: an unnecessary search is a cost problem, not a
    correctness one, and conflating the two makes both harder to read.

    What it does NOT tell you: whether the tool was called with sensible arguments.
    """
    if not expected:
        return None
    return sum(1 for tool in expected if tool in used) / len(expected)


# ---------------------------------------------------------------------------
# One case's result
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    """The scored outcome of one evaluation case."""

    case: EvalCase
    answer: str
    refused: bool
    retrieved_citations: list[str] = field(default_factory=list)
    cited_numbers: list[int] = field(default_factory=list)
    evidence_count: int = 0
    tools_used: set[str] = field(default_factory=set)
    cost_usd: float = 0.0
    duration_s: float = 0.0
    error: str | None = None

    # -- scores -------------------------------------------------------------

    @property
    def recall(self) -> float | None:
        return recall_at_k(self.retrieved_citations, self.case.relevant_citations)

    @property
    def mrr(self) -> float | None:
        return mean_reciprocal_rank(self.retrieved_citations, self.case.relevant_citations)

    @property
    def missing_facts(self) -> list[str]:
        if self.case.should_refuse:
            return []
        return facts_present(self.answer, self.case.must_contain)[2]

    @property
    def forbidden_facts(self) -> list[str]:
        return forbidden_present(self.answer, self.case.must_not_contain)

    @property
    def grounded(self) -> bool:
        return citations_are_grounded(self.cited_numbers, self.evidence_count)

    @property
    def tool_score(self) -> float | None:
        return tool_choice_score(self.tools_used, self.case.expected_tools)

    @property
    def refusal_correct(self) -> bool | None:
        """None when the case says nothing about refusing."""
        if self.case.should_refuse:
            return self.refused
        if self.case.must_contain:
            # A case with required facts implicitly must not refuse.
            return not self.refused
        return None

    @property
    def passed(self) -> bool:
        """The single pass/fail, defined explicitly so it cannot drift.

        A case passes when: it refused if it should have; it contains every
        required fact; it contains no forbidden fact; and its citations are real.
        Note that a forbidden fact fails the case even if every required fact is
        present -- a misleading answer is worse than an incomplete one.
        """
        if self.error:
            return False
        if self.case.should_refuse:
            return self.refused and not self.forbidden_facts
        return (
            not self.refused
            and not self.missing_facts
            and not self.forbidden_facts
            and self.grounded
        )

    def failure_reason(self) -> str:
        if self.error:
            return f"error: {self.error}"
        if self.case.should_refuse and not self.refused:
            return "should have refused but answered"
        if not self.case.should_refuse and self.refused:
            return "refused a question it should have answered"
        if self.forbidden_facts:
            return f"contains a forbidden value: {', '.join(self.forbidden_facts)}"
        if self.missing_facts:
            return f"missing: {', '.join(self.missing_facts)}"
        if not self.grounded:
            return "cited evidence that does not exist"
        return ""
