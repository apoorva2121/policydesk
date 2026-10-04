"""A tool that CHANGES something — the first one, and the reason we need approval.

Why this tool exists
--------------------
Everything so far has been read-only, so "human in the loop" was theoretical.
`flag_expense` writes a record into a review queue. That is a consequential action:
it puts a named person's expense claim in front of Finance Operations. Getting it
wrong has a cost that no amount of apologising undoes.

So this is the tool that forces the question: **what must a human approve?**

Two design decisions worth noticing
-----------------------------------
**1. It does not write to the expense database.** It appends to a separate review
queue file. The expense database stays read-only to this entire application, which
means no bug anywhere in the agent can corrupt a financial record. Giving an agent
write access to your system of record, and relying on logic to use it carefully, is
the kind of decision that looks fine until the day it does not.

**2. The approval policy says a machine may flag but never decide.** That is not
our invention — it is in the company's own approval policy, section 2: "Automated
systems may flag a claim for escalation but may never approve or reject one." The
tool enforces what the document already says, which is how a control should be
derived.

Idempotency, and a mistake worth remembering
--------------------------------------------
Flagging the same expense twice must not create two queue entries. A duplicate
escalation wastes a reviewer's time and erodes trust in the queue.

The first version keyed on expense id **plus the reason text**. It did not work,
and the reason is instructive: the reason is written by a language model, which
never phrases the same thought identically twice. Two runs produced "No receipt
attached for reimbursement" and "Missing required receipt for an expense of USD 75
or more" -- the same finding, two strings, two queue entries.

**Never key idempotency on model-generated text.** Key it on a stable fact. Here
that is the expense id: one open review per expense, whatever words accompany it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .base import Tool, ToolError, ToolResult
from .sql_safety import run_read_only_query


class FlagExpenseInput(BaseModel):
    expense_id: int = Field(ge=1, description="The id of the expense to flag")
    reason: str = Field(
        min_length=10,
        max_length=300,
        description="Why it is being flagged, quoting the policy section it breaches",
    )
    policy_citation: str = Field(
        min_length=3,
        max_length=200,
        description="The citation supporting the reason, e.g. 'travel-policy.pdf (p.1, 2. Meals)'",
    )


class FlagExpenseTool(Tool):
    """Add an expense to the Finance Operations review queue."""

    name = "flag_expense"
    description = (
        "Add one expense to the Finance Operations review queue, with a reason and "
        "a policy citation. Use only when an expense appears to breach a specific "
        "written policy rule. This notifies a human reviewer about a named person's "
        "claim, so it always requires approval first."
    )
    input_model = FlagExpenseInput
    read_only = False
    requires_human_approval = True

    def __init__(self, queue_path: Path, database_path: Path):
        self.queue_path = Path(queue_path)
        self.database_path = Path(database_path)

    def existing_entries(self) -> list[dict]:
        """Read the queue. Returns an empty list when there is no queue yet."""
        if not self.queue_path.exists():
            return []
        entries = []
        for line in self.queue_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a corrupt line must not hide the rest of the queue
        return entries

    def _execute(self, payload: BaseModel) -> ToolResult:
        assert isinstance(payload, FlagExpenseInput)

        # Check the expense is real BEFORE writing anything. Flagging an id that
        # does not exist would put a phantom claim in front of a reviewer.
        found = run_read_only_query(
            self.database_path,
            "SELECT e.id, em.name, e.amount_usd, e.category "
            f"FROM expenses e JOIN employees em ON em.id = e.employee_id "
            f"WHERE e.id = {payload.expense_id}",
            row_limit=1,
        )
        if found.row_count == 0:
            raise ToolError(f"there is no expense with id {payload.expense_id}")

        _, employee_name, amount, category = found.rows[0]

        # Idempotency on a stable fact: the expense id. One open review per
        # expense. See the note at the top of this file for why the reason text
        # cannot be part of this key.
        for entry in self.existing_entries():
            if entry.get("expense_id") == payload.expense_id:
                return ToolResult(
                    ok=True,
                    tool=self.name,
                    summary=f"expense {payload.expense_id} was already in the queue; nothing added",
                    data={"expense_id": payload.expense_id, "duplicate": True, "entry": entry},
                )

        entry = {
            "expense_id": payload.expense_id,
            "employee_name": employee_name,
            "amount_usd": amount,
            "category": category,
            "reason": payload.reason,
            "policy_citation": payload.policy_citation,
            "flagged_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "flagged_by": "policydesk-agent",
            # Stated explicitly in the record, because the approval policy says a
            # machine may flag but never decide. The record must not look like a
            # decision to whoever reads it later.
            "decision": "none - awaiting human review",
        }

        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        with self.queue_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

        return ToolResult(
            ok=True,
            tool=self.name,
            summary=(
                f"expense {payload.expense_id} ({employee_name}, {category}, "
                f"USD {amount}) added to the review queue"
            ),
            data={"expense_id": payload.expense_id, "duplicate": False, "entry": entry},
        )
