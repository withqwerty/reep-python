"""Database access for the sync: the engine, table checks, reads and guarded writes.

Every statement comes from SQLAlchemy Core with reflected ``Column`` objects, so
names are quoted for each dialect and every value is a bound parameter.
"""

from __future__ import annotations

import importlib.util
import re
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc

from reep.dbsync._plan import ColumnKind, Row, read_cell
from reep.errors import ConfigError, DatabaseError, ReepError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from reep.dbsync._config import TableConfig

__all__ = [
    "RowUpdate",
    "TableHandle",
    "apply_writes",
    "database_errors",
    "make_engine",
    "masked_url",
    "read_rows",
    "reflect",
]

#: Reep ID columns and status columns need at least this many characters.
MIN_TEXT_LENGTH = 16
_WRITE_CHUNK = 1000

# The driver that each reep extra installs, used when the dialect's default driver
# is missing: reep[mysql] installs PyMySQL, reep[mssql] installs pymssql.
_FALLBACK_DRIVERS = {
    "postgresql": ("psycopg", "psycopg2", "psycopg2"),
    "mysql": ("MySQLdb", "pymysql", "pymysql"),
    "mariadb": ("MySQLdb", "pymysql", "pymysql"),
    "mssql": ("pyodbc", "pymssql", "pymssql"),
}
_EXTRA_FOR = {
    "postgresql": "reep[postgres]",
    "mysql": "reep[mysql]",
    "mariadb": "reep[mysql]",
    "mssql": "reep[mssql]",
}


def masked_url(url: sa.URL | str) -> str:
    """Return only the driver, host, port and database of a URL.

    Args:
        url: The URL.

    Returns:
        The location, without credentials or query parameters. SQLite keeps its
        path. An invalid URL returns a fixed description.
    """
    try:
        parsed = sa.make_url(url) if isinstance(url, str) else url
        location = sa.URL.create(
            drivername=parsed.drivername,
            host=parsed.host,
            port=parsed.port,
            database=parsed.database,
        )
        if location.host is None and location.database is None:
            return f"{location.drivername}:///"
        return location.render_as_string(hide_password=False)
    except Exception:
        return "(a URL that is not valid)"


def _with_driver(url: sa.URL) -> sa.URL:
    backend = url.get_backend_name()
    if backend == "postgres":
        url = url.set(drivername="postgresql")
        backend = "postgresql"
    if "+" in url.drivername or backend not in _FALLBACK_DRIVERS:
        return url
    default, fallback, driver = _FALLBACK_DRIVERS[backend]
    if importlib.util.find_spec(default) is None and importlib.util.find_spec(fallback):
        return url.set(drivername=f"{backend}+{driver}")
    return url


def make_engine(url: str) -> tuple[sa.Engine, str]:
    """Create an engine for a database URL.

    A URL without a driver (``mysql://``, ``mssql://``) uses the driver that the
    reep extra installs when the dialect's default driver is missing.

    Args:
        url: The SQLAlchemy URL.

    Returns:
        The engine and the URL location, without credentials or query parameters.

    Raises:
        ConfigError: If parsing the URL or creating the engine fails.
    """
    extra = "the driver for your database"
    try:
        parsed = _with_driver(sa.make_url(url))
        extra = _EXTRA_FOR.get(parsed.get_backend_name(), extra)
        engine = sa.create_engine(parsed)
    except Exception as exc:
        raise ConfigError(
            f"Creating the database engine for {masked_url(url)} failed: "
            f"{type(exc).__name__}.",
            hint="Check the database URL, the credentials and that the database is "
            f"running. Install {extra} if the driver is missing.",
        ) from None
    return engine, masked_url(parsed)


def _error_code(exc: Exception) -> str | None:
    """Return a short code from structured driver attributes, never diagnostics."""
    for attribute in ("sqlstate", "pgcode"):
        code = getattr(exc, attribute, None)
        if isinstance(code, str) and re.fullmatch(r"[0-9A-Z]{5}", code):
            return code
    code = getattr(exc, "sqlite_errorcode", None)
    if code is None and exc.args:
        code = exc.args[0]
    if type(code) is int and 0 <= code <= 999999:
        return str(code)
    return None


@contextmanager
def database_errors(shown: str, action: str) -> Iterator[None]:
    """Turn database and connection errors into safe :class:`DatabaseError`.

    Args:
        shown: The URL location, without credentials or query parameters.
        action: What the code was doing, for the message.

    Yields:
        Nothing.

    Raises:
        DatabaseError: If database access fails. Only the exception class and a
            structured driver code appear; the original traceback is suppressed.
    """
    try:
        yield
    except ReepError:
        raise
    except Exception as exc:
        driver = exc.orig if isinstance(exc, sa_exc.DBAPIError) else exc
        detail = type(driver).__name__
        if isinstance(driver, Exception) and (code := _error_code(driver)):
            detail += f" ({code})"
        raise DatabaseError(
            f"{action} failed on {shown}: {detail}",
            hint="Check the database URL, the credentials and that the database is "
            "running.",
        ) from None


@dataclass(frozen=True, slots=True)
class TableHandle:
    """A reflected table and what its columns can hold.

    Attributes:
        config: The table entry.
        table: The reflected table.
        columns: The actual column name for each configured name.
        kinds: What each provider column can hold, by configured name.
    """

    config: TableConfig
    table: sa.Table
    columns: dict[str, str]
    kinds: dict[str, ColumnKind]

    def column(self, name: str) -> sa.Column[Any]:
        """Return the reflected column for a configured column name."""
        return self.table.c[self.columns[name]]


@dataclass(frozen=True, slots=True)
class RowUpdate:
    """A guarded update of one row.

    Attributes:
        pk: The primary key values of the row.
        values: The new value of each cell to write, by configured column name.
        guard: The value that each column the run read must still hold, by
            configured column name; ``None`` means NULL.
    """

    pk: tuple[Any, ...]
    values: dict[str, Any]
    guard: dict[str, Any]


def column_kind(type_: sa.types.TypeEngine[Any]) -> ColumnKind | None:
    """Return what a column type can hold, or ``None`` if it cannot hold IDs.

    Args:
        type_: The reflected column type.

    Returns:
        The kind, or ``None`` for other types (floats, decimals, dates, booleans).
    """
    if isinstance(type_, sa.Boolean):
        return None
    if isinstance(type_, sa.SmallInteger):
        return ColumnKind("integer", max_int=2**15 - 1)
    if isinstance(type_, sa.BigInteger):
        return ColumnKind("integer", max_int=2**63 - 1)
    if isinstance(type_, sa.Integer):
        return ColumnKind("integer", max_int=2**31 - 1)
    if isinstance(type_, sa.Uuid):
        return ColumnKind("uuid")
    if isinstance(type_, sa.String) and not isinstance(type_, sa.Enum):
        return ColumnKind("text", length=type_.length)
    return None


def reflect(engine: sa.Engine, config: TableConfig, shown: str) -> TableHandle:
    """Reflect a configured table and check its columns.

    Args:
        engine: The engine.
        config: The table entry.
        shown: The URL location, without credentials or query parameters.

    Returns:
        The handle.

    Raises:
        ConfigError: If the table or a column is missing, a column cannot hold its
            values, or the primary key is not a unique key of the table.
        DatabaseError: If the database cannot be read.
    """
    where = f"reep.yml table entry {config.name!r}"
    with database_errors(
        shown,
        f"Reading the structure of {config.qualified_name}",
    ):
        inspector = sa.inspect(engine)
        if not inspector.has_table(config.table, schema=config.schema):
            names = sorted(inspector.get_table_names(schema=config.schema))
            raise ConfigError(
                f"{where}: the database has no table {config.qualified_name}.",
                hint=f"Tables: {', '.join(names[:30]) or '(none)'}.",
            )
        names = inspector.get_table_names(schema=config.schema)
        actual_name = config.table
        if actual_name not in names:
            folded = [name for name in names if name.lower() == actual_name.lower()]
            if len(folded) == 1:
                # has_table succeeded: the database recognises this spelling.
                actual_name = folded[0]
        table = sa.Table(
            actual_name, sa.MetaData(), schema=config.schema, autoload_with=engine
        )
        pk_names = list(
            inspector.get_pk_constraint(actual_name, schema=config.schema).get(
                "constrained_columns"
            )
            or []
        )
        unique_sets = _unique_constraints(inspector, table)
        unique_sets += [
            # _table_wide_unique refuses an index with a column name of None.
            [c for c in i.get("column_names") or [] if c is not None]
            for i in inspector.get_indexes(actual_name, schema=config.schema)
            if _table_wide_unique(i)
        ]
    actual = [c.name for c in table.columns]
    configured = [
        *config.primary_key,
        *(c for c in (config.reep_id, config.status) if c is not None),
        *(pc.column for pc in config.provider_ids),
    ]
    columns = {name: _find_column(name, actual, where, config) for name in configured}
    _check_distinct_roles(config, columns, where)
    for name in (config.reep_id, config.status):
        if name is None:
            continue
        kind = column_kind(table.c[columns[name]].type)
        if kind is None or kind.kind != "text":
            raise ConfigError(
                f"{where}: column {name!r} must be a text column.",
                hint=f"Use VARCHAR({MIN_TEXT_LENGTH}) or longer.",
            )
        if kind.length is not None and kind.length < MIN_TEXT_LENGTH:
            raise ConfigError(
                f"{where}: column {name!r} holds {kind.length} characters; it needs "
                f"{MIN_TEXT_LENGTH} or more.",
                hint=f"Change it to VARCHAR({MIN_TEXT_LENGTH}) or longer.",
            )
    kinds: dict[str, ColumnKind] = {}
    for pc in config.provider_ids:
        col_type = table.c[columns[pc.column]].type
        kind = column_kind(col_type)
        if kind is None:
            raise ConfigError(
                f"{where}: column {pc.column!r} is {col_type}; provider IDs need a "
                "text, integer or UUID column.",
                hint="Floating-point columns change IDs such as 568177 into 568177.0.",
            )
        kinds[pc.column] = kind
    key = {columns[name] for name in config.primary_key}
    if not any(key == set(cols) for cols in [pk_names, *unique_sets] if cols):
        raise ConfigError(
            f"{where}: primary_key {list(config.primary_key)} is not the primary key "
            f"or a unique key of {config.qualified_name}.",
            hint=f"The table's primary key is {pk_names or '(none)'}. The sync updates "
            "one row for each key value, so the key must be unique.",
        )
    return TableHandle(config=config, table=table, columns=columns, kinds=kinds)


def _table_wide_unique(index: Any) -> bool:
    """Return ``True`` for a unique index over plain columns of every row.

    A partial (filtered) index is unique only among the rows its condition selects,
    and an index on an expression is not unique on the columns it names.
    """
    if not index.get("unique"):
        return False
    names = index.get("column_names") or []
    if not names or any(name is None for name in names) or index.get("expressions"):
        return False
    options = index.get("dialect_options") or {}
    # The condition is a SQL clause; its truth value is not defined, so test None.
    return not any(
        key.endswith("_where") and value is not None for key, value in options.items()
    )


def _check_distinct_roles(
    config: TableConfig, columns: dict[str, str], where: str
) -> None:
    """Refuse a config in which two roles name the same column.

    Column names match without regard to case, so ``reep_id`` and ``REEP_ID`` can
    both find one column; the sync would then write a status over a Reep ID. A
    provider ID column may also be the key: the sync never writes a key column,
    because a key is never NULL.
    """
    written: list[tuple[str, str]] = [
        *(
            (f"{role} {name!r}", name)
            for role, name in (("reep_id", config.reep_id), ("status", config.status))
            if name is not None
        ),
        *((f"provider_ids {pc.key!r}", pc.column) for pc in config.provider_ids),
    ]
    owner: dict[str, str] = {}
    for role, name in written:
        actual = columns[name]
        if actual in owner:
            raise ConfigError(
                f"{where}: {role} and {owner[actual]} both name column {actual!r}.",
                hint="Each role needs its own column.",
            )
        owner[actual] = role
    for name in config.primary_key:
        role = owner.get(columns[name], "")
        if role.startswith(("reep_id", "status")):
            raise ConfigError(
                f"{where}: primary_key {name!r} and {role} name the same column.",
                hint="The key must be a column that the sync does not write.",
            )


def _unique_constraints(inspector: sa.Inspector, table: sa.Table) -> list[list[str]]:
    # The SQL Server dialect does not list unique constraints; SQL Server backs each
    # one with a unique index, which get_indexes reports.
    try:
        found = inspector.get_unique_constraints(table.name, schema=table.schema)
    except NotImplementedError:
        return []
    return [list(u.get("column_names") or []) for u in found]


def _find_column(
    name: str, actual: Sequence[str], where: str, config: TableConfig
) -> str:
    if name in actual:
        return name
    folded = [c for c in actual if c.lower() == name.lower()]
    if len(folded) == 1:
        return folded[0]
    raise ConfigError(
        f"{where}: table {config.qualified_name} has no column {name!r}.",
        hint=f"Columns: {', '.join(actual)}.",
    )


def read_rows(
    engine: sa.Engine,
    handle: TableHandle,
    columns: Sequence[str],
    *,
    partition_size: int,
    shown: str,
) -> Iterator[list[Row]]:
    """Read a table's rows in partitions, ordered by the primary key.

    Args:
        engine: The engine.
        handle: The table.
        columns: The configured column names to read; the primary key comes first.
        partition_size: The most rows in one partition.
        shown: The URL location, without credentials or query parameters.

    Yields:
        Lists of rows.

    Raises:
        DatabaseError: If the table cannot be read.
    """
    pk = handle.config.primary_key
    stmt = sa.select(*(handle.column(c) for c in columns)).order_by(
        *(handle.column(c) for c in pk)
    )
    width = len(pk)
    with (
        database_errors(shown, f"Reading {handle.config.qualified_name}"),
        engine.connect() as conn,
    ):
        result = conn.execution_options(yield_per=partition_size).execute(stmt)
        for partition in result.partitions():
            rows = [
                Row(
                    pk=tuple(r[:width]),
                    cells={name: read_cell(r[i]) for i, name in enumerate(columns)},
                )
                for r in partition
            ]
            for row in rows:
                if any(value is None for value in row.pk):
                    raise ConfigError(
                        f"reep.yml table entry {handle.config.name!r}: a row of "
                        f"{handle.config.qualified_name} has NULL in primary_key "
                        f"{list(pk)}, so the sync cannot name it.",
                        hint="Use the table's primary key, or a unique key whose "
                        "columns are NOT NULL.",
                    )
            yield rows


def apply_writes(
    engine: sa.Engine,
    groups: Sequence[tuple[TableHandle, Sequence[RowUpdate]]],
    *,
    shown: str,
) -> list[Counter[str]]:
    """Run guarded row updates in one transaction.

    Each update names one row by its primary key, writes all its planned cells
    together, and changes the row only while every column the run read still holds
    the value the run saw. A row that changed in the meantime is left as it is, so
    no cell gets a value planned from data that is no longer there.

    Args:
        engine: The engine.
        groups: ``(table, updates)`` pairs.
        shown: The URL location, without credentials or query parameters.

    Returns:
        For each table, the number of updated rows that wrote each column, by
        configured column name.

    Raises:
        DatabaseError: If an update fails; the transaction is rolled back.
    """
    counts: list[Counter[str]] = []
    with (
        database_errors(shown, "Writing"),
        engine.begin() as conn,
    ):
        multi = bool(conn.dialect.supports_sane_multi_rowcount)
        for handle, updates in groups:
            written: Counter[str] = Counter()
            # One statement per shape: the columns written and the guards that are
            # NULL. Every row of a shape writes the same columns, so the rowcount
            # counts each of them.
            shapes: dict[
                tuple[tuple[str, ...], tuple[tuple[str, bool], ...]], list[RowUpdate]
            ] = {}
            for update in updates:
                key = (
                    tuple(sorted(update.values)),
                    tuple(sorted((c, v is None) for c, v in update.guard.items())),
                )
                shapes.setdefault(key, []).append(update)
            for (value_cols, guard_shape), rows in shapes.items():
                stmt = _update_statement(
                    handle, value_cols, guard_shape, conn.dialect.name
                )
                params = [_update_params(u, value_cols, guard_shape) for u in rows]
                count = 0
                if multi:
                    for start in range(0, len(params), _WRITE_CHUNK):
                        chunk = params[start : start + _WRITE_CHUNK]
                        count += max(conn.execute(stmt, chunk).rowcount, 0)
                else:
                    for item in params:
                        count += max(conn.execute(stmt, item).rowcount, 0)
                for column in value_cols:
                    written[column] += count
            counts.append(written)
    return counts


def _exact_equal(
    col: sa.Column[Any], param: sa.BindParameter[Any], dialect: str
) -> sa.ColumnElement[bool]:
    """Return ``col = param`` that compares text byte for byte.

    A case-insensitive collation (SQLite NOCASE, MySQL ``*_ci``, SQL Server ``CI``)
    makes ``'alpha' = 'ALPHA'`` true, so a guard on a changed provider ID would
    still pass. SQL Server cannot compare legacy ``text``/``ntext`` with ``=`` at
    all; the cast to ``nvarchar(max)`` makes it comparable.
    """
    if not isinstance(col.type, sa.String):
        return col == param
    if dialect == "sqlite":
        return col.collate("BINARY") == param
    if dialect == "postgresql":
        # citext and domains keep their own '=' under COLLATE; compare as text.
        return sa.cast(col, sa.Text).collate("C") == sa.cast(param, sa.Text)
    if dialect in ("mysql", "mariadb"):
        from sqlalchemy.dialects import mysql

        return sa.cast(col, mysql.BINARY()) == sa.cast(param, mysql.BINARY())
    if dialect == "mssql":
        from sqlalchemy.dialects import mssql

        return sa.cast(col, mssql.NVARCHAR()).collate("Latin1_General_BIN2") == param
    return col == param


def _update_statement(
    handle: TableHandle,
    value_cols: Sequence[str],
    guard_shape: Sequence[tuple[str, bool]],
    dialect: str,
) -> sa.Update:
    # The key can be a provider ID column, so it compares exactly too.
    conditions: list[sa.ColumnElement[bool]] = [
        _exact_equal(handle.column(name), sa.bindparam(f"reep_sync_pk{i}"), dialect)
        for i, name in enumerate(handle.config.primary_key)
    ]
    for i, (name, is_null) in enumerate(guard_shape):
        col = handle.column(name)
        conditions.append(
            col.is_(None)
            if is_null
            else _exact_equal(col, sa.bindparam(f"reep_sync_old{i}"), dialect)
        )
    return (
        sa.update(handle.table)
        .where(*conditions)
        .values(
            {
                handle.column(name): sa.bindparam(f"reep_sync_new{i}")
                for i, name in enumerate(value_cols)
            }
        )
    )


def _update_params(
    update: RowUpdate,
    value_cols: Sequence[str],
    guard_shape: Sequence[tuple[str, bool]],
) -> dict[str, Any]:
    item: dict[str, Any] = {f"reep_sync_pk{i}": v for i, v in enumerate(update.pk)}
    item.update(
        {f"reep_sync_new{i}": update.values[c] for i, c in enumerate(value_cols)}
    )
    item.update(
        {
            f"reep_sync_old{i}": update.guard[name]
            for i, (name, is_null) in enumerate(guard_shape)
            if not is_null
        }
    )
    return item
