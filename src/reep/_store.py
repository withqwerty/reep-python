"""Build and inspect local DuckDB stores.

The SDK store uses the layout of the DuckDB file that ships with each release: one
table per CSV file, named after the file, with every column as VARCHAR, plus a
``release_metadata`` key-value table and an ``entity_search`` table. One query path
therefore reads both kinds of file.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import duckdb

from reep._version import __version__
from reep.errors import UnsupportedReleaseError
from reep.models import WITHDRAWN_REASONS

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Tables in the slim profile.
SLIM_TABLES: tuple[str, ...] = ("bridges", "entities", "redirects")

#: The layout version of SDK-built stores.
STORE_VERSION = "1"

#: Redirect reasons that this version of reep knows.
REDIRECT_REASONS = frozenset({"merged", *WITHDRAWN_REASONS})
#: Entity statuses that this version of reep knows.
ENTITY_STATUSES = frozenset({"active", "redirected", "retired"})

#: Columns that the SDK reads. A release without them is not supported.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "bridges": ("provider", "namespace", "external_id", "reep_id"),
    "entities": ("reep_id", "entity_type", "status", "label"),
    "redirects": ("from_id", "to_id", "reason"),
    "aliases": ("reep_id", "alias"),
    "overlay_aliases": ("reep_id", "name"),
}

#: Columns that older releases do not have; the SDK adds them as NULL.
OPTIONAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "bridges": ("rung", "upstream_status"),
    "entities": (
        "gender",
        "country",
        "corroboration_grade",
        "corroboration_count",
        "source_tier_band",
    ),
}

# The CSV dialect of the release files (Python csv.writer output).
_CSV_OPTIONS = "header = true, all_varchar = true, quote = '\"', escape = '\"'"

# release_metadata keys.
META_STAMP = "source_stamp"
META_SCHEMA = "schema_version"
META_TIER = "projection_mode"
META_GENERATED_AT = "generated_at"
META_SDK_VERSION = "sdk_version"
META_STORE_VERSION = "sdk_store_version"
META_PROFILE = "sdk_profile"
META_SOURCE = "sdk_source"
META_BUILT_AT = "sdk_built_at"
META_BRIDGE_ATTRS = "sdk_bridge_attributes_stamp"
META_IMPORT_FINGERPRINT = "sdk_import_fingerprint"
META_REPLAYED_FROM = "sdk_replayed_from"
META_REPLAY_STEPS = "sdk_replay_steps"


def connect(path: Path, *, read_only: bool) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB file.

    The connection uses DuckDB's default settings, so that code in the same process
    can open the same file with ``duckdb.connect``. The SDK's queries use no DuckDB
    extensions.

    Args:
        path: The DuckDB file.
        read_only: Open the file read-only.

    Returns:
        The connection.
    """
    return duckdb.connect(str(path), read_only=read_only)


@dataclass(frozen=True, slots=True)
class StoreSpec:
    """What to build into a store.

    Attributes:
        tables: The CSV file of each table.
        stamp: The release stamp, or ``None`` when unknown.
        schema_version: The release schema version.
        profile: ``"slim"`` or ``"full"``.
        source: ``"download"`` or ``"import"``.
        generated_at: When the release was generated.
        tier: The publication tier.
        expected_rows: The row count of each table from the manifest.
        extra_meta: More ``release_metadata`` keys to write.
    """

    tables: Mapping[str, Path]
    stamp: str | None
    schema_version: str | None
    profile: str
    source: str
    generated_at: str | None = None
    tier: str | None = None
    expected_rows: Mapping[str, int] = field(default_factory=dict[str, int])
    extra_meta: Mapping[str, str] = field(default_factory=dict[str, str])


def build_store(spec: StoreSpec, dest: Path) -> None:
    """Build a store from CSV files, then move it into place.

    The build writes a temporary file next to ``dest`` and renames it over
    ``dest`` only after every check passes, so a failed build leaves the old store
    unchanged.

    Args:
        spec: What to build.
        dest: The store path.

    Raises:
        UnsupportedReleaseError: If a table lacks a required column, has an
            unknown redirect reason or status, or has the wrong row count.
    """
    for table in SLIM_TABLES:
        if table not in spec.tables:
            raise UnsupportedReleaseError(f"The release has no {table} table.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
    try:
        con = connect(tmp, read_only=False)
        try:
            con.execute(f"SET temp_directory = '{_sql_text(str(dest.parent))}'")
            for table, path in spec.tables.items():
                logger.info("Loading %s from %s", table, path.name)
                load_table(con, table, path)
            check_store(con, spec.expected_rows)
            build_entity_search(con)
            write_metadata(
                con,
                {
                    META_STAMP: spec.stamp,
                    META_SCHEMA: spec.schema_version,
                    META_TIER: spec.tier,
                    META_GENERATED_AT: spec.generated_at,
                    META_SDK_VERSION: __version__,
                    META_STORE_VERSION: STORE_VERSION,
                    META_PROFILE: spec.profile,
                    META_SOURCE: spec.source,
                    META_BUILT_AT: now_utc(),
                    META_BRIDGE_ATTRS: spec.stamp,
                    **spec.extra_meta,
                },
            )
            con.execute("CHECKPOINT")
        finally:
            con.close()
        replace_file(tmp, dest)
    finally:
        remove_db_files(tmp)


def load_table(con: duckdb.DuckDBPyConnection, table: str, path: Path) -> None:
    """Load one gzip CSV file into a VARCHAR table.

    Args:
        con: An open writable connection.
        table: The table name.
        path: The CSV file.

    Raises:
        UnsupportedReleaseError: If a required column is missing.
    """
    _check_identifier(table)
    header = con.execute(
        f"DESCRIBE SELECT * FROM read_csv(?, {_CSV_OPTIONS})", [str(path)]
    ).fetchall()
    columns = [str(row[0]) for row in header]
    missing = [c for c in REQUIRED_COLUMNS.get(table, ()) if c not in columns]
    if missing:
        raise UnsupportedReleaseError(
            f"{path.name} lacks the column(s) {', '.join(missing)}.",
            hint="Upgrade the reep package, or check that the file is a Reep release "
            "table.",
        )
    # Sorting the bridges by the lookup key lets DuckDB skip row groups.
    order = " ORDER BY provider, namespace, external_id" if table == "bridges" else ""
    con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(
        f"CREATE TABLE {table} AS SELECT * FROM read_csv(?, {_CSV_OPTIONS}){order}",
        [str(path)],
    )
    for column in OPTIONAL_COLUMNS.get(table, ()):
        if column not in columns:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR")


def check_store(
    con: duckdb.DuckDBPyConnection, expected_rows: Mapping[str, int]
) -> None:
    """Check row counts and the closed vocabularies that the SDK relies on.

    Args:
        con: An open connection.
        expected_rows: The row count of each table from the manifest.

    Raises:
        UnsupportedReleaseError: If a check fails.
    """
    tables = table_names(con)
    for table, expected in expected_rows.items():
        if table not in tables:
            continue
        # The table name comes from the database's own catalogue.
        actual = scalar_int(con, f"SELECT count(*) FROM {table}")
        if actual != expected:
            raise UnsupportedReleaseError(
                f"{table} has {actual} rows, but the manifest lists {expected}.",
                hint="The file may be damaged. Delete it and download it again.",
            )
    reasons = {
        str(row[0])
        for row in con.execute(
            "SELECT DISTINCT reason FROM redirects WHERE reason IS NOT NULL"
        ).fetchall()
    }
    unknown = sorted(reasons - REDIRECT_REASONS)
    if unknown:
        raise UnsupportedReleaseError(
            f"redirects has unknown reason(s): {', '.join(unknown)}.",
            hint="Upgrade the reep package to a version that handles these reasons.",
        )
    statuses = {
        str(row[0])
        for row in con.execute(
            "SELECT DISTINCT status FROM entities WHERE status IS NOT NULL"
        ).fetchall()
    }
    unknown = sorted(statuses - ENTITY_STATUSES)
    if unknown:
        raise UnsupportedReleaseError(
            f"entities has unknown status(es): {', '.join(unknown)}.",
            hint="Upgrade the reep package to a version that handles these statuses.",
        )


def build_entity_search(con: duckdb.DuckDBPyConnection) -> None:
    """Build the ``entity_search`` table from labels, aliases and bridge counts.

    The table matches the one in the shipped release DuckDB file, without the
    full-text index.

    Args:
        con: An open writable connection.
    """
    tables = table_names(con)
    names: list[str] = []
    if "aliases" in tables:
        names.append("SELECT reep_id, alias AS name FROM aliases")
    if "overlay_aliases" in tables:
        names.append("SELECT reep_id, name FROM overlay_aliases")
    alias_sql = (
        " UNION ".join(names)
        if names
        else "SELECT NULL::VARCHAR AS reep_id, NULL::VARCHAR AS name WHERE false"
    )
    con.execute("DROP TABLE IF EXISTS entity_search")
    con.execute(
        "CREATE TABLE entity_search AS "
        f"WITH names AS ({alias_sql}), "
        "name_text AS (SELECT reep_id, string_agg(DISTINCT name, ' ') AS names, "
        "count(DISTINCT name) AS alias_count FROM names GROUP BY reep_id), "
        "bridge_counts AS (SELECT reep_id, count(*) AS bridge_count FROM bridges "
        "GROUP BY reep_id) "
        "SELECT e.reep_id, e.entity_type, e.label, "
        "trim(concat_ws(' ', e.label, n.names)) AS search_text, "
        "coalesce(b.bridge_count, 0)::BIGINT AS bridge_count, "
        "coalesce(n.alias_count, 0)::BIGINT AS alias_count "
        "FROM entities e "
        "LEFT JOIN name_text n USING (reep_id) "
        "LEFT JOIN bridge_counts b USING (reep_id)"
    )


def write_metadata(
    con: duckdb.DuckDBPyConnection, values: Mapping[str, str | None]
) -> None:
    """Write or update keys in ``release_metadata``.

    Args:
        con: An open writable connection.
        values: The keys and values; ``None`` values are not written.
    """
    con.execute(
        "CREATE TABLE IF NOT EXISTS release_metadata (key VARCHAR, value VARCHAR)"
    )
    for key, value in values.items():
        con.execute("DELETE FROM release_metadata WHERE key = ?", [key])
        if value is not None:
            con.execute("INSERT INTO release_metadata VALUES (?, ?)", [key, value])


def read_metadata(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Return ``release_metadata`` as a dictionary, or an empty one.

    Args:
        con: An open connection.

    Returns:
        The keys and values.
    """
    if "release_metadata" not in table_names(con):
        return {}
    rows = con.execute("SELECT key, value FROM release_metadata").fetchall()
    return {str(k): str(v) for k, v in rows if k is not None and v is not None}


def table_names(con: duckdb.DuckDBPyConnection) -> set[str]:
    """Return the names of the tables and views in the main schema.

    Args:
        con: An open connection.

    Returns:
        The names.
    """
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_catalog = current_database()"
    ).fetchall()
    return {str(row[0]) for row in rows}


def table_columns(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    """Return the column names of a table.

    Args:
        con: An open connection.
        table: The table name.

    Returns:
        The column names in order.
    """
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_catalog = current_database() "
        "AND table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()
    return [str(row[0]) for row in rows]


def scalar_int(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    """Run a query that returns one integer.

    Args:
        con: An open connection.
        sql: The query.

    Returns:
        The integer.
    """
    row = con.execute(sql).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def now_utc() -> str:
    """Return the current UTC time in ISO 8601 form."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def replace_file(src: Path, dest: Path) -> None:
    """Move ``src`` over ``dest`` in one step.

    Args:
        src: The new file.
        dest: The file to replace.

    Raises:
        OSError: If the file cannot be replaced, for example on Windows while
            another process has ``dest`` open.
    """
    src.replace(dest)


def remove_db_files(path: Path) -> None:
    """Remove a DuckDB file and its write-ahead log, if they exist.

    Args:
        path: The DuckDB file.
    """
    for candidate in (path, path.with_name(path.name + ".wal")):
        candidate.unlink(missing_ok=True)


def copy_store(src: Path, dest: Path) -> None:
    """Copy a store file with all its data checkpointed.

    Args:
        src: The store to copy.
        dest: The new file.
    """
    shutil.copyfile(src, dest)


def _check_identifier(name: str) -> None:
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        msg = f"{name!r} is not a valid table name."
        raise UnsupportedReleaseError(msg)


def _sql_text(value: str) -> str:
    return value.replace("'", "''")


def known_tables(names: Iterable[str]) -> list[str]:
    """Return the names that are safe table identifiers, sorted.

    Args:
        names: Candidate table names.

    Returns:
        The valid names.
    """
    out = []
    for name in names:
        try:
            _check_identifier(name)
        except UnsupportedReleaseError:
            continue
        out.append(name)
    return sorted(out)
