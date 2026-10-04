"""Tests for the tool contract: validation, failure handling, and the allow-list."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from fakes import FakeLLMClient
from policydesk.config import Settings
from policydesk.tools import SqlQueryTool, Tool, ToolError, ToolRegistry, ToolResult
from policydesk.tools.sql_safety import QueryResult
from policydesk.tools.sql_tool import _strip_code_fence, describe_suspicious_result


class EchoInput(BaseModel):
    text: str = Field(min_length=1, max_length=20)


class EchoTool(Tool):
    name = "echo"
    description = "Return the text given to it."
    input_model = EchoInput

    def _execute(self, payload: BaseModel) -> ToolResult:
        assert isinstance(payload, EchoInput)
        return ToolResult(ok=True, tool=self.name, summary="echoed", data={"text": payload.text})


class ExplodingTool(Tool):
    name = "explode"
    description = "Always fails with an unexpected error."
    input_model = EchoInput

    def _execute(self, payload: BaseModel) -> ToolResult:
        raise RuntimeError("database password is hunter2 and the path is /srv/secret.db")


class RefusingTool(Tool):
    name = "refuse"
    description = "Always fails politely."
    input_model = EchoInput

    def _execute(self, payload: BaseModel) -> ToolResult:
        raise ToolError("that is not available right now.", internal_detail="internal reason")


class WritingTool(Tool):
    name = "write_thing"
    description = "Changes data."
    input_model = EchoInput
    read_only = False
    requires_human_approval = True

    def _execute(self, payload: BaseModel) -> ToolResult:
        return ToolResult(ok=True, tool=self.name, summary="written")


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_a_working_tool_returns_its_data_and_a_duration() -> None:
    result = EchoTool().run({"text": "hello"})

    assert result.ok
    assert result.data["text"] == "hello"
    assert result.duration_ms >= 0


def test_invalid_input_is_rejected_before_the_tool_runs() -> None:
    """A tool must never see input it did not ask for."""
    result = EchoTool().run({"text": "x" * 500})

    assert not result.ok
    assert "not valid" in (result.error or "")
    assert result.internal_detail  # the validation detail is kept, for logs


def test_a_missing_field_is_a_clean_failure() -> None:
    result = EchoTool().run({})
    assert not result.ok


def test_an_unexpected_exception_becomes_a_result_not_a_crash() -> None:
    """An agent must be able to see a failure and decide what to do next."""
    result = ExplodingTool().run({"text": "x"})

    assert not result.ok
    assert result.error == "'explode' failed unexpectedly."


def test_internal_details_never_reach_the_user_facing_error() -> None:
    """The reason this split exists.

    A raw exception message can contain a password, a file path or the query text.
    It belongs in the logs, never in a response.
    """
    result = ExplodingTool().run({"text": "x"})

    assert "hunter2" not in (result.error or "")
    assert "/srv/secret.db" not in (result.error or "")
    assert "hunter2" in (result.internal_detail or "")  # kept for the operator


def test_a_deliberate_tool_error_gives_a_safe_message() -> None:
    result = RefusingTool().run({"text": "x"})

    assert not result.ok
    assert result.error == "that is not available right now."
    assert result.internal_detail == "internal reason"


# ---------------------------------------------------------------------------
# The registry: the allow-list
# ---------------------------------------------------------------------------


def test_an_unknown_tool_name_is_refused_clearly() -> None:
    """A model asking for a tool that does not exist must not crash the run."""
    registry = ToolRegistry([EchoTool()])
    result = registry.call("delete_everything", {})

    assert not result.ok
    assert "no tool called 'delete_everything'" in (result.error or "")
    assert "echo" in (result.error or "")  # tells the model what IS available


def test_the_registry_is_the_allow_list() -> None:
    """Only registered tools can be called. That is the control, not the prompt."""
    registry = ToolRegistry([EchoTool()])

    assert registry.names() == ["echo"]
    assert registry.get("explode") is None


def test_duplicate_tool_names_are_a_programming_error() -> None:
    registry = ToolRegistry([EchoTool()])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(EchoTool())


def test_the_prompt_description_flags_dangerous_tools() -> None:
    """A router choosing a tool must be able to see which ones write or need approval."""
    registry = ToolRegistry([EchoTool(), WritingTool()])
    described = registry.describe_for_prompt()

    assert "WRITES DATA" in described
    assert "NEEDS APPROVAL" in described
    assert "echo" in described


def test_tool_specs_include_an_input_schema() -> None:
    spec = EchoTool().spec()

    assert spec["name"] == "echo"
    assert spec["read_only"] is True
    assert "properties" in spec["input_schema"]


# ---------------------------------------------------------------------------
# The SQL tool's own helpers
# ---------------------------------------------------------------------------


def test_code_fences_are_stripped() -> None:
    """Models wrap SQL in fences however firmly you ask them not to."""
    assert _strip_code_fence("```sql\nSELECT 1\n```") == "SELECT 1"
    assert _strip_code_fence("```\nSELECT 1\n```") == "SELECT 1"
    assert _strip_code_fence("SELECT 1") == "SELECT 1"


def _result(columns: tuple[str, ...], rows: tuple[tuple, ...]) -> QueryResult:
    return QueryResult(
        columns=columns, rows=rows, sql_executed="SELECT ...", truncated=False, duration_ms=1
    )


def test_an_empty_result_is_flagged_as_suspicious() -> None:
    """Because an empty result is also exactly what a wrong filter looks like."""
    message = describe_suspicious_result(_result(("spend",), ()))
    assert message is not None
    assert "no rows matched" in message


def test_a_null_aggregate_is_flagged_as_suspicious() -> None:
    """The real failure this came from: SUM() over an empty set returns NULL."""
    message = describe_suspicious_result(_result(("spend",), ((None,),)))
    assert message is not None
    assert "filter" in message


def test_a_normal_result_is_not_flagged() -> None:
    assert describe_suspicious_result(_result(("spend",), ((1464.8,),))) is None


def test_a_row_of_zeroes_is_not_flagged() -> None:
    """Zero is a real answer. NULL is an absent one. They must not be confused."""
    assert describe_suspicious_result(_result(("spend",), ((0.0,),))) is None


def test_cannot_answer_is_turned_into_a_clear_failure(
    sample_data: dict[str, Path], tmp_path: Path
) -> None:
    settings = Settings(app_env="test", _env_file=None, sqlite_path=sample_data["sqlite_db"])
    llm = FakeLLMClient(settings, chat_reply="CANNOT_ANSWER")
    tool = SqlQueryTool(sample_data["sqlite_db"], llm)

    result = tool.run({"question": "What is the capital of France?"})

    assert not result.ok
    assert "cannot be answered from the expense database" in (result.error or "")


def test_a_rejected_query_is_not_echoed_back_to_the_user(
    sample_data: dict[str, Path],
) -> None:
    """Echoing a rejected query is how an attacker learns your schema."""
    settings = Settings(app_env="test", _env_file=None, sqlite_path=sample_data["sqlite_db"])
    llm = FakeLLMClient(settings, chat_reply="DROP TABLE expenses")
    tool = SqlQueryTool(sample_data["sqlite_db"], llm)

    result = tool.run({"question": "remove all expenses"})

    assert not result.ok
    assert "DROP TABLE" not in (result.error or "")
    assert "DROP TABLE" in (result.internal_detail or "")


def test_the_sql_prompt_says_the_question_is_not_instructions(
    sample_data: dict[str, Path],
) -> None:
    settings = Settings(app_env="test", _env_file=None, sqlite_path=sample_data["sqlite_db"])
    llm = FakeLLMClient(settings, chat_reply="SELECT id FROM expenses")
    tool = SqlQueryTool(sample_data["sqlite_db"], llm)
    tool.run({"question": "list expenses"})

    system = llm.chat_calls[0]["messages"][0]["content"]
    assert "never a" in system and "instructions" in system
    # The business glossary must be present: without it the model cannot map
    # "travel spend" onto the real category values.
    assert "travel spend" in system
    assert "SCHEMA" in system
