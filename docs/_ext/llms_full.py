"""Write ``llms-full.txt``: the guide pages, the skill file and every command's help.

The file is built from the same sources as the HTML pages, so it does not drift from
the code. Sphinx writes it next to ``llms.txt`` in the output folder.
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path
from typing import Any

import click

#: The guide pages, in reading order.
PAGES = ("index", "quickstart", "concepts", "sources", "dbsync", "cli", "agents")

_DIRECTIVE = re.compile(r"^```\{(toctree|eval-rst)\}.*?^```\n?", re.M | re.S)


def _commands(
    command: click.Command, ctx: click.Context
) -> list[tuple[str, click.Context]]:
    out = [(ctx.command_path, ctx)]
    if isinstance(command, click.Group):
        for name in sorted(command.list_commands(ctx)):
            sub = command.get_command(ctx, name)
            if sub is None or sub.hidden:
                continue
            sub_ctx = click.Context(sub, info_name=name, parent=ctx)
            out.extend(_commands(sub, sub_ctx))
    return out


def cli_help() -> str:
    """Return the ``--help`` text of every ``reep`` command."""
    from reep.cli.app import cli

    root = click.Context(cli, info_name="reep", terminal_width=88, max_content_width=88)
    parts = []
    for path, ctx in _commands(cli, root):
        parts.append(f"### {path}\n\n```text\n{ctx.get_help()}\n```")
    return "\n\n".join(parts)


def render(srcdir: Path) -> str:
    """Return the contents of ``llms-full.txt``.

    Args:
        srcdir: The documentation source folder.

    Returns:
        The text.
    """
    sections = [
        "# reep: full documentation\n\n"
        "> Python SDK and command-line tool for the Reep Register, the open "
        "crosswalk of football provider IDs. Exact matching on (provider, namespace, "
        "external_id) only."
    ]
    for page in PAGES:
        text = (srcdir / f"{page}.md").read_text(encoding="utf-8")
        sections.append(_DIRECTIVE.sub("", text).strip())
    skill = files("reep.skill").joinpath("SKILL.md").read_text(encoding="utf-8")
    sections.append("# Agent skill file (SKILL.md)\n\n" + skill.strip())
    sections.append("# Command reference\n\n" + cli_help())
    return "\n\n".join(sections) + "\n"


def _write(app: Any, exception: Exception | None) -> None:
    if exception is not None or app.builder.format != "html":
        return
    out = Path(app.outdir) / "llms-full.txt"
    out.write_text(render(Path(app.srcdir)), encoding="utf-8")


def setup(app: Any) -> dict[str, Any]:
    """Register the extension with Sphinx."""
    app.connect("build-finished", _write)
    return {"version": "1", "parallel_read_safe": True, "parallel_write_safe": True}
