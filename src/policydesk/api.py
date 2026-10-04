"""A FastAPI service in front of the agent.

What changes once there is an HTTP boundary
-------------------------------------------
Everything that was implicit becomes explicit, and several things that were fine
in a CLI become wrong:

- **Errors must be safe.** A stack trace in an HTTP response tells a stranger your
  file paths and your schema. Every handler returns a message that is useful
  without being revealing, and the detail goes to the log.
- **Requests must be bounded.** A CLI user types one question. An HTTP caller can
  send a 10MB one, a hundred at once, or the same one in a loop. So: a size limit
  on the body, and the per-run spend and tool-call caps already built in.
- **Health must be checkable.** Something other than a human needs to know whether
  this process is working. `/healthz` answers "am I alive", `/readyz` answers "can
  I actually serve a request" -- and the difference matters: a process that is
  alive but has no index should be restarted, not sent traffic.
- **Approvals need addresses.** The CLI prints a thread id. An API returns it, and
  a separate endpoint resolves it, because the approving human is a different
  person on a different machine at a later time.

What is deliberately NOT here
-----------------------------
Authentication. There is no login, no API key, no per-user filtering. That is a
*limitation*, recorded as one, not an oversight: a real deployment needs identity
before anything else, because document permissions must be enforced at retrieval
time against a known user, and a service with no identity cannot do that. Running
this on a public address would expose one company's expense data to everyone.

It binds to localhost by default for exactly that reason.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .logging_setup import configure_logging, get_logger

MAX_QUESTION_CHARS = 4_000


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=MAX_QUESTION_CHARS)
    # Off by default over HTTP. A caller must opt in to a run that could propose a
    # write, which is the safer default when you do not know who the caller is.
    allow_actions: bool = False


class Citation(BaseModel):
    number: int
    source: str


class AskResponse(BaseModel):
    thread_id: str
    answer: str | None
    refused: bool
    missing: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    verified: bool = False
    verify_problem: str | None = None
    waiting_for_approval: bool = False
    pending_action: dict[str, Any] | None = None
    trace: list[str] = Field(default_factory=list)
    usage: str = ""


class DecisionRequest(BaseModel):
    approval: str = Field(pattern="^(approved|rejected)$")


def create_app(settings: Settings | None = None):
    """Build the FastAPI application.

    Imported lazily so the CLI does not pay FastAPI's import cost on every command.
    """
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse

    settings = settings or get_settings()
    configure_logging(settings)
    log = get_logger("policydesk.api")

    app = FastAPI(
        title="PolicyDesk",
        version="0.1.0",
        description=(
            "Answers questions about one company's travel and expense policy and its "
            "expense records, with citations. A LEARNING project: no authentication, "
            "not for real data."
        ),
    )

    # -- error handling: safe outside, detailed inside ----------------------

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Never leak internals to a caller.

        A stack trace in a response body is a gift to anyone probing the service:
        file paths, library versions, sometimes the SQL that failed. The detail
        belongs in the log, where the redactor has already been applied.
        """
        log.error(
            "unhandled_request_error",
            path=str(request.url.path),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "the request could not be completed",
                "hint": "the detail is in the server log",
            },
        )

    # -- health -------------------------------------------------------------

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        """Am I alive? Answers without touching any dependency, on purpose.

        A liveness check that queries the database fails during a database blip and
        gets the process killed for something that was not its fault.
        """
        return {"status": "alive"}

    # `response_class` rather than a `-> JSONResponse` return annotation.
    #
    # This module uses `from __future__ import annotations`, so every annotation is
    # a STRING that FastAPI resolves against the function's module globals. But
    # FastAPI is imported lazily inside this factory -- deliberately, so the CLI
    # does not pay its import cost on every command -- which means `JSONResponse`
    # is a local name and the string never resolves. FastAPI then fell back to
    # treating the unresolved ForwardRef as a response MODEL, and generating the
    # schema died:
    #
    #   PydanticUserError: `TypeAdapter[Annotated[ForwardRef('JSONResponse'), ...]]`
    #   is not fully defined
    #
    # So `GET /openapi.json` and `/docs` both returned 500 while every actual
    # endpoint worked perfectly. No test caught it because the tests called the
    # endpoints, and nothing had ever asked the app to describe itself.
    @app.get("/readyz", tags=["ops"], response_class=JSONResponse)
    def readyz():
        """Can I actually serve a request? Checks the things a request needs.

        Separate from liveness because the right response differs: not-ready means
        "stop sending traffic", not-alive means "restart me".
        """
        from .rag import index_exists

        checks = {
            "index": index_exists(settings),
            "database": settings.sqlite_path.exists(),
            "model_credentials": bool(settings.openai_api_key) or settings.is_test,
        }
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not ready", "checks": checks},
        )

    # -- the agent ----------------------------------------------------------

    def _settings() -> Settings:
        return settings  # a dependency, so a test can override it

    @app.post("/ask", response_model=AskResponse, tags=["agent"])
    def ask(request: AskRequest, config: Settings = Depends(_settings)) -> AskResponse:
        """Ask the agent one question."""
        from .pipeline import run_agent
        from .rag import IndexMismatch

        try:
            run = run_agent(request.question, config, allow_actions=request.allow_actions)
        except IndexMismatch as exc:
            # 503, not 500: the service is correctly configured but not ready, and
            # the caller should retry after someone builds the index.
            log.warning("index_not_ready", error=str(exc))
            raise HTTPException(
                status_code=503, detail="the document index is missing or out of date"
            ) from exc

        return _to_response(run)

    @app.post("/threads/{thread_id}/decision", response_model=AskResponse, tags=["agent"])
    def decide(
        thread_id: str, request: DecisionRequest, config: Settings = Depends(_settings)
    ) -> AskResponse:
        """Approve or reject an action a paused run proposed."""
        from .pipeline import resume_agent

        try:
            run = resume_agent(thread_id, request.approval, config)
        except ValueError as exc:
            # The message here is safe: it says only whether the thread exists or
            # is waiting, which the caller already knows if it holds the id.
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return _to_response(run)

    @app.get("/queue", tags=["agent"])
    def queue(config: Settings = Depends(_settings)) -> dict[str, Any]:
        """The Finance Operations review queue."""
        import json

        path = config.data_dir / "processed" / "review-queue.jsonl"
        if not path.exists():
            return {"count": 0, "entries": []}
        entries = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return {"count": len(entries), "entries": entries}

    return app


def _to_response(run: Any) -> AskResponse:
    """Convert an AgentRun into the API's own shape.

    A deliberate translation layer rather than returning the internal state. The
    state holds evidence text, internal error detail and tool arguments; shipping
    all of it because it happens to be in the object is how internals leak.
    """
    state = run.state
    evidence = state.get("evidence") or []
    numbers = list(state.get("cited_numbers") or [])
    citations = list(state.get("citations") or [])

    action = state.get("pending_action")
    pending = (
        {"tool": action.tool, "arguments": action.arguments, "rationale": action.rationale}
        if action is not None
        else None
    )

    # On a refusal the raw answer still carries the internal protocol token the
    # model was told to emit -- "INSUFFICIENT_EVIDENCE: ...". That is a private
    # signal between the prompt and the graph, and a caller rendering `answer`
    # would put it in front of a person. `missing` already holds the same sentence
    # in plain words, and `answer` is Optional for exactly this case.
    refused = bool(state.get("refused"))
    answer = None if refused else state.get("answer")

    return AskResponse(
        thread_id=run.thread_id,
        answer=answer,
        refused=refused,
        missing=state.get("missing"),
        citations=[
            Citation(number=number, source=source)
            for number, source in zip(numbers, citations, strict=False)
        ],
        # The SQL is returned on purpose: it is how a caller checks that the
        # question was understood the way they meant it.
        queries=[item.sql for item in evidence if getattr(item, "sql", None)],
        verified=bool(state.get("verified")),
        verify_problem=state.get("verify_problem"),
        waiting_for_approval=run.waiting_for_approval,
        pending_action=pending,
        trace=list(state.get("trace") or []),
        usage=run.usage,
    )
