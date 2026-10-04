"""Shared test setup.

Tests must never need a real API key, and must never read your personal .env.
This file makes both guarantees for every test automatically.
"""

from __future__ import annotations

import pytest

from policydesk.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test in a clean, key-free environment.

    autouse=True means pytest applies this to every test without being asked.
    """
    for var in (
        "APP_ENV",
        "LOG_LEVEL",
        "LOG_JSON",
        "LLM_PROVIDER",
        "OPENAI_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "RETRIEVAL_TOP_K",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    """A plain test-mode Settings object, ignoring any .env on disk."""
    return Settings(app_env="test", _env_file=None)
