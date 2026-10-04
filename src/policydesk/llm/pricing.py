"""What each model call costs, and a hard stop before you overspend.

Why this file exists
--------------------
"How much will this cost?" should be a command you can run, not a number
somebody guessed. So we keep a price table, count the tokens we actually use,
and refuse to continue past a limit you set.

An important habit, visible in the code below
---------------------------------------------
Prices change. A price written from memory is a wrong number waiting to be
discovered. So the table records **when it was verified and where it came from**,
and `price_age_warning()` tells you when it is getting old.

Treat the table as a local cache of someone else's fact, never as the truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

# --- The verified facts, with their provenance -----------------------------

PRICES_VERIFIED_ON = date(2026, 10, 4)
PRICES_SOURCE_URL = "https://developers.openai.com/api/docs/pricing"
# Re-check after this many days before trusting an estimate for a decision.
PRICE_STALE_AFTER_DAYS = 60


@dataclass(frozen=True)
class ModelPrice:
    """US dollars per one million tokens."""

    input_per_1m: float
    output_per_1m: float

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * self.input_per_1m
            + output_tokens / 1_000_000 * self.output_per_1m
        )


# Chat models. Output tokens cost several times more than input tokens, which is
# why "ask for a shorter answer" is a real cost lever and not a nicety.
PRICES: dict[str, ModelPrice] = {
    "gpt-4o": ModelPrice(input_per_1m=2.50, output_per_1m=10.00),
    "gpt-4o-mini": ModelPrice(input_per_1m=0.15, output_per_1m=0.60),
    "gpt-5-mini": ModelPrice(input_per_1m=0.25, output_per_1m=2.00),
    "gpt-5-nano": ModelPrice(input_per_1m=0.05, output_per_1m=0.40),
    # Embedding models charge for input only; output_per_1m is 0 by definition.
    "text-embedding-3-small": ModelPrice(input_per_1m=0.02, output_per_1m=0.0),
    "text-embedding-3-large": ModelPrice(input_per_1m=0.13, output_per_1m=0.0),
}


def price_age_warning(today: date | None = None) -> str | None:
    """Return a warning if the price table is old enough to be untrustworthy."""
    today = today or date.today()
    age_days = (today - PRICES_VERIFIED_ON).days
    if age_days <= PRICE_STALE_AFTER_DAYS:
        return None
    return (
        f"price table was verified {age_days} days ago "
        f"({PRICES_VERIFIED_ON.isoformat()}). Re-check {PRICES_SOURCE_URL} before "
        "trusting any estimate based on it."
    )


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int = 0) -> float:
    """Cost of one call. Raises on an unknown model rather than guessing zero.

    Returning 0.0 for an unknown model would be the worst possible behaviour:
    spending would look free right up until the invoice arrived.
    """
    if model not in PRICES:
        raise KeyError(
            f"no price recorded for model '{model}'. Add it to PRICES after checking "
            f"{PRICES_SOURCE_URL}. Refusing to estimate, because a silent 0.00 would "
            "hide real spending."
        )
    return PRICES[model].cost_usd(input_tokens, output_tokens)


# --- Counting what we actually spend ---------------------------------------


class BudgetExceeded(RuntimeError):
    """Raised when a run would cost more than the configured limit.

    This is deliberately an exception, not a log line. A warning about spending
    is a warning nobody reads; stopping the run is a limit.
    """


class CallRecord(BaseModel):
    model: str
    purpose: str = Field(description="which step made the call, e.g. 'router' or 'verify'")
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    # A cached call costs nothing but still happened. We keep its token counts so
    # we can show how much the cache saved, which is the only way to know
    # whether it is worth having.
    cached: bool = False


class CostLedger(BaseModel):
    """Records every model call in one run, and enforces a spending cap.

    Honest scope: this only counts calls made THROUGH this ledger. It is a
    seatbelt for our own code, not a guarantee about your account. The real hard
    limit is the monthly usage limit you set in the OpenAI dashboard, because
    that one cannot be bypassed by a bug in our code.
    """

    limit_usd: float = Field(gt=0)
    calls: list[CallRecord] = Field(default_factory=list)

    @property
    def spent_usd(self) -> float:
        return sum(call.cost_usd for call in self.calls)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    @property
    def total_input_tokens(self) -> int:
        return sum(call.input_tokens for call in self.calls)

    @property
    def total_output_tokens(self) -> int:
        return sum(call.output_tokens for call in self.calls)

    def record(
        self,
        model: str,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        cached: bool = False,
    ) -> CallRecord:
        """Record a completed call, then stop the run if we are over the limit.

        Note the order: we charge for the call that already happened, and only
        then refuse to continue. You cannot un-spend money, so the limit protects
        the NEXT call, not this one.
        """
        cost = 0.0 if cached else estimate_cost_usd(model, input_tokens, output_tokens)
        record = CallRecord(
            model=model,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            cached=cached,
        )
        self.calls.append(record)

        if self.spent_usd > self.limit_usd:
            raise BudgetExceeded(
                f"this run has spent ${self.spent_usd:.4f}, over the ${self.limit_usd:.2f} "
                f"limit, after {len(self.calls)} call(s). Raise MAX_SPEND_USD_PER_RUN in .env "
                "if this is expected, or look at the breakdown to find the runaway step."
            )
        return record

    @property
    def saved_usd(self) -> float:
        """What the cache saved: what the cached calls would have cost."""
        return sum(
            estimate_cost_usd(call.model, call.input_tokens, call.output_tokens)
            for call in self.calls
            if call.cached
        )

    @property
    def cached_call_count(self) -> int:
        return sum(1 for call in self.calls if call.cached)

    def breakdown_by_purpose(self) -> dict[str, float]:
        """Where the money went. Usually one step is most of the bill."""
        totals: dict[str, float] = {}
        for call in self.calls:
            totals[call.purpose] = totals.get(call.purpose, 0.0) + call.cost_usd
        return dict(sorted(totals.items(), key=lambda item: item[1], reverse=True))

    def summary_line(self) -> str:
        saved = f", saved ${self.saved_usd:.4f} via cache" if self.cached_call_count else ""
        return (
            f"{len(self.calls)} call(s) ({self.cached_call_count} cached), "
            f"{self.total_input_tokens:,} in / {self.total_output_tokens:,} out, "
            f"spent ${self.spent_usd:.4f} of ${self.limit_usd:.2f}{saved}"
        )
