"""``reep init``, ``reep sync`` and ``reep review`` through the CLI."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

sa = pytest.importorskip("sqlalchemy")

from reep.config import Settings  # noqa: E402
from tests.support.fixture_release import MERGED, PALMER  # noqa: E402
from tests.test_cli import Cli  # noqa: E402

CONFIG = """\
version: 1
database: ${TEST_DB_URL}
run_log: false
tables:
  players:
    table: players
    entity_type: player
    primary_key: id
    reep_id: reep_id
    status: reep_status
    provider_ids:
      transfermarkt/spieler: tm_id
      fbref/person: fbref_id
  clean:
    table: clean_players
    entity_type: player
    primary_key: id
    reep_id: reep_id
    provider_ids:
      transfermarkt/spieler: tm_id
"""


@pytest.fixture
def cli(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    store_s1: Settings,
) -> Cli:
    monkeypatch.setenv("REEP_CACHE_DIR", str(store_s1.cache_dir))
    monkeypatch.setenv("REEP_CONFIG_DIR", str(store_s1.config_dir))
    monkeypatch.setattr("reep.auth._keyring", lambda: None)
    return Cli(capsys, monkeypatch)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A folder with reep.yml and a SQLite database; the current directory."""
    db = tmp_path / "data.db"
    engine = sa.create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        for name in ("players", "clean_players"):
            conn.execute(
                sa.text(
                    f"CREATE TABLE {name} (id INTEGER PRIMARY KEY, tm_id VARCHAR(32), "
                    "fbref_id VARCHAR(32), reep_id VARCHAR(16), "
                    "reep_status VARCHAR(32))"
                )
            )
        conn.execute(
            sa.text("INSERT INTO players (id, tm_id, reep_id) VALUES (:i, :t, :r)"),
            [
                {"i": 1, "t": "568177", "r": None},
                {"i": 2, "t": "999999", "r": None},
                {"i": 3, "t": None, "r": MERGED},
            ],
        )
        conn.execute(
            sa.text("INSERT INTO clean_players (id, tm_id) VALUES (1, '568177')")
        )
    engine.dispose()
    (tmp_path / "reep.yml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("TEST_DB_URL", f"sqlite:///{db}")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _reep_ids(workspace: Path) -> dict[int, Any]:
    engine = sa.create_engine(f"sqlite:///{workspace / 'data.db'}")
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT id, reep_id FROM players ORDER BY id"))
        out = {int(i): r for i, r in rows}
    engine.dispose()
    return out


def test_init_writes_an_example(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, doc = cli.json("init")
    assert code == 0
    assert doc["data"]["path"] == "reep.yml"
    text = (tmp_path / "reep.yml").read_text()
    assert "provider_ids:" in text

    code, _out, err = cli("init")
    assert code == 2
    assert "already exists" in err

    code, _out, _err = cli("init", "--force", "--path", "other.yml")
    assert code == 0
    assert (tmp_path / "other.yml").is_file()


def test_sync_dry_run_then_apply(cli: Cli, workspace: Path) -> None:
    code, doc = cli.json("sync", "reep", "--table", "players")
    assert code == 4  # rows 2 (unresolved) and 3 (redirected) need review
    data = doc["data"]
    assert data["apply"] is False
    assert data["planned"] == 4  # one Reep ID and three status cells
    assert data["written"] == 0
    assert data["outcomes"]["new"] == 1
    assert data["outcomes"]["unresolved"] == 1
    assert data["outcomes"]["redirected"] == 1
    assert data["needs_review"] == 2
    assert doc["release_stamp"] == data["release_stamp"]
    assert "s3cret" not in json.dumps(doc)
    assert _reep_ids(workspace)[1] is None

    code, doc = cli.json("sync", "reep", "--table", "players", "--apply")
    assert code == 4
    assert doc["data"]["written"] == 4
    assert _reep_ids(workspace) == {1: PALMER, 2: None, 3: MERGED}

    code, out, _err = cli("sync", "reep", "--table", "players", "--apply")
    assert code == 4
    assert "applied" in out
    assert "Needs review: redirected 1, unresolved 1." in out
    assert "reep review" in out

    code, doc = cli.json(
        "sync", "reep", "--table", "players", "--apply", "--repair-redirected"
    )
    assert doc["data"]["outcomes"]["repair"] == 1
    assert _reep_ids(workspace)[3] == PALMER


def test_sync_exit_0_when_nothing_needs_review(cli: Cli, workspace: Path) -> None:
    code, out, err = cli("sync", "reep", "--table", "clean", "--apply")
    assert code == 0, err
    assert "dry run" not in out
    code, doc = cli.json("review", "--table", "clean", "--target", "reep")
    assert code == 0
    assert doc["data"]["needs_review"] == 0


def test_sync_text_dry_run(cli: Cli, workspace: Path) -> None:
    code, out, _err = cli("sync", "reep", "--table", "clean")
    assert code == 0
    assert "dry run: nothing written; add --apply to write" in out
    assert "table" in out
    assert "clean" in out


def test_sync_jsonl_prints_one_line_per_target(cli: Cli, workspace: Path) -> None:
    code, out, _err = cli("sync", "reep", "fbref", "--table", "players", "--jsonl")
    assert code == 4
    lines = [json.loads(x) for x in out.splitlines()]
    assert [x["target"] for x in lines] == ["reep", "fbref/person"]


def test_review_writes_the_csv(cli: Cli, workspace: Path) -> None:
    code, doc = cli.json("review", "--output", "out.csv")
    assert code == 4
    data = doc["data"]
    assert data["command"] == "review"
    assert data["review_file"] == "out.csv"
    with (workspace / "out.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == data["needs_review"]
    assert {r["table"] for r in rows} == {"players"}


def test_run_log_options(cli: Cli, workspace: Path) -> None:
    log = workspace / "runs.jsonl"
    code, doc = cli.json("sync", "reep", "--table", "clean", "--log-file", str(log))
    assert code == 0
    assert doc["data"]["run_log"] == str(log)
    assert len(log.read_text().splitlines()) == 1
    code, doc = cli.json(
        "sync", "reep", "--table", "clean", "--log-file", str(log), "--no-log"
    )
    assert doc["data"]["run_log"] is None
    assert len(log.read_text().splitlines()) == 1


def test_config_from_the_environment(
    cli: Cli, workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    moved = workspace / "conf" / "reep.yml"
    moved.parent.mkdir()
    (workspace / "reep.yml").rename(moved)
    code, _out, err = cli("sync", "reep")
    assert code == 2
    assert "reep init" in err
    monkeypatch.setenv("REEP_CONFIG", str(moved))
    code, _doc = cli.json("sync", "reep", "--table", "clean")
    assert code == 0


def test_missing_env_var_is_a_usage_error(
    cli: Cli, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_DB_URL")
    code, _out, err = cli("sync", "reep", "--json")
    assert code == 2
    assert json.loads(err)["error"]["code"] == "config_error"
    assert "TEST_DB_URL" in err


def test_database_error_exit_6(
    cli: Cli, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DB_URL", "postgresql://reep:s3cret@127.0.0.1:9/reep")
    code, _out, err = cli("sync", "reep", "--json")
    assert code == 6
    error = json.loads(err)["error"]
    assert error["code"] == "database_error"
    assert "s3cret" not in err


def test_unknown_target_exit_2(cli: Cli, workspace: Path) -> None:
    code, _out, err = cli("sync", "Not A Target")
    assert code == 2
    assert "not a sync target" in err


def test_no_catalogue_exit_7(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("REEP_CACHE_DIR", str(tmp_path / "empty-cache"))
    run = Cli(capsys, monkeypatch)
    code, _out, err = run("sync", "reep", "--json")
    assert code == 7
    assert json.loads(err)["error"]["code"] == "catalog_missing"


def test_schemas_exist(cli: Cli) -> None:
    for command in ("init", "sync", "review"):
        code, out, _err = cli("schema", command)
        assert code == 0
        assert json.loads(out)["properties"]["command"]["const"] == command
