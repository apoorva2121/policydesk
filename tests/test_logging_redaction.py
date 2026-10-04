"""Tests for the log redaction safety net.

These test the processor function directly, which is the part that must never
be wrong. Testing it in isolation keeps the test fast and precise.
"""

from __future__ import annotations

from policydesk.config import Settings
from policydesk.logging_setup import REDACTED, collect_known_secrets, make_redactor

REAL_KEY = "sk-live-abcdefghijklmnop123456"


def _redactor_for(settings: Settings):
    return make_redactor(collect_known_secrets(settings))


def test_known_secret_is_removed_from_a_message() -> None:
    settings = Settings(app_env="test", openai_api_key=REAL_KEY, _env_file=None)
    redact = _redactor_for(settings)

    out = redact(None, "info", {"event": f"calling api with {REAL_KEY}"})

    assert REAL_KEY not in out["event"]
    assert REDACTED in out["event"]


def test_sensitive_field_name_is_removed_even_if_value_is_unknown() -> None:
    """A field called 'api_key' is hidden regardless of what is in it."""
    settings = Settings(app_env="test", _env_file=None)
    redact = _redactor_for(settings)

    out = redact(None, "info", {"event": "request", "api_key": "some-other-key-value"})

    assert out["api_key"] == REDACTED


def test_keylike_text_is_removed_even_if_we_never_configured_it() -> None:
    """Catches a key pasted by a user, or one belonging to another service."""
    settings = Settings(app_env="test", _env_file=None)
    redact = _redactor_for(settings)

    out = redact(None, "info", {"event": "oops sk-someoneelseskey9876543210 leaked"})

    assert "sk-someoneelseskey9876543210" not in out["event"]


def test_nested_structures_are_cleaned() -> None:
    """Secrets hide inside dicts and lists, which is how they usually escape."""
    settings = Settings(app_env="test", openai_api_key=REAL_KEY, _env_file=None)
    redact = _redactor_for(settings)

    out = redact(
        None,
        "info",
        {
            "event": "http_call",
            "headers": {"Authorization": f"Bearer {REAL_KEY}"},
            "trace": [{"note": REAL_KEY}],
        },
    )

    assert REAL_KEY not in str(out)


def test_ordinary_values_pass_through_untouched() -> None:
    """A redactor that mangles normal logs is useless."""
    settings = Settings(app_env="test", openai_api_key=REAL_KEY, _env_file=None)
    redact = _redactor_for(settings)

    out = redact(
        None, "info", {"event": "retrieved_chunks", "count": 5, "doc": "travel-policy.pdf"}
    )

    assert out == {"event": "retrieved_chunks", "count": 5, "doc": "travel-policy.pdf"}
