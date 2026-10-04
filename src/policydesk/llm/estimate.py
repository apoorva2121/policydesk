"""A budget for the whole project, built from stated assumptions.

This is a model, not a promise. Its value is that every assumption is written
down and changeable, so when the real bill differs you can find out which
assumption was wrong instead of shrugging.

The honest health warning: token counts per step are engineering estimates, not
measurements, because the steps are not built yet. From Milestone 2 onward the
`CostLedger` records what we really spend, and you should trust that over this.
"""

from __future__ import annotations

from dataclasses import dataclass

from .pricing import PRICES, estimate_cost_usd

# Measured from our actual corpus on 2026-10-04 (policydesk extract):
# 13 blocks, 6,807 characters. English prose is roughly 4 characters per token.
CORPUS_TOKENS = 1_700
# A question is ~20 tokens; we re-embed it on every retrieval attempt.
QUESTION_TOKENS = 25
# How many times we expect to rebuild the index while tuning chunking/embeddings.
INDEX_REBUILDS = 40


@dataclass(frozen=True)
class Step:
    """One model call in the pipeline, sized.

    `calls_per_question` can be fractional: 0.5 means half of all questions
    trigger this step, 1.3 means it sometimes runs twice because of the retry
    loop. That fraction is where most cost surprises actually live.
    """

    name: str
    role: str  # "cheap" | "strong"
    input_tokens: int
    output_tokens: int
    calls_per_question: float


# The finished agentic pipeline, as designed in docs/PLAN.md.
PIPELINE: tuple[Step, ...] = (
    Step("router", "cheap", 400, 50, 1.0),
    Step("sql_generation", "cheap", 800, 120, 0.5),
    Step("grade_evidence", "cheap", 1_800, 100, 1.3),
    Step("compose_answer", "strong", 2_000, 400, 1.0),
    Step("verify", "strong", 2_200, 150, 1.0),
)

# Expected number of full question-runs per milestone, including debugging.
# Milestone 7 dominates: an evaluation set of 30 questions, re-run about 25 times
# as we compare chunking, embeddings, prompts and models.
QUESTION_VOLUME: dict[str, int] = {
    "M2 plain RAG baseline": 150,
    "M3 text-to-SQL tools": 150,
    "M4 LangGraph orchestration": 200,
    "M5 guardrails and red-team": 100,
    "M6 MCP": 50,
    "M7 evaluation (30 questions x 25 runs)": 750,
    "M8 Langfuse tracing": 50,
    "M9 serving and CI": 50,
}
# Every evaluated question also gets a judge call that reads the answer and its
# evidence. Only Milestone 7's runs are judged.
JUDGED_QUESTIONS = QUESTION_VOLUME["M7 evaluation (30 questions x 25 runs)"]
JUDGE_INPUT_TOKENS = 2_500
JUDGE_OUTPUT_TOKENS = 200


@dataclass(frozen=True)
class Scenario:
    name: str
    cheap_model: str
    strong_model: str
    judge_model: str
    embedding_model: str
    note: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "A. Cheapest sensible (recommended)",
        cheap_model="gpt-4o-mini",
        strong_model="gpt-4o-mini",
        judge_model="gpt-5-mini",
        embedding_model="text-embedding-3-small",
        note="Mini for everything. The judge is a different model family on purpose, "
        "so it does not share the generator's blind spots.",
    ),
    Scenario(
        "B. Strong model for answers",
        cheap_model="gpt-4o-mini",
        strong_model="gpt-4o",
        judge_model="gpt-4o",
        embedding_model="text-embedding-3-small",
        note="gpt-4o writes and checks every answer. Better quality, roughly ten "
        "times the cost. Worth switching on only to measure the difference.",
    ),
    Scenario(
        "C. Absolute floor",
        cheap_model="gpt-5-nano",
        strong_model="gpt-4o-mini",
        judge_model="gpt-5-mini",
        embedding_model="text-embedding-3-small",
        note="Nano for routing and grading. Expect more routing mistakes, which is "
        "itself a useful thing to observe in the evaluation.",
    ),
)


def cost_per_question(scenario: Scenario) -> dict[str, float]:
    """Cost of one average question, broken down by step."""
    models = {"cheap": scenario.cheap_model, "strong": scenario.strong_model}
    breakdown: dict[str, float] = {}
    for step in PIPELINE:
        per_call = estimate_cost_usd(models[step.role], step.input_tokens, step.output_tokens)
        breakdown[step.name] = per_call * step.calls_per_question

    # Retrieval re-embeds the question on each attempt (1.3 attempts on average).
    breakdown["query_embedding"] = (
        estimate_cost_usd(scenario.embedding_model, QUESTION_TOKENS) * 1.3
    )
    return breakdown


def project_total(scenario: Scenario) -> dict[str, float]:
    """Total project cost, broken down by where it goes."""
    per_question = sum(cost_per_question(scenario).values())
    questions = sum(QUESTION_VOLUME.values())

    judge_total = JUDGED_QUESTIONS * estimate_cost_usd(
        scenario.judge_model, JUDGE_INPUT_TOKENS, JUDGE_OUTPUT_TOKENS
    )
    index_total = INDEX_REBUILDS * estimate_cost_usd(scenario.embedding_model, CORPUS_TOKENS)

    return {
        "pipeline calls": per_question * questions,
        "evaluation judge": judge_total,
        "index rebuilds": index_total,
    }


def per_milestone(scenario: Scenario) -> dict[str, float]:
    """Cost per milestone, so you can see which one to watch."""
    per_question = sum(cost_per_question(scenario).values())
    judge_per_question = estimate_cost_usd(
        scenario.judge_model, JUDGE_INPUT_TOKENS, JUDGE_OUTPUT_TOKENS
    )

    out: dict[str, float] = {}
    for milestone, count in QUESTION_VOLUME.items():
        cost = per_question * count
        if milestone.startswith("M7"):
            cost += judge_per_question * count
        out[milestone] = cost
    return out


def unknown_models() -> list[str]:
    """Any model named in a scenario that has no recorded price."""
    named = {
        model
        for scenario in SCENARIOS
        for model in (
            scenario.cheap_model,
            scenario.strong_model,
            scenario.judge_model,
            scenario.embedding_model,
        )
    }
    return sorted(model for model in named if model not in PRICES)
