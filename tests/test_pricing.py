"""Tests for cost estimation and the spending cap.

The theme: a cost system must fail loudly. Every test below checks that something
refuses to pretend spending is zero.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from policydesk.llm import BudgetExceeded, CostLedger, estimate_cost_usd
from policydesk.llm.pricing import (
    PRICE_STALE_AFTER_DAYS,
    PRICES,
    PRICES_VERIFIED_ON,
    price_age_warning,
)


def test_cost_is_computed_per_million_tokens() -> None:
    """gpt-4o-mini: $0.15 per 1M input, $0.60 per 1M output (verified 2026-10-04)."""
    cost = estimate_cost_usd("gpt-4o-mini", input_tokens=1_000_000, output_tokens=0)
    assert cost == pytest.approx(0.15)

    cost = estimate_cost_usd("gpt-4o-mini", input_tokens=0, output_tokens=1_000_000)
    assert cost == pytest.approx(0.60)


def test_unknown_model_raises_instead_of_returning_zero() -> None:
    """The important one.

    If an unrecognised model silently cost 0.00, spending would look free right
    up until the invoice arrived. Refusing to estimate is the safe behaviour.
    """
    with pytest.raises(KeyError) as caught:
        estimate_cost_usd("some-model-released-next-year", 1000, 100)

    assert "no price recorded" in str(caught.value)


def test_embedding_models_have_no_output_price() -> None:
    """Embeddings charge for input only; there is no generated text."""
    for name in ("text-embedding-3-small", "text-embedding-3-large"):
        assert PRICES[name].output_per_1m == 0.0


def test_output_tokens_cost_more_than_input_for_chat_models() -> None:
    """Worth encoding: it is why 'ask for a shorter answer' is a real cost lever."""
    for name in ("gpt-4o", "gpt-4o-mini", "gpt-5-mini", "gpt-5-nano"):
        price = PRICES[name]
        assert price.output_per_1m > price.input_per_1m, name


def test_price_table_warns_when_it_goes_stale() -> None:
    """A price written once and trusted forever becomes a wrong number."""
    fresh = PRICES_VERIFIED_ON + timedelta(days=PRICE_STALE_AFTER_DAYS - 1)
    assert price_age_warning(today=fresh) is None

    stale = PRICES_VERIFIED_ON + timedelta(days=PRICE_STALE_AFTER_DAYS + 1)
    warning = price_age_warning(today=stale)
    assert warning is not None
    assert "Re-check" in warning


def test_price_table_is_not_dated_in_the_future() -> None:
    assert date.today() >= PRICES_VERIFIED_ON


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def test_ledger_accumulates_tokens_and_cost() -> None:
    ledger = CostLedger(limit_usd=1.00)
    ledger.record("gpt-4o-mini", purpose="router", input_tokens=400, output_tokens=50)
    ledger.record("gpt-4o-mini", purpose="verify", input_tokens=2200, output_tokens=150)

    assert len(ledger.calls) == 2
    assert ledger.total_input_tokens == 2600
    assert ledger.total_output_tokens == 200
    assert ledger.spent_usd > 0
    assert ledger.remaining_usd == pytest.approx(1.00 - ledger.spent_usd)


def test_ledger_stops_the_run_when_the_limit_is_passed() -> None:
    """A warning about spending is a warning nobody reads. This is a stop."""
    ledger = CostLedger(limit_usd=0.01)

    with pytest.raises(BudgetExceeded) as caught:
        # ~$0.15 of input: well past a one-cent limit.
        ledger.record(
            "gpt-4o-mini", purpose="runaway_loop", input_tokens=1_000_000, output_tokens=0
        )

    message = str(caught.value)
    assert "MAX_SPEND_USD_PER_RUN" in message
    # The call is still recorded. You cannot un-spend money, so the limit
    # protects the NEXT call, not the one that already happened.
    assert len(ledger.calls) == 1


def test_ledger_shows_where_the_money_went() -> None:
    """Usually one step is most of the bill; the breakdown finds it."""
    ledger = CostLedger(limit_usd=10.00)
    ledger.record("gpt-4o-mini", purpose="router", input_tokens=400, output_tokens=50)
    ledger.record("gpt-4o", purpose="compose_answer", input_tokens=2000, output_tokens=400)

    breakdown = ledger.breakdown_by_purpose()
    assert list(breakdown) == ["compose_answer", "router"]  # sorted most expensive first


def test_ledger_rejects_a_zero_or_negative_limit() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CostLedger(limit_usd=0)


# ---------------------------------------------------------------------------
# The project estimate
# ---------------------------------------------------------------------------


def test_every_scenario_uses_models_we_have_prices_for() -> None:
    """Guards against adding a scenario that silently cannot be priced."""
    from policydesk.llm.estimate import unknown_models

    assert unknown_models() == []


def test_cheapest_scenario_really_is_cheapest() -> None:
    from policydesk.llm.estimate import SCENARIOS, project_total

    totals = {s.name: sum(project_total(s).values()) for s in SCENARIOS}
    strong = next(name for name in totals if name.startswith("B."))
    floor = next(name for name in totals if name.startswith("C."))

    assert totals[floor] < totals[strong]
    # The strong-model scenario should be roughly an order of magnitude dearer.
    assert totals[strong] > totals[floor] * 5


def test_estimate_is_dominated_by_the_evaluation_milestone() -> None:
    """If this ever stops being true, the cost model has drifted from reality."""
    from policydesk.llm.estimate import SCENARIOS, per_milestone

    costs = per_milestone(SCENARIOS[0])
    most_expensive = max(costs, key=lambda key: costs[key])

    assert most_expensive.startswith("M7")
