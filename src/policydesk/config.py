"""All settings for the app, in one place.

Why this file exists
--------------------
Settings come from environment variables (usually loaded from a local `.env`
file). We read them ONCE, here, and check them immediately.

The rule: if a setting is wrong or missing, the app should refuse to start and
say exactly what is wrong. The alternative is much worse -- the app starts,
runs for ten minutes, and then crashes deep inside some other code with a
confusing error.

That idea is called "fail fast".
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py lives at <project>/src/policydesk/config.py
# parents[0] = policydesk, parents[1] = src, parents[2] = project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Every knob in the system. Each attribute maps to one env var.

    `app_env=dev` is the default on purpose: the safe, local, chatty mode.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,  # OPENAI_API_KEY and openai_api_key both work
        extra="ignore",  # unknown vars in .env are ignored, not fatal
    )

    # ---------- Runtime ----------
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False

    # ---------- Model provider ----------
    llm_provider: Literal["openai", "azure_openai", "gemini", "ollama"] = "openai"

    # SecretStr is a string that refuses to print itself. If this value ever
    # ends up in a log line or an error message, you see "**********".
    openai_api_key: SecretStr | None = None

    # Cheap models everywhere by default. Change one line to test a stronger one
    # -- and then measure whether it actually answers better. Do not assume.
    chat_model: str = "gpt-4o-mini"  # routing, grading, writing SQL
    answer_model: str = "gpt-4o-mini"  # composing the final answer
    # The judge is a different model family on purpose, so that it does not share
    # the generator's blind spots. Used only by the evaluation milestone.
    judge_model: str = "gpt-5-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_backend: Literal["provider", "local"] = "provider"

    # Cache identical model calls on disk. The biggest saving while learning.
    llm_cache_enabled: bool = True

    # ---------- Safety and cost limits ----------
    # These are enforced in later milestones, but they are declared now so that
    # there is exactly one place to change them.
    max_input_chars: int = Field(default=4000, ge=100, le=100_000)
    # Checked locally BEFORE a call is sent, so a runaway prompt costs nothing.
    max_prompt_tokens: int = Field(default=12_000, ge=500, le=200_000)
    retrieval_top_k: int = Field(default=5, ge=1, le=50)
    max_graph_loops: int = Field(default=2, ge=0, le=5)
    request_timeout_s: float = Field(default=30.0, gt=0, le=300)
    sql_row_limit: int = Field(default=200, ge=1, le=10_000)

    # --- Chunking (Milestone 2) ---
    # "section"  : split on the policy's numbered headings -> a chunk is a whole rule
    # "fixed"    : split every N characters with overlap -> even sizes, rules can split
    chunk_strategy: Literal["section", "fixed"] = "section"
    chunk_size_chars: int = Field(default=900, ge=200, le=8_000)
    chunk_overlap_chars: int = Field(default=150, ge=0, le=2_000)
    # A seatbelt, not a guarantee: it only stops calls that go through our
    # CostLedger. The hard limit is the usage cap set in the OpenAI dashboard.
    max_spend_usd_per_run: float = Field(default=1.00, gt=0, le=1_000)

    # ---------- Paths ----------
    data_dir: Path = PROJECT_ROOT / "data"
    vector_store_dir: Path = PROJECT_ROOT / "data" / "processed" / "chroma"
    sqlite_path: Path = PROJECT_ROOT / "data" / "processed" / "policydesk.db"

    # ---------- Observability (optional) ----------
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "http://localhost:3000"

    # ---------- Derived helpers ----------

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def langfuse_enabled(self) -> bool:
        """Tracing is optional. If keys are absent, the app still works."""
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @model_validator(mode="after")
    def _require_provider_credentials(self) -> Settings:
        """Check the chosen provider actually has what it needs.

        Skipped when app_env=test so the test suite never needs a real key.
        """
        if self.is_test:
            return self

        if self.llm_provider == "openai" and not self.openai_api_key:
            raise ValueError(
                "LLM_PROVIDER=openai but OPENAI_API_KEY is empty.\n"
                "Fix: copy .env.example to .env and paste your key into OPENAI_API_KEY.\n"
                "(Or set APP_ENV=test to run without a key.)"
            )
        return self

    @model_validator(mode="after")
    def _overlap_must_be_smaller_than_chunk(self) -> Settings:
        if self.chunk_overlap_chars >= self.chunk_size_chars:
            raise ValueError(
                f"CHUNK_OVERLAP_CHARS ({self.chunk_overlap_chars}) must be smaller than "
                f"CHUNK_SIZE_CHARS ({self.chunk_size_chars}). Equal or larger overlap makes "
                "the splitter loop forever."
            )
        return self

    def safe_summary(self) -> dict[str, object]:
        """A view of the config that is safe to print or log.

        Secrets become 'set' or 'missing' -- never the value itself.
        """

        def flag(value: SecretStr | None) -> str:
            return "set" if value else "missing"

        return {
            "app_env": self.app_env,
            "log_level": self.log_level,
            "log_json": self.log_json,
            "llm_provider": self.llm_provider,
            "openai_api_key": flag(self.openai_api_key),
            "chat_model": self.chat_model,
            "answer_model": self.answer_model,
            "judge_model": self.judge_model,
            "llm_cache_enabled": self.llm_cache_enabled,
            "embedding_model": self.embedding_model,
            "embedding_backend": self.embedding_backend,
            "retrieval_top_k": self.retrieval_top_k,
            "max_graph_loops": self.max_graph_loops,
            "max_input_chars": self.max_input_chars,
            "sql_row_limit": self.sql_row_limit,
            "max_prompt_tokens": self.max_prompt_tokens,
            "chunk_strategy": self.chunk_strategy,
            "chunk_size_chars": self.chunk_size_chars,
            "chunk_overlap_chars": self.chunk_overlap_chars,
            "max_spend_usd_per_run": self.max_spend_usd_per_run,
            "request_timeout_s": self.request_timeout_s,
            "data_dir": str(self.data_dir),
            "vector_store_dir": str(self.vector_store_dir),
            "sqlite_path": str(self.sqlite_path),
            "langfuse_enabled": self.langfuse_enabled,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read settings once, then reuse the same object everywhere.

    lru_cache means the .env file is parsed a single time per process.
    In tests, call get_settings.cache_clear() to force a fresh read.
    """
    return Settings()
