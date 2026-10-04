"""Logging that will not leak your secrets.

Why this file exists
--------------------
Logs are the main way you will debug this system. But logs are also the most
common way secrets escape: someone logs the whole request object "just to see
it", and an API key ends up in a file, or in a cloud logging service, forever.

So we build logging with a safety net:

1. A "redactor" step that scans every log line and replaces any known secret
   value, plus anything that merely LOOKS like a key, with ***REDACTED***.
2. Two output styles: readable colours for you (dev), one JSON object per line
   for machines (prod). Machines parse JSON; humans do not.

Important and honest: the redactor is a safety net, not a licence to log
secrets. Do not log them on purpose. The net only catches accidents.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog
from pydantic import SecretStr

from .config import Settings

REDACTED = "***REDACTED***"

# Patterns that look like credentials even if we do not know the exact value.
# Example: an OpenAI-style key, or an HTTP Authorization header value.
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
)

# Keys whose VALUE should never be printed, whatever it contains.
SENSITIVE_KEY_NAMES: frozenset[str] = frozenset(
    {
        "api_key",
        "openai_api_key",
        "authorization",
        "password",
        "secret",
        "token",
        "langfuse_secret_key",
        "langfuse_public_key",
    }
)


def collect_known_secrets(settings: Settings) -> list[str]:
    """Pull the real secret values out of settings so we can match them exactly."""
    candidates = [
        settings.openai_api_key,
        settings.langfuse_public_key,
        settings.langfuse_secret_key,
    ]
    out: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, SecretStr):
            value = candidate.get_secret_value()
            # Ignore very short values: matching on "" or "x" would redact everything.
            if value and len(value) >= 8:
                out.append(value)
    return out


def _scrub_text(text: str, known_secrets: list[str]) -> str:
    for secret in known_secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def _scrub_value(value: Any, known_secrets: list[str]) -> Any:
    """Walk any nested structure and clean the strings inside it."""
    if isinstance(value, str):
        return _scrub_text(value, known_secrets)
    if isinstance(value, SecretStr):
        return REDACTED
    if isinstance(value, dict):
        return {k: _scrub_entry(k, v, known_secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        cleaned = [_scrub_value(v, known_secrets) for v in value]
        return type(value)(cleaned) if isinstance(value, tuple) else cleaned
    return value


def _scrub_entry(key: Any, value: Any, known_secrets: list[str]) -> Any:
    """Redact by field NAME first, then by content."""
    if isinstance(key, str) and key.lower() in SENSITIVE_KEY_NAMES:
        return REDACTED if value not in (None, "") else value
    return _scrub_value(value, known_secrets)


def make_redactor(known_secrets: list[str]):
    """Build the structlog processor.

    A "processor" is just a function structlog calls for every log line, in
    order, before the line is written. Ours is the last line of defence.
    """

    def redactor(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        return {k: _scrub_entry(k, v, known_secrets) for k, v in event_dict.items()}

    return redactor


# Libraries that log every HTTP request at INFO level. Those lines contain full
# request URLs, which is both noisy and a quiet way for data to reach your logs.
# We raise their level rather than relying on the redactor to catch it: not
# logging something is always safer than logging it and cleaning it up.
NOISY_LIBRARY_LOGGERS = ("httpx", "httpcore", "openai", "chromadb", "urllib3")


def configure_logging(settings: Settings) -> None:
    """Set up logging for the whole process. Call this once, at startup."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level),
        force=True,  # replace any handler a library installed behind our back
    )

    for name in NOISY_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # carries request_id etc.
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            make_redactor(collect_known_secrets(settings)),  # must run before output
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.log_level)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "policydesk") -> Any:
    """Get a logger. Usage: log = get_logger(__name__); log.info("hi", k=v)."""
    return structlog.get_logger(name)
