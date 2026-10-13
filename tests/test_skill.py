"""The shipped skill file, ``reep skill``, and the help text of every command."""

from __future__ import annotations

import re
import shlex
from importlib.resources import files
from pathlib import Path

import click
import pytest

from reep.cli.app import cli as reep_cli
from tests.support.httpserver import FileServer
from tests.test_cli import Cli

SKILL = files("reep.skill").joinpath("SKILL.md").read_text(encoding="utf-8")


def _examples() -> list[str]:
    """Return every reep command line in the skill file's bash blocks."""
    blocks = re.findall(r"^```bash\n(.*?)^```", SKILL, re.M | re.S)
    lines = [line.strip() for block in blocks for line in block.splitlines()]
    return [line for line in lines if line.startswith("reep ") or "| reep " in line]


@pytest.fixture
def run(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    server: FileServer,
) -> Cli:
    """A CLI in an empty folder, with no catalogue, served by the fixture bucket."""
    monkeypatch.setenv("REEP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("REEP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("REEP_RELEASES_URL", server.url)
    monkeypatch.setattr("reep.auth._keyring", lambda: None)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return Cli(capsys, monkeypatch)


def test_skill_file_has_frontmatter() -> None:
    assert SKILL.startswith("---\nname: reep\ndescription: ")
    front = SKILL.split("---")[1]
    assert "Transfermarkt" in front
    assert len(_examples()) >= 10


def test_skill_examples_run(
    run: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every example runs, in order, as an agent would run it on a new machine."""
    sa = pytest.importorskip("sqlalchemy")
    pytest.importorskip("yaml")
    # The database examples use the reep.yml that 'reep init' writes.
    db = tmp_path / "work" / "football.db"
    engine = sa.create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE players (id INTEGER PRIMARY KEY, tm_id VARCHAR(32), "
                "fbref_id VARCHAR(32), reep_id VARCHAR(16))"
            )
        )
        conn.execute(sa.text("INSERT INTO players (id, tm_id) VALUES (1, '568177')"))
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")

    for line in _examples():
        stdin = None
        if "|" in line:
            left, _, line = line.partition("|")
            printf = shlex.split(left)
            assert printf[0] == "printf", line
            stdin = printf[1].replace("\\n", "\n")
        args = shlex.split(line.strip())[1:]
        code, out, err = run(*args, stdin=stdin)
        # 3 (not found) and 4 (rows need review) are answers; anything else means
        # the example is wrong.
        assert code in (0, 3, 4), (line, code, err)
        if "--json" in args:
            # A lookup that finds nothing prints its JSON error on stderr.
            assert (out if code != 3 else out + err).strip(), line


def test_skill_print(run: Cli) -> None:
    code, out, _err = run("skill", "print")
    assert code == 0
    assert out == SKILL


def test_skill_install(
    run: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, doc = run.json("skill", "install", "--dir", str(tmp_path / "skills"))
    assert code == 0
    target = tmp_path / "skills" / "reep" / "SKILL.md"
    assert doc["data"] == {"path": str(target), "changed": True}
    assert target.read_text() == SKILL

    code, doc = run.json("skill", "install", "--dir", str(tmp_path / "skills"))
    assert doc["data"]["changed"] is False

    target.write_text("old")
    code, _out, err = run("skill", "install", "--dir", str(tmp_path / "skills"))
    assert code == 2
    assert "--force" in err
    code, doc = run.json(
        "skill", "install", "--dir", str(tmp_path / "skills"), "--force"
    )
    assert doc["data"]["changed"] is True

    code, doc = run.json("skill", "install", "--project")
    assert code == 0
    assert Path(doc["data"]["path"]) == Path(".claude/skills/reep/SKILL.md")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    code, doc = run.json("skill", "install")
    assert doc["data"]["path"] == str(tmp_path / "home/.claude/skills/reep/SKILL.md")

    code, _out, err = run("skill", "install", "--project", "--dir", "x")
    assert code == 2


def _leaf_commands(
    command: click.Command, path: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    if isinstance(command, click.Group):
        return [
            leaf
            for name, sub in sorted(command.commands.items())
            for leaf in _leaf_commands(sub, (*path, name))
        ]
    return [path]


def test_every_command_has_examples(run: Cli) -> None:
    leaves = _leaf_commands(reep_cli)
    assert ("sync",) in leaves
    assert ("mcp",) in leaves
    for path in leaves:
        code, out, _err = run(*path, "--help")
        assert code == 0, path
        assert "Examples:" in out, path
