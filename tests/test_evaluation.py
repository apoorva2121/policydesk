"""Tests for the evaluation harness and the tracer.

A measurement system needs testing more than most code, because a broken metric
does not crash -- it just quietly reports the wrong number, and you act on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from policydesk.config import Settings
from policydesk.evaluation import CASES, EvalReport, case_by_id, compare, dataset_summary
from policydesk.evaluation.metrics import (
    CaseResult,
    forbidden_present,
    mean_reciprocal_rank,
    recall_at_k,
    tool_choice_score,
)
from policydesk.evaluation.runner import CaseRecord, RunConfiguration
from policydesk.observability import Tracer, docker_compose_snippet

# ---------------------------------------------------------------------------
# The dataset
# ---------------------------------------------------------------------------


def test_every_case_has_a_unique_id() -> None:
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids))


def test_every_case_can_actually_be_scored() -> None:
    """A case with no expectations always passes, which is worse than no case."""
    for case in CASES:
        has_expectation = bool(case.must_contain or case.must_not_contain or case.should_refuse)
        assert has_expectation, f"{case.id} asserts nothing"


def test_the_dataset_covers_every_category() -> None:
    summary = dataset_summary()
    for category in ("policy_lookup", "aggregate", "cross_source", "refusal", "adversarial"):
        assert summary.get(category, 0) > 0, category


def test_the_near_miss_cases_forbid_the_wrong_number() -> None:
    """The point of must_not_contain: catching wrong-but-plausible answers.

    Quoting USD 60 for an international trip is not an incomplete answer, it is a
    misleading one, and only a forbidden-value check notices the difference.
    """
    assert "60" in case_by_id("pol-01").must_not_contain
    assert "85" in case_by_id("pol-02").must_not_contain
    assert "289" in case_by_id("cross-01").must_not_contain
    assert "5000" in case_by_id("adv-02").must_not_contain


def test_hand_computed_answers_are_recorded_in_the_notes() -> None:
    """An expected value with no derivation is a guess that looks like ground truth."""
    for case_id in ("agg-01", "cross-01", "cross-02", "cross-03"):
        assert case_by_id(case_id).note, f"{case_id} has no derivation recorded"


# ---------------------------------------------------------------------------
# Retrieval metrics
# ---------------------------------------------------------------------------


def test_recall_counts_the_relevant_passages_found() -> None:
    retrieved = ["policy.pdf (p.1, 2. Meal allowances)", "policy.pdf (p.3, table 1)"]
    assert recall_at_k(retrieved, ("2. Meal allowances",)) == 1.0
    assert recall_at_k(retrieved, ("2. Meal allowances", "7. Receipts")) == 0.5
    assert recall_at_k(retrieved, ("9. Nowhere",)) == 0.0


def test_recall_is_none_when_a_case_declares_no_relevant_passages() -> None:
    """Returning None rather than 0.0 so the case is EXCLUDED from the average.

    Averaging in a meaningless zero is one of the easiest ways to produce a
    confident, wrong number -- an aggregate case has no retrieval expectation, and
    scoring it zero would invent a retrieval problem that does not exist.
    """
    assert recall_at_k(["anything"], ()) is None


def test_mrr_rewards_ranking_the_right_passage_first() -> None:
    relevant = ("2. Meal allowances",)
    assert mean_reciprocal_rank(["policy.pdf (2. Meal allowances)"], relevant) == 1.0
    assert mean_reciprocal_rank(["other", "policy.pdf (2. Meal allowances)"], relevant) == 0.5
    assert mean_reciprocal_rank(["other", "another"], relevant) == 0.0


def test_mrr_and_recall_disagree_in_a_way_that_matters() -> None:
    """Why both are reported.

    Two systems retrieve the same passage; one puts it first, one puts it fourth.
    Identical recall; the model reads the top of the list most carefully.
    """
    relevant = ("2. Meals",)
    first = ["policy (2. Meals)", "a", "b", "c"]
    fourth = ["a", "b", "c", "policy (2. Meals)"]

    assert recall_at_k(first, relevant) == recall_at_k(fourth, relevant) == 1.0
    assert mean_reciprocal_rank(first, relevant) > mean_reciprocal_rank(fourth, relevant)


# ---------------------------------------------------------------------------
# Answer checks
# ---------------------------------------------------------------------------


def test_numbers_written_differently_count_as_the_same_number() -> None:
    """Otherwise a correct answer fails and you debug the test instead of the system."""
    from policydesk.evaluation.metrics import facts_present

    found, total, missing = facts_present("Engineering spent USD 1,464.80.", ("1464.8",))
    assert missing == []
    assert found == total == 1


def test_a_forbidden_value_is_detected_even_when_formatted_differently() -> None:
    assert forbidden_present("the hotel was USD 289.00", ("289",)) == ["289"]
    assert forbidden_present("the limit is USD 85", ("5000",)) == []


def test_tool_choice_ignores_extra_tools() -> None:
    """An unnecessary search is a cost problem, not a correctness one.

    Conflating the two makes both harder to read, so using an extra tool is not
    penalised here.
    """
    assert tool_choice_score({"policy_search", "sql_query"}, ("policy_search",)) == 1.0
    assert tool_choice_score({"policy_search"}, ("policy_search", "sql_query")) == 0.5
    assert tool_choice_score({"policy_search"}, ()) is None


# ---------------------------------------------------------------------------
# Pass/fail
# ---------------------------------------------------------------------------


def _result(case_id: str, **kwargs) -> CaseResult:
    defaults = {"answer": "", "refused": False, "evidence_count": 1, "cited_numbers": [1]}
    defaults.update(kwargs)
    return CaseResult(case=case_by_id(case_id), **defaults)


def test_a_correct_answer_passes() -> None:
    result = _result("pol-01", answer="The international meal limit is USD 85 per day [1].")
    assert result.passed


def test_a_misleading_answer_fails_even_with_the_right_fact_present() -> None:
    """A forbidden value fails the case outright.

    "85 international, 60 domestic" contains the required fact. It also quotes the
    domestic figure in answer to a question about international travel, which is
    how a reader ends up with the wrong number.
    """
    result = _result("pol-01", answer="The limit is USD 85, or USD 60 domestically [1].")
    assert not result.passed
    assert "forbidden" in result.failure_reason()


def test_an_incomplete_answer_fails_with_the_missing_fact_named() -> None:
    result = _result("pol-01", answer="There is a daily cap on meals [1].")
    assert not result.passed
    assert "missing" in result.failure_reason()


def test_refusing_a_question_that_should_be_refused_passes() -> None:
    assert _result("ref-01", refused=True, answer="INSUFFICIENT_EVIDENCE: not indexed").passed


def test_answering_a_question_that_should_be_refused_fails() -> None:
    result = _result("ref-01", refused=False, answer="Parental leave is 26 weeks [1].")
    assert not result.passed
    assert "should have refused" in result.failure_reason()


def test_an_invented_citation_fails_the_case() -> None:
    result = _result(
        "pol-01", answer="The limit is USD 85 [9].", cited_numbers=[9], evidence_count=2
    )
    assert not result.passed
    assert "does not exist" in result.failure_reason()


def test_an_errored_case_fails_rather_than_being_skipped() -> None:
    """A crash must count as a failure. Skipping it flatters the score."""
    assert not _result("pol-01", error="TimeoutError: too slow").passed


# ---------------------------------------------------------------------------
# Reports and comparison
# ---------------------------------------------------------------------------


def _report(label: str, outcomes: dict[str, bool]) -> EvalReport:
    settings = Settings(app_env="test", _env_file=None)
    return EvalReport(
        run_at="2026-10-04T00:00:00+00:00",
        label=label,
        configuration=RunConfiguration.from_settings(settings),
        cases=[
            CaseRecord(id=cid, category="policy_lookup", passed=passed, refused=False)
            for cid, passed in outcomes.items()
        ],
    )


def test_a_report_averages_only_the_cases_where_a_metric_applies() -> None:
    settings = Settings(app_env="test", _env_file=None)
    report = EvalReport(
        run_at="x",
        label="y",
        configuration=RunConfiguration.from_settings(settings),
        cases=[
            CaseRecord(id="a", category="policy_lookup", passed=True, refused=False, recall=1.0),
            CaseRecord(id="b", category="aggregate", passed=True, refused=False, recall=None),
        ],
    )
    assert report.mean("recall") == 1.0  # not 0.5


def test_a_comparison_reports_churn_not_just_the_delta() -> None:
    """The number to look at before believing a small delta.

    Four cases flipping each way nets to zero and means the system is unstable,
    not unchanged. A comparison that only prints the total hides that completely.
    """
    from policydesk.evaluation.runner import RunConfiguration

    baseline = _report("before", {"a": True, "b": True, "c": False, "d": False})
    candidate = _report("after", {"a": False, "b": True, "c": True, "d": False})
    # A DIFFERENT configuration, or the comparison correctly reports the noise
    # floor instead of a churn verdict -- see the test below.
    candidate.configuration = RunConfiguration(
        **{**candidate.configuration.model_dump(), "chunk_strategy": "fixed"}
    )

    result = compare(baseline, candidate)

    assert result.delta == 0.0
    assert result.newly_passing == ["c"]
    assert result.newly_failing == ["a"]
    assert result.churn == 2
    assert "unstable" in result.verdict()


def test_an_identical_run_is_reported_as_identical() -> None:
    same = {"a": True, "b": False}
    result = compare(_report("x", same), _report("y", same))
    assert result.churn == 0
    assert "identical" in result.verdict()


def test_a_configuration_is_recorded_with_the_scores() -> None:
    """A score without its configuration is a number you will misinterpret."""
    settings = Settings(app_env="test", _env_file=None, chunk_strategy="fixed")
    config = RunConfiguration.from_settings(settings)

    assert config.chunk_strategy == "fixed"
    assert "fixed" in config.label()
    assert settings.embedding_model in config.label()


def test_a_saved_report_can_be_loaded_back(tmp_path: Path) -> None:
    from policydesk.evaluation import load_report, save_report

    settings = Settings(app_env="test", _env_file=None, data_dir=tmp_path)
    report = _report("roundtrip", {"a": True})

    path = save_report(report, settings)
    loaded = load_report(path)

    assert loaded.label == "roundtrip"
    assert loaded.pass_rate == 1.0


# ---------------------------------------------------------------------------
# The tracer
# ---------------------------------------------------------------------------


def test_the_tracer_works_with_no_langfuse_configured() -> None:
    """Tracing must never be a dependency of the thing it observes."""
    settings = Settings(app_env="test", _env_file=None)
    tracer = Tracer(settings)

    assert tracer.enabled is False
    with tracer.span("plan", attempt=0):
        pass

    assert len(tracer.spans) == 1
    assert tracer.spans[0].name == "plan"
    assert "local tracing only" in tracer.status_line()


def test_a_span_is_recorded_even_when_the_step_raises() -> None:
    """You most need the timing of the step that failed."""
    settings = Settings(app_env="test", _env_file=None)
    tracer = Tracer(settings)

    with pytest.raises(RuntimeError), tracer.span("gather"):
        raise RuntimeError("tool exploded")

    assert len(tracer.spans) == 1
    assert tracer.spans[0].name == "gather"


def test_span_metadata_is_redacted_before_it_could_leave_the_process() -> None:
    """A span's metadata is the easiest place for document text to escape."""
    settings = Settings(app_env="test", _env_file=None)
    tracer = Tracer(settings)

    with tracer.span("gather", note="contact priya.raman@northwindlabs.example"):
        pass

    assert "priya.raman@northwindlabs.example" not in str(tracer.spans[0].metadata)
    assert "[email redacted]" in str(tracer.spans[0].metadata)


def test_long_metadata_is_truncated() -> None:
    settings = Settings(app_env="test", _env_file=None)
    tracer = Tracer(settings)

    with tracer.span("compose", prompt="x" * 5000):
        pass

    assert len(str(tracer.spans[0].metadata["prompt"])) < 300


def test_the_slowest_step_is_identifiable() -> None:
    """The whole point: turning "it was slow" into "this step was slow"."""
    settings = Settings(app_env="test", _env_file=None)
    tracer = Tracer(settings)

    from policydesk.observability import SpanRecord

    tracer.spans.extend(
        [SpanRecord("plan", 100), SpanRecord("gather", 900), SpanRecord("verify", 10)]
    )

    assert tracer.slowest(1)[0].name == "gather"
    assert tracer.total_ms == 1010
    assert any("gather" in line for line in tracer.summary_lines())


def test_the_compose_snippet_does_not_ship_a_real_secret() -> None:
    """A compose file with a plausible-looking secret gets copied into production."""
    snippet = docker_compose_snippet()
    assert "langfuse" in snippet
    assert "local-dev-only-change-me" in snippet
    assert "openssl rand" in snippet  # tells the reader how to make a real one


def test_the_action_intent_gate_uses_real_word_boundaries() -> None:
    """A regression test for a genuinely invisible bug.

    The pattern was first written with \\b inside a NON-raw Python string, where it
    is the BACKSPACE character rather than a word boundary. It compiled cleanly,
    looked right in the source, and matched nothing at all -- so the gate skipped
    every question, including ones that plainly said "flag it".
    """
    from policydesk.graph.nodes import ACTION_INTENT

    assert chr(8) not in ACTION_INTENT.pattern, "the pattern contains a backspace character"
    assert ACTION_INTENT.search("Flag it for review")
    assert ACTION_INTENT.search("please escalate expense 1101")
    assert not ACTION_INTENT.search("What is the hotel limit in Tokyo?")
    assert not ACTION_INTENT.search("flagship product")  # the word boundary matters


def test_comparing_two_runs_of_the_same_configuration_reports_the_noise_floor() -> None:
    """A comparison tool that congratulates you on noise is worse than no tool.

    This is a real result turned into a test. Two runs of the SAME configuration
    came back at 81.8% and 86.4%, and `verdict()` reported "better by +4.5% with 1
    case changed" -- flattering, and completely meaningless, since nothing had
    been changed.
    """
    baseline = _report("run-1", {"a": True, "b": False, "c": True})
    candidate = _report("run-2", {"a": True, "b": True, "c": True})

    result = compare(baseline, candidate)

    assert result.same_configuration is True
    assert result.delta > 0  # it did "improve"
    verdict = result.verdict()
    assert "IDENTICAL CONFIGURATION" in verdict
    assert "NOISE FLOOR" in verdict
    assert "undemonstrated" in verdict


def test_a_genuinely_different_configuration_is_compared_normally() -> None:
    from policydesk.evaluation.runner import RunConfiguration

    baseline = _report("mini", {"a": True, "b": False})
    candidate = _report("large", {"a": True, "b": True})
    candidate.configuration = RunConfiguration(
        **{**candidate.configuration.model_dump(), "answer_model": "gpt-4o"}
    )

    result = compare(baseline, candidate)

    assert result.same_configuration is False
    assert "IDENTICAL CONFIGURATION" not in result.verdict()


def test_a_single_case_moving_is_reported_as_undemonstrated_even_when_config_changed() -> None:
    """The measured noise floor applies to real changes too.

    The original verdict only invoked the noise floor when the two configurations
    were identical. So a genuine change that moved exactly one case was reported as
    "better by +4.5% with 1 case(s) changed" -- the same flattering sentence the
    identical-config branch was written to prevent, just one step further along.

    This is not hypothetical: adding the retrieval floor moved one case, and the
    tool called it an improvement. An independent sample then came back at the
    baseline rate, which is what the noise floor had been telling us all along.
    """
    baseline = _report("before", {f"c{n}": n > 0 for n in range(22)})
    candidate = _report("after", {f"c{n}": True for n in range(22)})
    # floor=1 is what the runs recorded before the floor existed.
    baseline.configuration = RunConfiguration(
        **{**baseline.configuration.model_dump(), "retrieval_floor": 1}
    )

    result = compare(baseline, candidate)

    assert result.churn == 1
    assert not result.same_configuration, "the floor must be part of the fingerprint"
    assert "UNDEMONSTRATED" in result.verdict()


def test_the_retrieval_floor_is_part_of_the_run_fingerprint() -> None:
    """It lives in code rather than in `.env`, and was therefore left out.

    The consequence was concrete: `compare()` saw two identical fingerprints and
    announced "IDENTICAL CONFIGURATION -- nothing was changed" about a run in which
    the retrieval floor had just been added. A fingerprint must cover everything
    that can change behaviour, not just what happens to be a setting.
    """
    from policydesk.tools.policy_tool import MINIMUM_TOP_K

    settings = Settings(app_env="test", _env_file=None)
    config = RunConfiguration.from_settings(settings)

    assert config.retrieval_floor == MINIMUM_TOP_K
    assert f"floor={MINIMUM_TOP_K}" in config.label()


def test_a_fully_cached_run_is_flagged_as_a_replay() -> None:
    """A zero-cost run cannot confirm anything.

    The comparison tool said "re-run before believing it". The eval was re-run,
    returned an identical score, and that looked like confirmation. It was not: the
    response cache keys on the complete request, so nothing was asked and nothing
    was learned. A replay cannot disagree with the run it replays.
    """
    replay = _report("second-look", {"a": True, "b": False})
    assert replay.total_cost_usd == 0.0
    assert replay.is_replay

    real = _report("real", {"a": True, "b": False})
    real.cases[0].cost_usd = 0.0003
    assert not real.is_replay
