"""Secrets and write safety through public database sync entry points."""

from __future__ import annotations

import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import duckdb
import pytest
import sqlalchemy as sa
from click.testing import CliRunner

from reep import Register, catalog, dbsync
from reep.cli.app import main
from reep.dbsync._database import database_errors, make_engine, masked_url
from reep.errors import ConfigError, DatabaseError
from tests.conftest import make_settings
from tests.support.fixture_release import PALMER, STAMP_1, state_1, write_release


@pytest.fixture
def release_folder(tmp_path: Path) -> Path:
    write_release(tmp_path / "bucket", STAMP_1, state_1())
    return tmp_path / "bucket" / "releases" / STAMP_1


@pytest.fixture
def local(release_folder: Path) -> Iterator[Register]:
    with Register.local(release_folder / "duckdb" / "reep-register-v1.duckdb") as reg:
        yield reg


def _config(**changes: Any) -> dbsync.Config:
    entry = {
        "table": "players",
        "entity_type": "player",
        "primary_key": "id",
        "reep_id": "reep_id",
        "status": "status",
        "provider_ids": {
            "transfermarkt/spieler": "tm_id",
            "fbref/person": "fbref_id",
        },
    }
    entry.update(changes)
    return dbsync.parse_config({"version": 1, "run_log": False, "tables": {"p": entry}})


@pytest.mark.parametrize("stored", ["", "   "])
@pytest.mark.parametrize("multi", [True, False])
def test_blank_stored_id_blocks_provider_write(
    tmp_path: Path, local: Register, stored: str, multi: bool
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'rows.db'}")
    engine.dialect.supports_sane_multi_rowcount = multi
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE players (id INTEGER PRIMARY KEY, tm_id TEXT, "
                "reep_id TEXT, fbref_id TEXT, status TEXT)"
            )
            conn.exec_driver_sql(
                "INSERT INTO players VALUES (1, '568177', ?, NULL, NULL)", (stored,)
            )
        report = dbsync.sync(
            _config(), local, ["reep", "fbref"], apply=True, engine=engine
        )
        assert report.targets[1].outcomes == {"conflict": 1}
        assert report.targets[1].written == 0
        assert report.targets[0].outcomes == {"invalid_value": 1}
        with engine.connect() as conn:
            assert tuple(
                conn.exec_driver_sql(
                    "SELECT reep_id, fbref_id, status FROM players"
                ).one()
            ) == (stored, None, "invalid_value")
    finally:
        engine.dispose()


@pytest.mark.parametrize("multi", [True, False])
def test_missing_release_metadata_blocks_provider_write(
    tmp_path: Path, release_folder: Path, multi: bool
) -> None:
    path = release_folder / "duckdb" / "reep-register-v1.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("DELETE FROM release_metadata")
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'rows.db'}")
    engine.dialect.supports_sane_multi_rowcount = multi
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE players (id INTEGER PRIMARY KEY, tm_id TEXT, "
                "reep_id TEXT, fbref_id TEXT, status TEXT)"
            )
            conn.exec_driver_sql(
                "INSERT INTO players VALUES (1, NULL, ?, NULL, NULL)", (PALMER,)
            )
        with (
            Register.local(path) as reg,
            pytest.raises(ConfigError, match="current statuses"),
        ):
            dbsync.sync(_config(), reg, ["fbref"], apply=True, engine=engine)
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT fbref_id FROM players").scalar() is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("source", ["shipped", "import", "csv"])
def test_manifest_backed_sources_have_current_provenance(
    tmp_path: Path, release_folder: Path, source: str
) -> None:
    settings = make_settings(tmp_path / "cache")
    if source == "shipped":
        reg = Register.local(release_folder / "duckdb" / "reep-register-v1.duckdb")
    elif source == "csv":
        reg = Register.local(csv_dir=release_folder, settings=settings)
    else:
        catalog.import_release(release_folder, settings=settings)
        reg = Register.local(settings=settings)
    with reg:
        assert reg.release.stamp == reg.release.bridge_attributes_stamp == STAMP_1
        from reep.dbsync._plan import Target
        from reep.dbsync._run import _check_bridge_attributes

        pc = _config().tables[0].provider_ids[1]
        _check_bridge_attributes(reg, {"p": [Target(pc.column, pc)]})


@pytest.mark.parametrize(
    ("schema", "table"), [(None, "players"), ("main", "players"), ("main", "PLAYERS")]
)
def test_duplicate_physical_table_entries_are_refused(
    tmp_path: Path, local: Register, schema: str | None, table: str
) -> None:
    first = _config(reep_id="reep_a", status=None).tables[0]
    second = _config(reep_id="reep_b", status=None, schema=schema, table=table).tables[
        0
    ]
    from dataclasses import replace

    config = dbsync.Config(
        tables=(first, replace(second, name="other")), log_runs=False
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'rows.db'}")
    updates: list[str] = []

    def capture(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("UPDATE"):
            updates.append(statement)

    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE players (id INTEGER PRIMARY KEY, tm_id TEXT, "
                "reep_a TEXT, reep_b TEXT, fbref_id TEXT)"
            )
            conn.exec_driver_sql(
                "INSERT INTO players VALUES (1, '568177', NULL, NULL, NULL)"
            )
        with pytest.raises(ConfigError, match="same physical table"):
            dbsync.sync(config, local, apply=True, engine=engine)
        assert updates == []
        with engine.connect() as conn:
            assert tuple(
                conn.exec_driver_sql("SELECT reep_a, reep_b FROM players").one()
            ) == (None, None)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        (
            "postgresql://user:xy@db:5432/x?sslmode=qq&client_secret=zz",
            "postgresql://db:5432/x",
        ),
        (
            "mssql+pyodbc:///?odbc_connect=" + quote_plus("UID=u;PWD={odbc}}secret};"),
            "mssql+pyodbc:///",
        ),
        ("sqlite:///path/to/file.db?timeout=xy", "sqlite:///path/to/file.db"),
        ("postgresql://u:xy@[::1]:5432/x?password=qq", "postgresql://[::1]:5432/x"),
        ("postgresql://db:xy/x", "(a URL that is not valid)"),
    ],
)
def test_shown_url_contains_only_location(url: str, shown: str) -> None:
    assert masked_url(url) == shown


@pytest.mark.parametrize("parameter", ["timeout", "uri"])
def test_engine_coercion_is_a_safe_config_error(parameter: str) -> None:
    url = f"sqlite:///?{parameter}=xy"
    with pytest.raises(ConfigError) as info:
        make_engine(url)
    assert "xy" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__
    assert "xy" not in "".join(traceback.format_exception(info.value))


@pytest.mark.parametrize("parameter", ["timeout", "uri"])
@pytest.mark.parametrize("mode", ["--json", "--jsonl", "human"])
def test_engine_coercion_is_safe_through_cli(
    tmp_path: Path,
    release_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    parameter: str,
    mode: str,
) -> None:
    monkeypatch.setenv(
        "REEP_DUCKDB_PATH", str(release_folder / "duckdb" / "reep-register-v1.duckdb")
    )
    config = tmp_path / "reep.yml"
    config.write_text(
        "version: 1\nrun_log: false\ndatabase: sqlite:///?" + parameter + "=q-secret\n"
        "tables:\n  p:\n    table: players\n    entity_type: player\n"
        "    primary_key: id\n    reep_id: reep_id\n"
    )
    with CliRunner().isolation() as streams:
        code = main(
            [
                "sync",
                "reep",
                "--config",
                str(config),
                *([] if mode == "human" else [mode]),
            ]
        )
        output = streams[2].getvalue().decode()
    assert code == 2
    assert "q-secret" not in output
    assert "config_error" in output if mode != "human" else "Check" in output


@pytest.mark.parametrize(
    "diagnostic", ["password=xy token=qq", "password=two  words", "PWD=odbc}secret"]
)
@pytest.mark.parametrize("exception", ["dbapi", "sqlalchemy", "coercion"])
def test_database_errors_never_include_diagnostics(
    diagnostic: str, exception: str
) -> None:
    error: Exception = RuntimeError(diagnostic)
    if exception == "dbapi":
        error = sa.exc.OperationalError(None, None, error)
    elif exception == "sqlalchemy":
        error = sa.exc.ArgumentError(diagnostic)
    else:
        error = ValueError(diagnostic)
    with (
        pytest.raises(DatabaseError) as info,
        database_errors("postgresql://db/x", "Connecting"),
    ):
        raise error
    assert diagnostic not in str(info.value)
    assert diagnostic not in "".join(traceback.format_exception(info.value))
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__
    assert "Check the database URL" in (info.value.hint or "")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:xy@db/x?client_secret=qq",
        "mssql+pyodbc:///?odbc_connect=" + quote_plus("UID=u;PWD=odbc-secret;"),
    ],
)
@pytest.mark.parametrize(
    "field", ["version", "unknown", "entry", "entity_type", "provider", "column"]
)
def test_config_errors_do_not_quote_configured_values(
    tmp_path: Path, url: str, field: str
) -> None:
    entry: dict[str, Any] = {
        "table": "players",
        "entity_type": "player",
        "primary_key": "id",
        "reep_id": "reep_id",
    }
    data: dict[str, Any] = {"version": 1, "tables": {"p": entry}}
    if field == "version":
        data["version"] = {"database": url}
    elif field == "unknown":
        data[url] = True
    elif field == "entry":
        data["tables"] = {url: []}
    elif field == "entity_type":
        entry["entity_type"] = url
    elif field == "provider":
        entry["provider_ids"] = {url: "tm_id"}
    else:
        entry["primary_key"] = {"database": url}
    with pytest.raises(ConfigError) as info:
        dbsync.parse_config(data)
    assert url not in str(info.value)
    assert "xy" not in str(info.value)
    assert "qq" not in str(info.value)
    assert "odbc-secret" not in str(info.value)
    import yaml

    path = tmp_path / "reep.yml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError) as info:
        dbsync.load_config(path)
    assert "qq" not in str(info.value)
    assert "odbc-secret" not in str(info.value)
    with CliRunner().isolation() as streams:
        code = main(["sync", "reep", "--config", str(path), "--json"])
        output = streams[2].getvalue().decode()
    assert code == 2
    assert "qq" not in output
    assert "odbc-secret" not in output


@pytest.mark.parametrize("attribute", ["sqlstate", "pgcode", "numeric"])
def test_database_error_uses_only_a_structured_code(attribute: str) -> None:
    class DriverError(Exception):
        pass

    driver = DriverError("password=xy")
    expected = "DriverError (28P01)"
    if attribute == "numeric":
        driver.args = (1045, "password=xy")
        expected = "DriverError (1045)"
    else:
        setattr(driver, attribute, "28P01")
    with (
        pytest.raises(DatabaseError) as info,
        database_errors("postgresql://db/x", "Connecting"),
    ):
        raise sa.exc.OperationalError(None, None, driver)
    assert info.value.message == f"Connecting failed on postgresql://db/x: {expected}"
    assert info.value.__cause__ is None


@pytest.mark.parametrize("error", [ImportError, ValueError, RuntimeError])
def test_all_engine_construction_errors_are_safe(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def failed(*args: Any, **kwargs: Any) -> Any:
        raise error("password=xy token=qq")

    monkeypatch.setattr(sa, "create_engine", failed)
    with pytest.raises(ConfigError) as info:
        make_engine("sqlite:///?timeout=1")
    assert error.__name__ in info.value.message
    assert "xy" not in str(info.value)
    assert "qq" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__
