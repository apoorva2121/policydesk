"""Tests for the HTTP boundary.

The theme: at an HTTP boundary, several things that were fine in a CLI become
wrong. Oversized input, leaked internals, and a health check that cannot tell
"restart me" from "stop sending me traffic".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from policydesk.api import MAX_QUESTION_CHARS, create_app
from policydesk.config import Settings


@pytest.fixture
def client(sample_data: dict[str, Path], tmp_path: Path) -> TestClient:
    settings = Settings(
        app_env="test",
        _env_file=None,
        data_dir=tmp_path,
        sqlite_path=sample_data["sqlite_db"],
        vector_store_dir=tmp_path / "chroma",
    )
    # raise_server_exceptions=False so the app's own error handler runs, which is
    # the thing being tested. With it True, TestClient re-raises and we would be
    # testing pytest instead of the API.
    return TestClient(create_app(settings), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_liveness_touches_no_dependency(client: TestClient) -> None:
    """A liveness check that queries the database gets the process killed during a
    database blip, for something that was not its fault."""
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_reports_what_is_missing(client: TestClient) -> None:
    """Separate from liveness because the right response differs: not-ready means
    stop sending traffic, not-alive means restart me."""
    response = client.get("/readyz")

    assert response.status_code == 503  # no index built in this fixture
    body = response.json()
    assert body["status"] == "not ready"
    assert body["checks"]["index"] is False
    assert body["checks"]["database"] is True  # the database IS there


# ---------------------------------------------------------------------------
# Input bounds
# ---------------------------------------------------------------------------


def test_an_oversized_question_is_rejected_before_any_work(client: TestClient) -> None:
    """A CLI user types one question. An HTTP caller can send ten megabytes."""
    response = client.post("/ask", json={"question": "x" * (MAX_QUESTION_CHARS + 100)})
    assert response.status_code == 422


def test_an_empty_question_is_rejected(client: TestClient) -> None:
    assert client.post("/ask", json={"question": ""}).status_code == 422


def test_a_malformed_body_is_rejected(client: TestClient) -> None:
    assert client.post("/ask", json={"not_the_field": "hello"}).status_code == 422


def test_actions_are_off_by_default_over_http(client: TestClient) -> None:
    """The safer default when you do not know who the caller is.

    A CLI user is the operator. An HTTP caller is a stranger until there is
    authentication, so a run that could propose a write has to be opted into.
    """
    from policydesk.api import AskRequest

    assert AskRequest(question="a question").allow_actions is False


def test_only_two_approval_values_are_accepted(client: TestClient) -> None:
    for value in ("maybe", "yes", "APPROVED", ""):
        response = client.post("/threads/abc/decision", json={"approval": value})
        assert response.status_code == 422, value


# ---------------------------------------------------------------------------
# Errors must not leak
# ---------------------------------------------------------------------------


def test_an_unknown_thread_is_a_404_not_a_500(client: TestClient) -> None:
    response = client.post("/threads/does-not-exist/decision", json={"approval": "approved"})
    assert response.status_code == 404


def test_an_internal_error_never_returns_a_stack_trace(
    sample_data: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The most important test in this file.

    A stack trace in a response body is a gift to anyone probing the service: file
    paths, library versions, sometimes the SQL that failed. The detail goes to the
    log; the caller gets a sentence.
    """
    settings = Settings(
        app_env="test", _env_file=None, data_dir=tmp_path, sqlite_path=sample_data["sqlite_db"]
    )
    app = create_app(settings)
    test_client = TestClient(app, raise_server_exceptions=False)

    def explode(*args, **kwargs):
        raise RuntimeError("connection string is Server=prod;Password=hunter2")

    monkeypatch.setattr("policydesk.pipeline.run_agent", explode)

    response = test_client.post("/ask", json={"question": "what is the meal limit?"})

    assert response.status_code == 500
    body = response.text
    assert "hunter2" not in body
    assert "Password" not in body
    assert "Traceback" not in body
    assert "RuntimeError" not in body
    assert "could not be completed" in body


# ---------------------------------------------------------------------------
# The response shape
# ---------------------------------------------------------------------------


def test_the_response_is_a_translation_not_the_internal_state() -> None:
    """A deliberate translation layer.

    The agent state holds evidence text, internal error detail and tool arguments.
    Returning all of it because it happens to be in the object is how internals
    leak. The response model names exactly what a caller gets.
    """
    from policydesk.api import AskResponse

    fields = set(AskResponse.model_fields)

    # What a caller needs to judge an answer:
    assert {"answer", "refused", "citations", "queries", "verified"} <= fields
    # What it must never receive:
    assert "evidence" not in fields
    assert "internal_detail" not in fields
    assert "errors" not in fields


def test_the_queue_endpoint_handles_an_empty_queue(client: TestClient) -> None:
    """An absent file is an empty queue, not an error."""
    response = client.get("/queue")

    assert response.status_code == 200
    assert response.json() == {"count": 0, "entries": []}


def test_the_sql_is_returned_to_the_caller() -> None:
    """On purpose: it is how a caller checks the question was understood.

    "Q3" might be calendar or fiscal; "travel spend" might include conference fees.
    The answer hides that choice and the query shows it.
    """
    from policydesk.api import AskResponse

    assert "queries" in AskResponse.model_fields


# ---------------------------------------------------------------------------
# The Streamlit interface
# ---------------------------------------------------------------------------


def test_the_ui_offers_an_example_of_every_question_type() -> None:
    """The examples are the teaching surface: they show what the system can and
    cannot do, including the attack and the refusal."""
    from policydesk.ui import EXAMPLE_QUESTIONS

    labels = " ".join(name for name, _ in EXAMPLE_QUESTIONS).lower()
    for expected in ("policy", "database", "both", "refused", "attack", "approval"):
        assert expected in labels, expected


def test_the_ui_module_imports_without_streamlit_running() -> None:
    """Streamlit is imported inside the function, so the module is importable in
    CI and in tests without starting a server."""
    import policydesk.ui as ui

    assert callable(ui.run)


def test_the_app_can_describe_itself(client: TestClient) -> None:
    """`GET /openapi.json` and `/docs` returned 500 while every endpoint worked.

    `readyz` was annotated `-> JSONResponse`. With `from __future__ import
    annotations` that is the STRING "JSONResponse", resolved against the module's
    globals -- but FastAPI is imported lazily inside `create_app`, so the name is a
    local and never resolves. FastAPI then treated the unresolved ForwardRef as a
    response model and schema generation raised PydanticUserError.

    Nothing caught it because every test called an endpoint, and calling an endpoint
    never asks the application to describe itself. Two different capabilities.
    """
    response = client.get("/openapi.json")

    assert response.status_code == 200, response.text
    schema = response.json()
    assert "/readyz" in schema["paths"]
    assert "/ask" in schema["paths"]

    # The interactive docs page is what a human actually opens.
    assert client.get("/docs").status_code == 200


def test_a_refusal_does_not_ship_the_internal_protocol_token() -> None:
    """`answer` carried "INSUFFICIENT_EVIDENCE: ..." straight out of the model.

    That token is a private signal between the prompt and the graph, used so a
    refusal can be detected exactly rather than by matching apologetic phrasing. A
    caller rendering `answer` would have put it in front of a person.

    `missing` already holds the same sentence in plain words, and `answer` is
    Optional precisely so a refusal can leave it empty. The translation layer in
    `_to_response` exists to stop internals leaking merely because they happen to
    be in the object; this was a case of it not doing that.

    Tested directly on the translation rather than over HTTP, because reaching a
    model-produced refusal needs a real API key and this has nothing to do with the
    network.
    """
    from types import SimpleNamespace

    from policydesk.api import _to_response

    refusal = SimpleNamespace(
        thread_id="run-test",
        state={
            "answer": "INSUFFICIENT_EVIDENCE: the evidence says nothing about share prices.",
            "refused": True,
            "missing": "the evidence says nothing about share prices.",
        },
        usage="",
        tracer=None,
        waiting_for_approval=False,
    )

    response = _to_response(refusal)

    assert response.refused is True
    assert response.answer is None
    assert response.missing, "a refusal must still say what was missing"
    assert "INSUFFICIENT_EVIDENCE" not in response.model_dump_json()

    # An ordinary answer is still passed through untouched.
    answered = SimpleNamespace(
        thread_id="run-test-2",
        state={"answer": "The limit is USD 85 [1].", "refused": False},
        usage="",
        tracer=None,
        waiting_for_approval=False,
    )
    assert _to_response(answered).answer == "The limit is USD 85 [1]."
