"""Tests for configuration loading and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from policydesk.config import Settings


def test_test_mode_needs_no_api_key() -> None:
    """The suite must run on a machine with no credentials at all."""
    settings = Settings(app_env="test", _env_file=None)
    assert settings.is_test is True
    assert settings.openai_api_key is None


def test_dev_mode_without_key_fails_with_a_helpful_message() -> None:
    """Fail fast: refuse to start, and say exactly how to fix it."""
    with pytest.raises(ValidationError) as caught:
        Settings(app_env="dev", llm_provider="openai", openai_api_key=None, _env_file=None)

    message = str(caught.value)
    assert "OPENAI_API_KEY" in message
    assert ".env" in message


def test_api_key_never_prints_itself() -> None:
    """SecretStr must hide the value in repr(), str() and model_dump()."""
    secret = "sk-not-a-real-key-123456789"
    settings = Settings(app_env="test", openai_api_key=secret, _env_file=None)

    assert secret not in repr(settings)
    assert secret not in str(settings)
    assert secret not in str(settings.model_dump())
    # ...but the real value is still available when the app genuinely needs it.
    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == secret


def test_safe_summary_reports_presence_not_value() -> None:
    settings = Settings(app_env="test", openai_api_key="sk-abcdefgh12345678", _env_file=None)
    summary = settings.safe_summary()

    assert summary["openai_api_key"] == "set"
    assert "sk-abcdefgh12345678" not in str(summary)


def test_limits_reject_nonsense_values() -> None:
    """Bad numbers are caught at startup, not at request time."""
    with pytest.raises(ValidationError):
        Settings(app_env="test", retrieval_top_k=0, _env_file=None)
    with pytest.raises(ValidationError):
        Settings(app_env="test", max_graph_loops=99, _env_file=None)


def test_langfuse_is_optional() -> None:
    """No tracing keys means tracing is off -- not a crash."""
    settings = Settings(app_env="test", _env_file=None)
    assert settings.langfuse_enabled is False
