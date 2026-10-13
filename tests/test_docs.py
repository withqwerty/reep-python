"""The ``llms-full.txt`` generator in ``docs/_ext``."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

DOCS = Path(__file__).resolve().parents[1] / "docs"


def _extension() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "llms_full", DOCS / "_ext" / "llms_full.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_llms_full_has_every_page_the_skill_and_the_commands() -> None:
    ext = _extension()
    text = ext.render(DOCS)
    for heading in ("# Quickstart", "# Database sync", "# For AI agents"):
        assert f"\n{heading}\n" in text
    assert "name: reep" in text
    for command in (
        "### reep resolve",
        "### reep sync",
        "### reep mcp",
        "### reep skill install",
    ):
        assert command in text
    assert "{toctree}" not in text
    assert "{eval-rst}" not in text


def test_llms_txt_links_the_full_file() -> None:
    assert "llms-full.txt" in (DOCS / "llms.txt").read_text()


def test_markdown_copies_have_no_directives() -> None:
    ext = _extension()
    for page in ext.PAGES:
        text = ext.page_markdown(DOCS, page)
        assert text.startswith("# ")
        assert "{toctree}" not in text


def test_llms_txt_uses_the_published_address() -> None:
    text = (DOCS / "llms.txt").read_text()
    assert "https://reep.football/docs/python/quickstart.md" in text
    assert "https://reep.football/docs/python/llms-full.txt" in text
