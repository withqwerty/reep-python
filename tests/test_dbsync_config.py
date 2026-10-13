"""The ``reep.yml`` parser."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("yaml")

from reep.dbsync import (
    Config,
    ProviderColumn,
    config_template,
    load_config,
    parse_config,
)
from reep.dbsync._config import require
from reep.errors import ConfigError


def _entry(**changes: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "table": "players",
        "entity_type": "player",
        "primary_key": "id",
        "reep_id": "reep_id",
        "provider_ids": {"transfermarkt/spieler": "tm_id"},
    }
    entry.update(changes)
    return {k: v for k, v in entry.items() if v is not None}


def _config(**changes: Any) -> dict[str, Any]:
    return {"version": 1, "tables": {"players": _entry(**changes)}}


def test_template_parses(tmp_path: Path) -> None:
    path = tmp_path / "reep.yml"
    path.write_text(config_template())
    config = load_config(path)
    assert config.path == path
    assert config.database == "${DATABASE_URL}"
    [table] = config.tables
    assert table.name == "players"
    assert table.primary_key == ("id",)
    assert table.provider_ids == (
        ProviderColumn("transfermarkt", "spieler", "tm_id"),
        ProviderColumn("fbref", "person", "fbref_id"),
    )
    assert table.columns_for("fbref") == [ProviderColumn("fbref", "person", "fbref_id")]
    assert table.columns_for("fbref/other") == []
    assert all(len(line) <= 88 for line in config_template().splitlines())


def test_full_entry() -> None:
    config = parse_config(
        {
            "version": 1,
            "database": "sqlite:///x.db",
            "run_log": "logs/runs.jsonl",
            "tables": {
                "matches": _entry(
                    table="fixtures",
                    schema="football",
                    entity_type="match",
                    primary_key=["season", "match_no"],
                    status="reep_status",
                    provider_ids={"Opta/Match": "opta_id"},
                )
            },
        },
        path=Path("/srv/conf/reep.yml"),
    )
    [table] = config.tables
    assert table.qualified_name == "football.fixtures"
    assert table.primary_key == ("season", "match_no")
    assert table.provider_ids == (ProviderColumn("opta", "match", "opta_id"),)
    assert config.run_log == Path("/srv/conf/logs/runs.jsonl")
    assert config.log_runs is True
    assert "sqlite" not in repr(config)  # the URL can hold a password


def test_run_log_false() -> None:
    config = parse_config({**_config(), "run_log": False})
    assert config.log_runs is False
    assert config.run_log is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "must be a mapping"),
        ({"tables": {}}, "version must be 1"),
        ({"version": 2, "tables": {}}, "version must be 1"),
        ({"version": 1, "tables": {}}, "has no entries"),
        ({"version": 1, "tables": []}, "tables must be a mapping"),
        ({**_config(), "extra": 1}, "unknown key"),
        ({**_config(), "database": 5}, "database must be a URL"),
        ({**_config(), "run_log": 3}, "run_log must be"),
        (_config(colour="red"), "unknown key"),
        (_config(table=None), "table is required"),
        (_config(entity_type="manager"), "entity_type must be one of"),
        (_config(primary_key=None), "primary_key is required"),
        (_config(primary_key=["id", "id"]), "distinct columns"),
        (_config(primary_key=True), "must be a column or table name"),
        (_config(reep_id=None, provider_ids=None), "needs reep_id, provider_ids"),
        (_config(reep_id=None, status="s"), "status needs reep_id"),
        (
            _config(provider_ids={"transfermarkt": "tm_id"}),
            "each key must be provider/namespace",
        ),
        (
            _config(provider_ids={"a/b": "x", "c/d": "x"}),
            "names a column more than once",
        ),
        (_config(reep_id="id"), "must not have another role"),
        (_config(status="tm_id"), "must not have another role"),
        (_config(status="reep_id"), "must be different columns"),
        (_config(provider_ids={"a/b": None}), "is required"),
        (_config(provider_ids=["a/b"]), "must be a mapping"),
    ],
)
def test_invalid_configs(data: Any, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_database_url_expands_variables() -> None:
    config = parse_config({**_config(), "database": "postgresql://u:${PW}@${HOST}/db"})
    url = config.database_url({"PW": "secret", "HOST": "db.local"})
    assert url == "postgresql://u:secret@db.local/db"
    with pytest.raises(ConfigError, match="HOST, which is not set") as info:
        config.database_url({"PW": "secret"})
    assert "secret" not in str(info.value)


def test_database_url_from_the_environment() -> None:
    config = parse_config(_config())
    assert config.database_url({"DATABASE_URL": "sqlite://"}) == "sqlite://"
    assert (
        config.database_url({"DATABASE_URL": "a://", "REEP_DATABASE_URL": "b://"})
        == "b://"
    )
    with pytest.raises(ConfigError, match="No database URL"):
        config.database_url({})


def test_select() -> None:
    config = Config(
        tables=(
            parse_config(_config()).tables[0],
            parse_config(
                {"version": 1, "tables": {"teams": _entry(entity_type="team")}}
            ).tables[0],
        )
    )
    assert [t.name for t in config.select()] == ["players", "teams"]
    assert [t.name for t in config.select(["teams"])] == ["teams"]
    assert [t.name for t in config.select(entity_type="team")] == ["teams"]
    with pytest.raises(ConfigError, match="no table entry 'coaches'"):
        config.select(["coaches"])
    with pytest.raises(ConfigError, match="No table entry matches"):
        config.select(["players"], entity_type="team")


def test_load_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="No config file") as info:
        load_config(tmp_path / "missing.yml")
    assert "reep init" in (info.value.hint or "")
    bad = tmp_path / "bad.yml"
    bad.write_text("tables: [unclosed")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad)


def test_yaml_booleans_need_quotes(tmp_path: Path) -> None:
    path = tmp_path / "reep.yml"
    path.write_text(
        "version: 1\ntables:\n  t:\n    table: t\n    entity_type: player\n"
        "    primary_key: on\n    reep_id: reep_id\n"
    )
    with pytest.raises(ConfigError, match="in quotes") as info:
        load_config(path)
    assert "quotes" in (info.value.hint or "")


def test_missing_dependency_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A None entry in sys.modules makes the import fail, as if not installed.
    monkeypatch.setitem(sys.modules, "sqlalchemy", None)
    with pytest.raises(ConfigError, match="needs the 'sqlalchemy' package") as info:
        require("sqlalchemy")
    assert "reep[db]" in (info.value.hint or "")


def test_a_yaml_error_does_not_print_the_source_line(tmp_path: Path) -> None:
    # PyYAML quotes the line it could not parse; that line can hold a password.
    path = tmp_path / "reep.yml"
    path.write_text('version: 1\ndatabase: "postgresql://u:s3cr@db/x\ntables: {}\n')
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert "s3cr" not in str(info.value)
    assert "line" in info.value.message


def test_a_yaml_tag_error_does_not_echo_the_url(tmp_path: Path) -> None:
    # PyYAML puts an unknown tag, here the whole URL, into its message.
    path = tmp_path / "reep.yml"
    path.write_text("version: 1\ndatabase: !postgresql://u:s3cr@db/x\n")
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert "s3cr" not in str(info.value)
    assert "line 2" in info.value.message


def test_a_config_error_does_not_quote_a_url_password(tmp_path: Path) -> None:
    # A URL in the wrong place is quoted by the validation message.
    path = tmp_path / "reep.yml"
    path.write_text('version: {database: "postgresql://u:s3cr@db/x"}\ntables: {}\n')
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert "s3cr" not in str(info.value)
    assert "postgresql://" not in str(info.value)
