"""Database access for the sync: the engine, table checks, reads and guarded writes.

Every statement comes from SQLAlchemy Core with reflected ``Column`` objects, so
names are quoted for each dialect and every value is a bound parameter.
"""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc

from reep.dbsync._plan import ColumnKind, Row, read_cell
from reep.errors import ConfigError, DatabaseError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from reep.dbsync._config import TableConfig
    from reep.dbsync._plan import Write

__all__ = [
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
    """Return a database URL with its password hidden.

    Args:
        url: The URL.

    Returns:
        The URL text with ``***`` in place of the password.
    """
    try:
        parsed = sa.make_url(url) if isinstance(url, str) else url
    except sa_exc.ArgumentError:
        return "(a URL that is not valid)"
    return parsed.render_as_string(hide_password=True)


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
        The engine and the URL with its password hidden.

    Raises:
        ConfigError: If the URL is not valid or its driver is not installed.
    """
    try:
        parsed = _with_driver(sa.make_url(url))
    except sa_exc.ArgumentError as exc:
        raise ConfigError(
            "The database URL is not valid.",
            hint="Use a SQLAlchemy URL such as postgresql://user:password@host/dbname "
            "or sqlite:///path/to/file.db.",
        ) from exc
    shown = masked_url(parsed)
    try:
        engine = sa.create_engine(parsed)
    except (ImportError, sa_exc.NoSuchModuleError) as exc:
        backend = parsed.get_backend_name()
        extra = _EXTRA_FOR.get(backend, "the driver for your database")
        raise ConfigError(
            f"No database driver for {shown}: {exc}",
            hint=f"Install {extra}, for example: pip install '{extra}'.",
        ) from exc
    return engine, shown


@contextmanager
def database_errors(shown: str, action: str) -> Iterator[None]:
    """Turn SQLAlchemy errors into :class:`DatabaseError`.

    The message uses the driver's own error text, not SQLAlchemy's, which can
    include statement parameters.

    Args:
        shown: The URL with its password hidden.
        action: What the code was doing, for the message.

    Yields:
        Nothing.

    Raises:
        DatabaseError: If a database error occurs.
    """
    try:
        yield
    except sa_exc.DBAPIError as exc:
        detail = " ".join(str(exc.orig).split()) if exc.orig is not None else ""
        raise DatabaseError(
            f"{action} failed on {shown}: {detail or type(exc).__name__}",
            hint="Check the database URL, the credentials and that the database is "
            "running.",
        ) from exc
    except sa_exc.SQLAlchemyError as exc:
        raise DatabaseError(f"{action} failed on {shown}: {exc}") from exc


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
        shown: The URL with its password hidden.

    Returns:
        The handle.

    Raises:
        ConfigError: If the table or a column is missing, a column cannot hold its
            values, or the primary key is not a unique key of the table.
        DatabaseError: If the database cannot be read.
    """
    where = f"reep.yml table entry {config.name!r}"
    with database_errors(shown, f"Reading the structure of {config.qualified_name}"):
        inspector = sa.inspect(engine)
        if not inspector.has_table(config.table, schema=config.schema):
            names = sorted(inspector.get_table_names(schema=config.schema))
            raise ConfigError(
                f"{where}: the database has no table {config.qualified_name}.",
                hint=f"Tables: {', '.join(names[:30]) or '(none)'}.",
            )
        table = sa.Table(
            config.table, sa.MetaData(), schema=config.schema, autoload_with=engine
        )
        pk_names = list(
            inspector.get_pk_constraint(config.table, schema=config.schema).get(
                "constrained_columns"
            )
            or []
        )
        unique_sets = [
            list(u.get("column_names") or [])
            for u in inspector.get_unique_constraints(
                config.table, schema=config.schema
            )
        ]
        unique_sets += [
            [c for c in (i.get("column_names") or []) if c is not None]
            for i in inspector.get_indexes(config.table, schema=config.schema)
            if i.get("unique")
        ]
    actual = [c.name for c in table.columns]
    configured = [
        *config.primary_key,
        *(c for c in (config.reep_id, config.status) if c is not None),
        *(pc.column for pc in config.provider_ids),
    ]
    columns = {name: _find_column(name, actual, where, config) for name in configured}
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
        shown: The URL with its password hidden.

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
            yield [
                Row(
                    pk=tuple(r[:width]),
                    cells={name: read_cell(r[i]) for i, name in enumerate(columns)},
                )
                for r in partition
            ]


def apply_writes(
    engine: sa.Engine,
    groups: Sequence[tuple[TableHandle, str, bool, Sequence[Write]]],
    *,
    shown: str,
) -> list[int]:
    """Run guarded updates in one transaction.

    Each update names one row by its primary key and changes a cell only while it
    still holds the value the plan saw: NULL for a new value, or the old value for a
    replacement. A row that changed in the meantime is left as it is.

    Args:
        engine: The engine.
        groups: ``(table, column, null_guard, writes)`` tuples. ``null_guard`` is
            ``True`` when each cell must still be NULL.
        shown: The URL with its password hidden.

    Returns:
        The number of rows updated in each group.

    Raises:
        DatabaseError: If an update fails; the transaction is rolled back.
    """
    counts: list[int] = []
    with database_errors(shown, "Writing"), engine.begin() as conn:
        multi = bool(conn.dialect.supports_sane_multi_rowcount)
        for handle, column, null_guard, writes in groups:
            if not writes:
                counts.append(0)
                continue
            col = handle.column(column)
            conditions: list[sa.ColumnElement[bool]] = [
                handle.column(name) == sa.bindparam(f"reep_sync_pk{i}")
                for i, name in enumerate(handle.config.primary_key)
            ]
            conditions.append(
                col.is_(None) if null_guard else col == sa.bindparam("reep_sync_old")
            )
            stmt = (
                sa.update(handle.table)
                .where(*conditions)
                .values({col: sa.bindparam("reep_sync_new")})
            )
            params: list[dict[str, Any]] = []
            for w in writes:
                item: dict[str, Any] = {"reep_sync_new": w.value}
                item.update({f"reep_sync_pk{i}": v for i, v in enumerate(w.pk)})
                if not null_guard:
                    item["reep_sync_old"] = w.old
                params.append(item)
            written = 0
            if multi:
                for start in range(0, len(params), _WRITE_CHUNK):
                    chunk = params[start : start + _WRITE_CHUNK]
                    written += max(conn.execute(stmt, chunk).rowcount, 0)
            else:
                for item in params:
                    written += max(conn.execute(stmt, item).rowcount, 0)
            counts.append(written)
    return counts
