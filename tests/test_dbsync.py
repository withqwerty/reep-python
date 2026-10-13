"""The database sync against a fixture table.

The tests run on SQLite. They also run on PostgreSQL, MySQL and SQL Server when
``REEP_TEST_POSTGRES_URL``, ``REEP_TEST_MYSQL_URL`` or ``REEP_TEST_MSSQL_URL`` is
set (CI sets all three).
"""

from __future__ import annotations

import csv
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sa = pytest.importorskip("sqlalchemy")

from reep import dbsync  # noqa: E402
from reep.config import Settings  # noqa: E402
from reep.errors import ConfigError, DatabaseError, InvalidArgumentError  # noqa: E402
from reep.register import Register  # noqa: E402
from tests.support.fake_api import BASE, VALID_KEY, FakeApi  # noqa: E402
from tests.support.fixture_release import (  # noqa: E402
    DECORROBORATED,
    MARESCA,
    MERGED,
    NAMESAKE,
    OZIL,
    PALMER,
    RETIRED,
    UNKNOWN,
    WITHHELD,
)

DIALECT_URLS = {
    "postgres": "REEP_TEST_POSTGRES_URL",
    "mysql": "REEP_TEST_MYSQL_URL",
    "mssql": "REEP_TEST_MSSQL_URL",
}
DIALECTS = ["sqlite"] + [
    name for name, var in DIALECT_URLS.items() if os.environ.get(var)
]

COLUMNS = (
    "tm_id",
    "whoscored_id",
    "wyscout_id",
    "fbref_id",
    "fotmob_id",
    "opta_id",
    "reep_id",
    "reep_status",
)

# id -> the values of the row. Each row tests one case; the comments give the
# expected outcome for the Reep ID column.
ROWS: dict[int, dict[str, Any]] = {
    1: {"tm_id": "568177"},  # new: Palmer
    2: {"whoscored_id": "333"},  # new: the bridge names a merged ID
    3: {"wyscout_id": 444},  # withdrawn: target_not_published
    4: {"wyscout_id": 777},  # retired: merged into a retired entity (written)
    5: {"tm_id": "999999"},  # unresolved: no bridge
    6: {},  # no_source
    7: {"tm_id": "35664", "reep_id": OZIL},  # unchanged
    8: {"reep_id": MERGED},  # redirected (repair writes Palmer)
    9: {"reep_id": DECORROBORATED},  # withdrawn
    10: {"reep_id": WITHHELD},  # withdrawn
    11: {"reep_id": UNKNOWN},  # unknown_id
    12: {"reep_id": RETIRED},  # retired (kept)
    13: {"tm_id": "568177", "reep_id": NAMESAKE},  # conflict: stored vs provider ID
    14: {"reep_id": "not-a-reep-id"},  # invalid_value
    15: {"reep_id": ""},  # invalid_value: an empty string is a value
    16: {"tm_id": "900001", "whoscored_id": "333"},  # conflict: two provider IDs
    17: {"opta_id": "c1"},  # type_mismatch: a coach in a player table
    18: {"reep_id": MARESCA},  # type_mismatch
    19: {"tm_id": "568177", "fbref_id": "dc7f8a28", "fotmob_id": "1001"},  # new
    20: {"tm_id": "900001"},  # new: the namesake
    21: {"tm_id": "568177", "fbref_id": "zzz"},  # new
}

REEP_OUTCOMES = {
    1: "new",
    2: "new",
    3: "withdrawn",
    4: "retired",
    5: "unresolved",
    6: "no_source",
    7: "unchanged",
    8: "redirected",
    9: "withdrawn",
    10: "withdrawn",
    11: "unknown_id",
    12: "retired",
    13: "conflict",
    14: "invalid_value",
    15: "invalid_value",
    16: "conflict",
    17: "type_mismatch",
    18: "type_mismatch",
    19: "new",
    20: "new",
    21: "new",
}

FBREF_OUTCOMES = {
    1: "new",
    2: "new",
    3: "withdrawn",
    4: "no_target",
    5: "unresolved",
    6: "unresolved",
    7: "no_target",
    8: "new",  # the stored ID merged into Palmer, who has an FBref ID
    9: "withdrawn",
    10: "withdrawn",
    11: "unresolved",
    12: "no_target",
    13: "conflict",
    14: "unresolved",
    15: "unresolved",
    16: "conflict",
    17: "type_mismatch",
    18: "type_mismatch",
    19: "unchanged",
    20: "no_target",
    21: "unresolved",
}


def _counts(outcomes: dict[int, str]) -> dict[str, int]:
    out = dict.fromkeys(dbsync.OUTCOMES, 0)
    for value in outcomes.values():
        out[value] += 1
    return out


def _engine(dialect: str, tmp_path: Path) -> Any:
    if dialect == "sqlite":
        return sa.create_engine(f"sqlite:///{tmp_path / 'players.db'}")
    url = os.environ[DIALECT_URLS[dialect]]
    return sa.create_engine(url)


@pytest.fixture(params=DIALECTS)
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    eng = _engine(request.param, tmp_path)
    yield eng
    eng.dispose()


@pytest.fixture
def table(engine: Any) -> Iterator[Any]:
    """A players table with the fixture rows, dropped after the test."""
    name = f"players_{uuid.uuid4().hex[:8]}"
    metadata = sa.MetaData()
    tbl = sa.Table(
        name,
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("tm_id", sa.String(32)),
        sa.Column("whoscored_id", sa.String(32)),
        sa.Column("wyscout_id", sa.Integer),
        sa.Column("fbref_id", sa.String(32)),
        sa.Column("fotmob_id", sa.String(32)),
        sa.Column("opta_id", sa.String(32)),
        sa.Column("reep_id", sa.String(16)),
        sa.Column("reep_status", sa.String(32)),
    )
    metadata.create_all(engine)
    rows = [{"id": i, **dict.fromkeys(COLUMNS), **values} for i, values in ROWS.items()]
    with engine.begin() as conn:
        conn.execute(tbl.insert(), rows)
    yield tbl
    metadata.drop_all(engine)


def _config(table_name: str, **extra: Any) -> dbsync.Config:
    entry: dict[str, Any] = {
        "table": table_name,
        "entity_type": "player",
        "primary_key": "id",
        "reep_id": "reep_id",
        "status": "reep_status",
        "provider_ids": {
            "transfermarkt/spieler": "tm_id",
            "whoscored/player": "whoscored_id",
            "wyscout/player": "wyscout_id",
            "fbref/person": "fbref_id",
            "fotmob/person": "fotmob_id",
            "opta/person": "opta_id",
        },
    }
    entry.update(extra)
    return dbsync.parse_config(
        {"version": 1, "run_log": False, "tables": {"players": entry}}
    )


def _snapshot(engine: Any, tbl: Any) -> dict[int, dict[str, Any]]:
    with engine.connect() as conn:
        result = conn.execute(sa.select(tbl).order_by(tbl.c.id))
        return {
            int(row["id"]): {c: row[c] for c in COLUMNS} for row in result.mappings()
        }


def _prefilled() -> dict[tuple[int, str], Any]:
    """Return every cell the fixture fills before a run."""
    return {
        (i, column): value
        for i, values in ROWS.items()
        for column, value in values.items()
    }


def test_first_run_counts_then_idempotent(
    engine: Any, table: Any, reg: Register
) -> None:
    config = _config(table.name)
    before = _snapshot(engine, table)

    dry = dbsync.sync(config, reg, ["reep"], engine=engine)
    assert dry.apply is False
    assert dry.written == 0
    [target] = dry.targets
    assert target.rows == len(ROWS)
    assert target.to_dict()["outcomes"] == _counts(REEP_OUTCOMES)
    assert target.planned == 6  # five new IDs and one retired ID
    assert target.status_planned == len(ROWS)
    assert _snapshot(engine, table) == before

    first = dbsync.sync(config, reg, ["reep"], apply=True, engine=engine)
    [target] = first.targets
    assert target.to_dict()["outcomes"] == _counts(REEP_OUTCOMES)
    assert target.written == 6
    assert target.status_written == len(ROWS)
    assert first.needs_review == sum(
        1 for o in REEP_OUTCOMES.values() if o in dbsync.REVIEW_OUTCOMES
    )

    after = _snapshot(engine, table)
    assert after[1]["reep_id"] == PALMER
    assert after[2]["reep_id"] == PALMER
    assert after[4]["reep_id"] == RETIRED
    assert after[20]["reep_id"] == NAMESAKE
    assert after[3]["reep_id"] is None
    assert after[5]["reep_id"] is None
    for i, outcome in REEP_OUTCOMES.items():
        expected = "resolved" if outcome in ("new", "unchanged") else outcome
        assert after[i]["reep_status"] == expected, i

    # No pre-filled cell changed.
    for (i, column), value in _prefilled().items():
        assert after[i][column] == value, (i, column)

    second = dbsync.sync(config, reg, ["reep"], apply=True, engine=engine)
    assert second.planned == 0
    assert second.written == 0
    assert _snapshot(engine, table) == after


def test_repair_redirected_changes_only_merged_ids(
    engine: Any, table: Any, reg: Register
) -> None:
    config = _config(table.name)
    dbsync.sync(config, reg, ["reep"], apply=True, engine=engine)
    before = _snapshot(engine, table)

    report = dbsync.sync(
        config, reg, ["reep"], apply=True, repair_redirected=True, engine=engine
    )
    [target] = report.targets
    assert target.outcomes.get("repair") == 1
    assert target.written == 1
    assert target.status_written == 1

    after = _snapshot(engine, table)
    changed = {(i, c) for i in ROWS for c in COLUMNS if before[i][c] != after[i][c]}
    assert changed == {(8, "reep_id"), (8, "reep_status")}
    assert after[8]["reep_id"] == PALMER
    assert after[8]["reep_status"] == "resolved"


def test_provider_sync_fills_through_the_reep_id(
    engine: Any, table: Any, reg: Register
) -> None:
    config = _config(table.name)
    before = _snapshot(engine, table)

    report = dbsync.sync(config, reg, ["fbref"], apply=True, engine=engine)
    [target] = report.targets
    assert target.target == "fbref/person"
    assert target.to_dict()["outcomes"] == _counts(FBREF_OUTCOMES)
    assert target.written == 3
    assert target.status_planned == 0  # the status column records the Reep ID sync

    after = _snapshot(engine, table)
    for i in (1, 2, 8):
        assert after[i]["fbref_id"] == "dc7f8a28"
    assert after[19]["fbref_id"] == "dc7f8a28"
    assert after[21]["fbref_id"] == "zzz"
    assert all(after[i]["reep_id"] == before[i]["reep_id"] for i in ROWS)

    again = dbsync.sync(config, reg, ["fbref"], apply=True, engine=engine)
    assert again.written == 0


def test_provider_sync_skips_ids_the_provider_retired(
    engine: Any, table: Any, reg: Register
) -> None:
    config = _config(table.name)
    report = dbsync.sync(config, reg, ["fotmob"], apply=True, engine=engine)
    [target] = report.targets
    # Palmer has a live FotMob ID (1002) and a retired one (1001); row 19 holds the
    # retired one.
    assert target.outcomes["new"] == 4  # rows 1, 2, 8 and 21
    assert target.outcomes["upstream_retired"] == 1
    assert target.written == 4
    after = _snapshot(engine, table)
    assert after[1]["fotmob_id"] == "1002"
    assert after[19]["fotmob_id"] == "1001"


def test_integer_provider_column(engine: Any, table: Any, reg: Register) -> None:
    # wyscout_id is an INTEGER column: rows 3 and 4 hold 444 and 777.
    config = _config(table.name)
    report = dbsync.sync(config, reg, ["wyscout"], engine=engine)
    [target] = report.targets
    expected = dict.fromkeys(dbsync.OUTCOMES, 0)
    expected.update(
        no_target=8,
        withdrawn=3,
        unchanged=1,
        unresolved=5,
        conflict=2,
        type_mismatch=2,
    )
    assert target.to_dict()["outcomes"] == expected


def test_review_writes_csv(
    engine: Any, table: Any, reg: Register, tmp_path: Path
) -> None:
    config = _config(table.name)
    out = tmp_path / "review.csv"
    report = dbsync.review(config, reg, output=out, engine=engine)
    assert report.command == "review"
    assert report.apply is False
    assert report.review_file == out
    assert {t.target for t in report.targets} == {
        "reep",
        "transfermarkt/spieler",
        "whoscored/player",
        "wyscout/player",
        "fbref/person",
        "fotmob/person",
        "opta/person",
    }
    with out.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == list(dbsync.REVIEW_COLUMNS)
    assert len(rows) == report.needs_review
    assert all(r["outcome"] in dbsync.REVIEW_OUTCOMES for r in rows)
    reep_rows = {int(r["primary_key"]): r for r in rows if r["target"] == "reep"}
    assert set(reep_rows) == {
        i for i, o in REEP_OUTCOMES.items() if o in dbsync.REVIEW_OUTCOMES
    }
    assert reep_rows[8]["detail"] == f"merged into {PALMER}"
    assert all(r["release_stamp"] == reg.release.stamp for r in rows)
    assert not out.with_name("review.csv.part").exists()


def test_run_log(engine: Any, table: Any, reg: Register, tmp_path: Path) -> None:
    log = tmp_path / "logs" / "runs.jsonl"
    config = _config(table.name)
    dbsync.sync(config, reg, ["reep"], engine=engine, run_log=log)
    report = dbsync.sync(config, reg, ["reep"], apply=True, engine=engine, run_log=log)
    assert report.run_log == log
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert [line["apply"] for line in lines] == [False, True]
    assert all(line["release_stamp"] == reg.release.stamp for line in lines)
    assert lines[1]["written"] == report.written


def test_default_run_log_uses_settings(
    engine: Any, table: Any, reg: Register, tmp_path: Path
) -> None:
    entry = dbsync.parse_config(
        {
            "version": 1,
            "tables": {
                "players": {
                    "table": table.name,
                    "entity_type": "player",
                    "primary_key": "id",
                    "reep_id": "reep_id",
                }
            },
        }
    )
    settings = Settings(
        api_key=None,
        duckdb_path=None,
        cache_dir=tmp_path / "cache",
        config_dir=tmp_path / "config",
        log_dir=tmp_path / "logdir",
    )
    report = dbsync.sync(entry, reg, ["reep"], engine=engine, settings=settings)
    expected = tmp_path / "logdir" / "dbsync-runs.jsonl"
    assert report.run_log == expected
    assert expected.is_file()


def test_missing_column_is_a_config_error(
    engine: Any, table: Any, reg: Register
) -> None:
    config = _config(table.name, reep_id="reep")
    with pytest.raises(ConfigError, match="no column 'reep'") as info:
        dbsync.sync(config, reg, ["reep"], engine=engine)
    assert "reep_status" in (info.value.hint or "")


def test_missing_table_is_a_config_error(engine: Any, reg: Register) -> None:
    config = _config("no_such_table")
    with pytest.raises(ConfigError, match="no table no_such_table"):
        dbsync.sync(config, reg, ["reep"], engine=engine)


def test_unknown_target(engine: Any, table: Any, reg: Register) -> None:
    config = _config(table.name)
    with pytest.raises(ConfigError, match="has a sofascore column"):
        dbsync.sync(config, reg, ["sofascore"], engine=engine)


def test_unknown_namespace_in_config(engine: Any, table: Any, reg: Register) -> None:
    config = _config(table.name, provider_ids={"fbref/player": "fbref_id"})
    with pytest.raises(ConfigError, match="no namespace 'player'") as info:
        dbsync.sync(config, reg, ["reep"], engine=engine)
    assert "person" in (info.value.hint or "")


def test_api_register_is_refused(table: Any, engine: Any) -> None:
    with (
        Register.api(VALID_KEY, base_url=BASE, http=FakeApi().client()) as reg,
        pytest.raises(InvalidArgumentError, match="local catalogue"),
    ):
        dbsync.sync(_config(table.name), reg, ["reep"], engine=engine)


def test_short_reep_id_column_is_refused(engine: Any, reg: Register) -> None:
    metadata = sa.MetaData()
    name = f"short_{uuid.uuid4().hex[:8]}"
    sa.Table(
        name,
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("tm_id", sa.String(32)),
        sa.Column("reep_id", sa.String(10)),
    )
    metadata.create_all(engine)
    try:
        config = dbsync.parse_config(
            {
                "version": 1,
                "run_log": False,
                "tables": {
                    "t": {
                        "table": name,
                        "entity_type": "player",
                        "primary_key": "id",
                        "reep_id": "reep_id",
                        "provider_ids": {"transfermarkt/spieler": "tm_id"},
                    }
                },
            }
        )
        with pytest.raises(ConfigError, match="needs 16 or more"):
            dbsync.sync(config, reg, ["reep"], engine=engine)
    finally:
        metadata.drop_all(engine)


def test_key_must_be_unique(engine: Any, reg: Register) -> None:
    metadata = sa.MetaData()
    name = f"nokey_{uuid.uuid4().hex[:8]}"
    sa.Table(
        name,
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("tm_id", sa.String(32)),
        sa.Column("reep_id", sa.String(16)),
    )
    metadata.create_all(engine)
    try:
        config = dbsync.parse_config(
            {
                "version": 1,
                "run_log": False,
                "tables": {
                    "t": {
                        "table": name,
                        "entity_type": "player",
                        "primary_key": "tm_id",
                        "reep_id": "reep_id",
                        "provider_ids": {"transfermarkt/spieler": "tm_id"},
                    }
                },
            }
        )
        with pytest.raises(ConfigError, match="not the primary key"):
            dbsync.sync(config, reg, ["reep"], engine=engine)
    finally:
        metadata.drop_all(engine)


def test_database_error_hides_the_password(reg: Register) -> None:
    config = dbsync.parse_config(
        {
            "version": 1,
            "run_log": False,
            "database": "postgresql://reep:s3cret-pass@127.0.0.1:9/reep",
            "tables": {
                "t": {
                    "table": "players",
                    "entity_type": "player",
                    "primary_key": "id",
                    "reep_id": "reep_id",
                }
            },
        }
    )
    with pytest.raises(DatabaseError) as info:
        dbsync.sync(config, reg, ["reep"])
    text = str(info.value) + json.dumps(info.value.to_dict())
    assert "s3cret-pass" not in text
    assert "***" in text
    assert info.value.exit_code == 6


def test_rows_are_written_one_by_one_without_multi_rowcount(
    engine: Any, table: Any, reg: Register, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pyodbc does not report row counts for executemany; the sync then runs one
    # UPDATE per row so that its written counts stay exact.
    monkeypatch.setattr(engine.dialect, "supports_sane_multi_rowcount", False)
    report = dbsync.sync(_config(table.name), reg, ["reep"], apply=True, engine=engine)
    [target] = report.targets
    assert target.written == 6
    assert target.status_written == len(ROWS)
