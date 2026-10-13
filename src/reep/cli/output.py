"""Output for the ``reep`` command: the JSON envelope, tables and errors.

The JSON output is part of the public interface (see ``reep schema``). Field names
are stable; new fields can appear in a minor release, and a removal or rename needs
a new ``schema_version``.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import click

from reep.models import to_jsonable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from reep.errors import ReepError

__all__ = [
    "SCHEMA_VERSION",
    "Output",
    "emit_error",
    "is_interactive",
    "render_table",
]

#: The version of the JSON envelope.
SCHEMA_VERSION = 1

_MAX_CELL = 60


def is_interactive() -> bool:
    """Return ``True`` when both stdin and stdout are terminals."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _colour(stream: Any) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


@dataclass
class Output:
    """Where and how a command writes its result.

    Attributes:
        command: The command name, for the envelope.
        fmt: ``"table"``, ``"json"`` or ``"jsonl"``.
    """

    command: str
    fmt: str = "table"

    @property
    def machine(self) -> bool:
        """Return ``True`` for JSON or JSON Lines output."""
        return self.fmt in ("json", "jsonl")

    def envelope(
        self,
        data: Any,
        *,
        release_stamp: str | None,
        backend: str | None,
        truncated: bool = False,
        next_offset: int | None = None,
    ) -> dict[str, Any]:
        """Return the JSON envelope for a result."""
        return {
            "schema_version": SCHEMA_VERSION,
            "command": self.command,
            "release_stamp": release_stamp,
            "backend": backend,
            "data": to_jsonable(data),
            "truncated": truncated,
            "next_offset": next_offset,
        }

    def emit(
        self,
        data: Any,
        *,
        release_stamp: str | None,
        backend: str | None,
        text: str | None = None,
        truncated: bool = False,
        next_offset: int | None = None,
    ) -> None:
        """Write a result in the chosen format.

        Args:
            data: The result: a model, a list of models or JSON-ready data.
            release_stamp: The release that answered.
            backend: ``"local"``, ``"api"`` or ``None``.
            text: The text form for table output.
            truncated: Whether a list result has more rows.
            next_offset: The offset of the next page, when truncated.
        """
        if self.fmt == "json":
            envelope = self.envelope(
                data,
                release_stamp=release_stamp,
                backend=backend,
                truncated=truncated,
                next_offset=next_offset,
            )
            click.echo(json.dumps(envelope, ensure_ascii=False))
            return
        if self.fmt == "jsonl":
            rows = to_jsonable(data)
            for row in rows if isinstance(rows, list) else [rows]:
                click.echo(json.dumps(row, ensure_ascii=False))
            return
        if text:
            click.echo(text)
        if truncated and next_offset is not None:
            click.echo(
                f"(more results: add --offset {next_offset}, or raise --limit)",
                err=True,
            )

    def note(self, message: str) -> None:
        """Write a note to stderr for people; JSON output writes no notes."""
        if not self.machine:
            click.echo(message, err=True)


def emit_error(error: ReepError, fmt: str) -> None:
    """Write an error to stderr, as JSON for machine formats.

    Args:
        error: The error.
        fmt: The output format.
    """
    if fmt in ("json", "jsonl"):
        click.echo(json.dumps({"error": error.to_dict()}, ensure_ascii=False), err=True)
        return
    colour = _colour(sys.stderr)
    click.echo(
        click.style("Error: ", fg="red", bold=True) + error.message,
        err=True,
        color=colour,
    )
    if error.hint:
        click.echo(f"Hint: {error.hint}", err=True)


def render_table(
    rows: Sequence[dict[str, Any]], columns: Sequence[tuple[str, str]]
) -> str:
    """Return rows as an aligned text table.

    Args:
        rows: The rows.
        columns: ``(key, header)`` pairs.

    Returns:
        The table, or ``"(no results)"``.
    """
    if not rows:
        return "(no results)"
    cells = [[_cell(row.get(key)) for key, _ in columns] for row in rows]
    headers = [header for _, header in columns]
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in cells)) for i in range(len(columns))
    ]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines.extend(
        "  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in cells
    )
    return "\n".join(lines)


def render_fields(pairs: Sequence[tuple[str, Any]]) -> str:
    """Return label and value pairs as aligned lines, skipping empty values.

    Args:
        pairs: ``(label, value)`` pairs.

    Returns:
        The text.
    """
    shown = [
        (label, _cell(value, limit=None))
        for label, value in pairs
        if value not in (None, "", [], ())
    ]
    if not shown:
        return ""
    width = max(len(label) for label, _ in shown)
    return "\n".join(f"{label.ljust(width)}  {value}" for label, value in shown)


def _cell(value: Any, limit: int | None = _MAX_CELL) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(v) for v in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    if limit is not None and len(text) > limit:
        return text[: limit - 1] + "…"
    return text
