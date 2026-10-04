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

    chat_model: str = "gpt-4o-mini"  # cheap: routing, grading
    synthesis_model: str = "gpt-4o"  # stronger: final answers, judging
    embedding_model: str = "text-embedding-3-small"
    embedding_backend: Literal["provider", "local"] = "provider"

    # ---------- Safety and cost limits ----------
    # These are enforced in later milestones, but they are declared now so that
    # there is exactly one place to change them.
    max_input_chars: int = Field(default=4000, ge=100, le=100_000)
    retrieval_top_k: int = Field(default=5, ge=1, le=50)
    max_graph_loops: int = Field(default=2, ge=0, le=5)
    request_timeout_s: float = Field(default=30.0, gt=0, le=300)
    sql_row_limit: int = Field(default=200, ge=1, le=10_000)

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
            "synthesis_model": self.synthesis_model,
            "embedding_model": self.embedding_model,
            "embedding_backend": self.embedding_backend,
            "retrieval_top_k": self.retrieval_top_k,
            "max_graph_loops": self.max_graph_loops,
            "max_input_chars": self.max_input_chars,
            "sql_row_limit": self.sql_row_limit,
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
