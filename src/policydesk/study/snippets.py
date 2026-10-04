"""Pulling real source code out of the project, by name.

Why not just paste the code into the study content
--------------------------------------------------
Because it would be wrong within a week. A tutorial that quotes code is a copy
that drifts: you fix a bug in the real file and the lesson keeps teaching the bug.

So the study UI never contains source code. It contains the *name* of a function,
and this module goes and reads it out of the actual file using Python's own parser.
If `verify_node` changes tomorrow, the lesson shows the change. If it is renamed,
the lesson says so loudly instead of quietly showing something stale.

That is the same principle as the index manifest and the dated price table: a copy
of a fact needs a way to notice when the fact moved.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = PROJECT_ROOT / "src" / "policydesk"


@dataclass(frozen=True)
class Snippet:
    """A piece of real source, with where it came from."""

    path: str
    symbol: str
    code: str
    first_line: int
    docstring: str | None = None
    error: str | None = None

    @property
    def found(self) -> bool:
        return self.error is None

    @property
    def location(self) -> str:
        return f"{self.path}:{self.first_line}"


def read_symbol(relative_path: str, symbol: str) -> Snippet:
    """Return the source of one function, class or method.

    `symbol` may be dotted for a method: "ToolRegistry.describe_for_prompt".

    A missing symbol is reported as an error rather than raised, because a study
    screen that cannot show one snippet should still show the other nine.
    """
    path = SOURCE_ROOT / relative_path
    if not path.exists():
        return Snippet(
            path=relative_path,
            symbol=symbol,
            code="",
            first_line=0,
            error=f"{relative_path} does not exist any more",
        )

    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return Snippet(
            path=relative_path,
            symbol=symbol,
            code="",
            first_line=0,
            error=f"could not parse {relative_path}: {exc}",
        )

    node = _find(tree, symbol.split("."))
    if node is None:
        return Snippet(
            path=relative_path,
            symbol=symbol,
            code="",
            first_line=0,
            error=(
                f"'{symbol}' is no longer in {relative_path}. The code has moved or "
                "been renamed, and this lesson needs updating."
            ),
        )

    lines = text.splitlines()
    # Include any decorators, which are part of how the function behaves.
    start = min([node.lineno, *[d.lineno for d in getattr(node, "decorator_list", [])]])
    end = node.end_lineno or node.lineno
    code = "\n".join(lines[start - 1 : end])

    return Snippet(
        path=relative_path,
        symbol=symbol,
        code=code,
        first_line=start,
        docstring=ast.get_docstring(node),
    )


def _find(tree: ast.AST, parts: list[str]) -> ast.AST | None:
    """Walk a dotted path through a module's top-level definitions."""
    current: ast.AST | None = tree
    for part in parts:
        if current is None:
            return None
        found = None
        for child in ast.iter_child_nodes(current):
            if (
                isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and child.name == part
            ):
                found = child
                break
        current = found
    return current


def read_constant(relative_path: str, name: str) -> Snippet:
    """Return the source of a module-level assignment, e.g. a prompt string.

    Prompts are the most important text in this project and the easiest to get
    wrong by quoting. This reads them from the file so a study screen always shows
    the prompt that is actually being sent.
    """
    path = SOURCE_ROOT / relative_path
    if not path.exists():
        return Snippet(
            path=relative_path,
            symbol=name,
            code="",
            first_line=0,
            error=f"{relative_path} does not exist any more",
        )

    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.splitlines()

    for node in ast.iter_child_nodes(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]

        if name in targets:
            start, end = node.lineno, node.end_lineno or node.lineno
            return Snippet(
                path=relative_path,
                symbol=name,
                code="\n".join(lines[start - 1 : end]),
                first_line=start,
            )

    return Snippet(
        path=relative_path,
        symbol=name,
        code="",
        first_line=0,
        error=f"'{name}' is no longer defined in {relative_path}",
    )


def read_module_docstring(relative_path: str) -> Snippet:
    """Return a module's docstring -- which in this project is the lesson.

    Every module here opens with an explanation of why it exists and what it
    trades off. Those docstrings are the real teaching material, so the study UI
    reads them rather than paraphrasing them.
    """
    path = SOURCE_ROOT / relative_path
    if not path.exists():
        return Snippet(
            path=relative_path,
            symbol="__doc__",
            code="",
            first_line=0,
            error=f"{relative_path} does not exist any more",
        )

    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc = ast.get_docstring(tree)
    if not doc:
        return Snippet(
            path=relative_path,
            symbol="__doc__",
            code="",
            first_line=0,
            error=f"{relative_path} has no module docstring",
        )
    return Snippet(path=relative_path, symbol="__doc__", code=doc, first_line=1, docstring=doc)
