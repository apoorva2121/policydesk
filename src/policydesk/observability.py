"""Tracing: seeing what one request actually did, step by step.

The problem this solves
-----------------------
A run of this agent makes five to fifteen model calls across nine nodes, with two
loops. When someone says "it was slow" or "it gave a wrong answer", the question is
always *where*. Logs tell you a sequence of events; a trace tells you a **tree**:
this run contained these spans, this span took 4.1 seconds, this one cost $0.0008,
this one is where the wrong number first appeared.

Our `trace` list in the agent state already gives a readable account of a run, and
for learning it is often enough. Langfuse adds three things it cannot:

1. **Timing and cost per step**, so "slow" becomes "the second policy_search took
   4 seconds".
2. **History across runs**, so you can see that a prompt change made answers worse
   last Tuesday.
3. **A shared view**, so someone who was not at your keyboard can look.

Deliberately optional
---------------------
If Langfuse is not configured, every function here becomes a no-op and the
application behaves identically. That is not politeness -- an observability
integration that can break the thing it observes is a liability, and the first
outage caused by a tracing backend teaches the lesson expensively.

What is NOT sent
----------------
Prompt and document text are redacted before leaving the process. A retrieved
chunk contains names and email addresses, and shipping them to a third-party
service copies personal data out of your system, where it is kept for months and
is subject to deletion requests you cannot honour. So spans carry *shapes* --
token counts, costs, citations, durations -- not content, unless you deliberately
turn content on for local debugging.
"""

from __future__ import annotations

from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .guardrails import redact_pii


@dataclass
class SpanRecord:
    """One step, recorded locally whether or not Langfuse is running."""

    name: str
    duration_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class Tracer:
    """Records spans locally, and mirrors them to Langfuse when it is configured.

    The local record is the point. It means `policydesk agent --trace` works with
    no account, no Docker and no network, and the Langfuse integration is a bonus
    rather than a dependency.
    """

    def __init__(self, settings: Settings, enabled: bool | None = None):
        self.settings = settings
        self.spans: list[SpanRecord] = []
        self._client = None
        self._trace = None

        wanted = settings.langfuse_enabled if enabled is None else enabled
        self.enabled = bool(wanted)
        self.unavailable_reason: str | None = None

        if self.enabled:
            self._connect()

    def _connect(self) -> None:
        """Try to reach Langfuse. Failure disables tracing, never the app."""
        try:
            from langfuse import Langfuse
        except ImportError:
            self.enabled = False
            self.unavailable_reason = (
                "the langfuse package is not installed; tracing is local-only. "
                "Install it with: uv pip install langfuse"
            )
            return

        try:
            assert self.settings.langfuse_public_key is not None
            assert self.settings.langfuse_secret_key is not None
            self._client = Langfuse(
                public_key=self.settings.langfuse_public_key.get_secret_value(),
                secret_key=self.settings.langfuse_secret_key.get_secret_value(),
                host=self.settings.langfuse_host,
            )
        except Exception as exc:  # noqa: BLE001 - tracing must never break the app
            self.enabled = False
            self.unavailable_reason = (
                f"could not reach Langfuse at {self.settings.langfuse_host}: {exc}"
            )

    # -- recording ----------------------------------------------------------

    @contextmanager
    def span(self, name: str, **metadata: Any):
        """Time a step and record it. Safe to use whether or not Langfuse is up."""
        import time

        started = time.monotonic()
        try:
            yield
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            safe = {key: _safe_value(value) for key, value in metadata.items()}
            self.spans.append(SpanRecord(name=name, duration_ms=duration_ms, metadata=safe))
            self._mirror(name, duration_ms, safe)

    def _mirror(self, name: str, duration_ms: int, metadata: dict[str, Any]) -> None:
        if not self.enabled or self._client is None:
            return
        try:
            # Deliberately metadata-only: no prompt text, no document content.
            self._client.create_event(name=name, metadata={**metadata, "duration_ms": duration_ms})
        except Exception:  # noqa: BLE001
            # One failed send must not take the request with it. We stop trying
            # for the rest of this run rather than failing on every span.
            self.enabled = False
            self.unavailable_reason = "a span failed to send; tracing disabled for this run"

    def flush(self) -> None:
        """Send anything buffered. A failure here is never worth an error."""
        if self._client is not None:
            with suppress(Exception):
                self._client.flush()

    # -- reading ------------------------------------------------------------

    @property
    def total_ms(self) -> int:
        return sum(span.duration_ms for span in self.spans)

    def slowest(self, count: int = 3) -> list[SpanRecord]:
        """The steps worth looking at first when someone says it was slow."""
        return sorted(self.spans, key=lambda span: span.duration_ms, reverse=True)[:count]

    def summary_lines(self) -> list[str]:
        """A local, readable trace: name, duration, share of the total."""
        total = self.total_ms or 1
        lines = []
        for span in self.spans:
            share = span.duration_ms / total
            bar = "#" * max(1, round(share * 24))
            extra = " ".join(f"{k}={v}" for k, v in span.metadata.items() if v not in (None, ""))
            lines.append(
                f"  {span.name:<16} {span.duration_ms:>6}ms {share:>5.0%} {bar:<24} {extra}"
            )
        return lines

    def status_line(self) -> str:
        if self.enabled:
            return f"tracing to Langfuse at {self.settings.langfuse_host}"
        if self.unavailable_reason:
            return f"local tracing only ({self.unavailable_reason})"
        return "local tracing only (Langfuse keys not set)"


def _safe_value(value: Any) -> Any:
    """Keep shapes, drop content.

    Numbers and booleans pass through. Strings are truncated and PII-redacted,
    because a span's metadata is the easiest place for a document's text to escape
    into a third-party service without anyone deciding that it should.
    """
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value)
    if len(text) > 200:
        text = text[:200] + "..."
    return redact_pii(text)


def docker_compose_snippet() -> str:
    """A self-hosted Langfuse, for someone who wants the real thing locally.

    Printed rather than written to the repository: it is a convenience for one
    machine, and a compose file in version control implies a supported deployment
    that this project does not have.
    """
    return """# Save as docker-compose.langfuse.yml, then:
#   docker compose -f docker-compose.langfuse.yml up -d
#   open http://localhost:3000, create a project, copy the keys into .env
#
# Free and self-hosted. Nothing leaves your machine.
services:
  langfuse-db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: langfuse
      POSTGRES_PASSWORD: langfuse
      POSTGRES_DB: langfuse
    volumes:
      - langfuse-db-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U langfuse"]
      interval: 5s
      retries: 10

  langfuse:
    image: langfuse/langfuse:2
    depends_on:
      langfuse-db:
        condition: service_healthy
    ports:
      - "3000:3000"
    environment:
      DATABASE_URL: postgresql://langfuse:langfuse@langfuse-db:5432/langfuse
      NEXTAUTH_URL: http://localhost:3000
      # Local development values. Generate real ones for anything shared:
      #   openssl rand -hex 32
      NEXTAUTH_SECRET: local-dev-only-change-me
      SALT: local-dev-only-change-me
      ENCRYPTION_KEY: 0000000000000000000000000000000000000000000000000000000000000000
      TELEMETRY_ENABLED: "false"

volumes:
  langfuse-db-data:
"""
