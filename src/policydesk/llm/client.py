"""The only place in this project that talks to a model.

Why funnel everything through one class
---------------------------------------
Every model call needs the same five things done to it: a timeout, retries, a
token count, a cost record, and a cache lookup. If each part of the app calls the
API directly, you end up with five slightly different versions of those five
things, and one of them will be missing a timeout.

So: one door. `LLMClient.chat()` and `LLMClient.embed()`. Everything else in the
project uses those, and gets the safety behaviour for free.

What this class does for you, in order
--------------------------------------
1. Counts the input tokens *before* calling, and refuses if the request is
   absurdly large. A limit you check after paying is not a limit.
2. Looks in the cache. A hit costs nothing and returns instantly.
3. Calls the API with a timeout and a bounded number of retries.
4. Records real token usage and cost in the ledger, which can stop the run.
5. Stores the result in the cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import tiktoken
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from ..config import Settings
from .cache import ResponseCache
from .pricing import CostLedger


class LLMError(RuntimeError):
    """A model call failed in a way the caller must handle.

    We raise a specific error rather than returning an empty string, because an
    empty string flows onward and becomes a confident, blank answer.
    """


class RequestTooLarge(LLMError):
    """The request was rejected before it was sent, to avoid paying for it."""


@dataclass(frozen=True)
class ChatResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cached: bool

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    model: str
    input_tokens: int
    cached_count: int

    @property
    def dimensions(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0


# tiktoken does not know every model name. When it does not, we fall back to a
# general-purpose encoder: the count is then approximate, which is fine for a
# size guard but is why we always re-record the REAL usage the API reports back.
_FALLBACK_ENCODING = "o200k_base"


def count_tokens(text: str, model: str) -> int:
    """Count tokens locally, before spending anything."""
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding(_FALLBACK_ENCODING)
    return len(encoding.encode(text))


class LLMClient:
    """Wraps the OpenAI SDK with cost control, caching and clear errors."""

    def __init__(
        self,
        settings: Settings,
        ledger: CostLedger | None = None,
        cache: ResponseCache | None = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger or CostLedger(limit_usd=settings.max_spend_usd_per_run)
        self.cache = cache or ResponseCache(
            directory=settings.data_dir / "processed" / "llm-cache",
            enabled=settings.llm_cache_enabled,
        )

        if settings.llm_provider != "openai":
            raise LLMError(
                f"LLM_PROVIDER={settings.llm_provider} is not wired up yet. "
                "Only 'openai' is implemented; the other providers are a later exercise."
            )
        if settings.openai_api_key is None:
            raise LLMError("OPENAI_API_KEY is not set. Run 'policydesk doctor' for help.")

        self._client = OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=settings.request_timeout_s,
            # The SDK retries connection errors and rate limits for us, with
            # exponential backoff. Bounded, so a broken API cannot hang forever.
            max_retries=2,
        )

    # -- chat ---------------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        model: str | None = None,
        max_output_tokens: int = 600,
        temperature: float = 0.0,
    ) -> ChatResult:
        """Send a conversation, get text back.

        `purpose` is required, not optional. It is how the cost breakdown can
        tell you which step of the pipeline is spending your money.
        """
        model = model or self.settings.chat_model

        joined = "\n".join(message.get("content", "") for message in messages)
        input_tokens = count_tokens(joined, model)
        budget = self.settings.max_prompt_tokens
        if input_tokens > budget:
            raise RequestTooLarge(
                f"the prompt for '{purpose}' is about {input_tokens:,} tokens, over the "
                f"{budget:,} token limit. Refusing to send it. Retrieve fewer chunks, or "
                "raise MAX_PROMPT_TOKENS if this is genuinely expected."
            )

        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_output_tokens,
            "temperature": temperature,
        }
        key = ResponseCache.key_for({"kind": "chat", **request})

        hit = self.cache.get(key)
        if hit is not None:
            self.ledger.record(
                model, purpose, hit["input_tokens"], hit["output_tokens"], cached=True
            )
            return ChatResult(
                text=hit["text"],
                model=model,
                input_tokens=hit["input_tokens"],
                output_tokens=hit["output_tokens"],
                cached=True,
            )

        try:
            response = self._client.chat.completions.create(**request)
        except (APITimeoutError, APIConnectionError) as exc:
            raise LLMError(
                f"could not reach the model for '{purpose}' within "
                f"{self.settings.request_timeout_s:.0f}s: {exc}"
            ) from exc
        except RateLimitError as exc:
            raise LLMError(
                f"rate limited while doing '{purpose}' (after retries). Wait a moment, "
                "or slow the pipeline down."
            ) from exc
        except APIStatusError as exc:
            # Never include the response body blindly: it can echo the prompt.
            raise LLMError(
                f"the API rejected the '{purpose}' call with status {exc.status_code}."
            ) from exc

        text = response.choices[0].message.content or ""
        usage = response.usage
        # Always prefer the usage the API reports over our local estimate.
        actual_in = usage.prompt_tokens if usage else input_tokens
        actual_out = usage.completion_tokens if usage else count_tokens(text, model)

        self.cache.put(key, {"text": text, "input_tokens": actual_in, "output_tokens": actual_out})
        self.ledger.record(model, purpose, actual_in, actual_out)

        return ChatResult(
            text=text,
            model=model,
            input_tokens=actual_in,
            output_tokens=actual_out,
            cached=False,
        )

    # -- embeddings ---------------------------------------------------------

    def embed(self, texts: list[str], *, purpose: str, model: str | None = None) -> EmbedResult:
        """Turn text into vectors, caching each piece of text separately.

        Per-text caching is the important detail. If you add one new document and
        rebuild the index, only that one document is embedded; the other hundred
        come from the cache. Caching the whole batch together would re-embed
        everything whenever anything changed.
        """
        model = model or self.settings.embedding_model
        if not texts:
            return EmbedResult(vectors=[], model=model, input_tokens=0, cached_count=0)

        vectors: list[list[float] | None] = [None] * len(texts)
        keys = [ResponseCache.key_for({"kind": "embed", "model": model, "text": t}) for t in texts]

        missing_positions: list[int] = []
        cached_count = 0
        for position, key in enumerate(keys):
            hit = self.cache.get(key)
            if hit is None:
                missing_positions.append(position)
            else:
                vectors[position] = hit["vector"]
                cached_count += 1

        input_tokens = 0
        if missing_positions:
            batch = [texts[position] for position in missing_positions]
            try:
                response = self._client.embeddings.create(model=model, input=batch)
            except (APITimeoutError, APIConnectionError) as exc:
                raise LLMError(f"could not reach the embedding API for '{purpose}': {exc}") from exc
            except RateLimitError as exc:
                raise LLMError(f"rate limited while embedding for '{purpose}'.") from exc
            except APIStatusError as exc:
                raise LLMError(
                    f"the embedding API rejected '{purpose}' with status {exc.status_code}."
                ) from exc

            for item, position in zip(response.data, missing_positions, strict=True):
                vectors[position] = item.embedding
                self.cache.put(keys[position], {"vector": item.embedding})

            input_tokens = response.usage.prompt_tokens if response.usage else 0
            self.ledger.record(model, purpose, input_tokens, 0)

        if cached_count:
            # Record the cached portion so the saving is visible in the summary.
            # Embedding prices are per input token, so we estimate those tokens.
            estimated = sum(
                count_tokens(texts[position], model)
                for position in range(len(texts))
                if position not in set(missing_positions)
            )
            self.ledger.record(model, f"{purpose} (cached)", estimated, 0, cached=True)

        assert all(vector is not None for vector in vectors)
        return EmbedResult(
            vectors=[vector for vector in vectors if vector is not None],
            model=model,
            input_tokens=input_tokens,
            cached_count=cached_count,
        )

    # -- reporting ----------------------------------------------------------

    def usage_summary(self) -> str:
        return f"{self.ledger.summary_line()} | {self.cache.summary_line()}"
