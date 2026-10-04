"""A fake model client, so tests never call the API.

Why a fake and not a mock of the OpenAI SDK
-------------------------------------------
We want to test *our* logic: chunking, retrieval wiring, citation parsing,
refusal handling. None of that needs a real model, and a test suite that needs
one is slow, costs money, and fails when the network does.

The fake embedding is the interesting part. It uses the "hashing trick": every
word is hashed into one of N slots and counted. Two texts sharing words end up
with similar vectors, so retrieval genuinely ranks a matching chunk above a
non-matching one. It is a crude stand-in for meaning, but it is deterministic and
good enough to prove the plumbing works.
"""

from __future__ import annotations

import hashlib
import math
import re

from policydesk.config import Settings
from policydesk.llm.client import ChatResult, EmbedResult
from policydesk.llm.pricing import CostLedger

FAKE_DIMENSIONS = 64


def fake_vector(text: str, dimensions: int = FAKE_DIMENSIONS) -> list[float]:
    """A deterministic vector where shared words produce similar directions."""
    counts = [0.0] * dimensions
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        counts[int.from_bytes(digest[:4], "big") % dimensions] += 1.0

    magnitude = math.sqrt(sum(value * value for value in counts))
    if magnitude == 0:
        counts[0] = 1.0
        magnitude = 1.0
    return [value / magnitude for value in counts]


class FakeLLMClient:
    """Stands in for LLMClient. Records every call so tests can assert on them."""

    def __init__(self, settings: Settings, chat_reply: str = "A fake answer [1]."):
        self.settings = settings
        self.ledger = CostLedger(limit_usd=settings.max_spend_usd_per_run)
        self.chat_reply = chat_reply
        self.chat_calls: list[dict] = []
        self.embed_calls: list[list[str]] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        model: str | None = None,
        max_output_tokens: int = 600,
        temperature: float = 0.0,
    ) -> ChatResult:
        model = model or self.settings.answer_model
        self.chat_calls.append(
            {
                "messages": messages,
                "purpose": purpose,
                "model": model,
                "max_output_tokens": max_output_tokens,
                "temperature": temperature,
            }
        )
        return ChatResult(
            text=self.chat_reply, model=model, input_tokens=100, output_tokens=20, cached=False
        )

    def embed(self, texts: list[str], *, purpose: str, model: str | None = None) -> EmbedResult:
        model = model or self.settings.embedding_model
        self.embed_calls.append(list(texts))
        return EmbedResult(
            vectors=[fake_vector(text) for text in texts],
            model=model,
            input_tokens=sum(len(text) // 4 for text in texts),
            cached_count=0,
        )

    def usage_summary(self) -> str:
        return "fake client, nothing spent"

    @property
    def last_user_prompt(self) -> str:
        """The user-role content of the most recent chat call."""
        messages = self.chat_calls[-1]["messages"]
        return next(m["content"] for m in reversed(messages) if m["role"] == "user")

    @property
    def last_system_prompt(self) -> str:
        messages = self.chat_calls[-1]["messages"]
        return next(m["content"] for m in messages if m["role"] == "system")
