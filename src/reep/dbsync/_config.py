"""The ``reep.yml`` file: which tables and columns the database sync reads and fills."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reep.errors import ConfigError
from reep.models import ENTITY_TYPES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType

__all__ = [
    "CONFIG_VERSION",
    "DEFAULT_CONFIG_NAME",
    "ENV_CONFIG",
    "ENV_DATABASE_URLS",
    "Config",
    "ProviderColumn",
    "TableConfig",
    "config_template",
    "load_config",
    "parse_config",
    "require",
]

#: The version of the ``reep.yml`` format.
CONFIG_VERSION = 1
#: The file that the commands read when no path is given.
DEFAULT_CONFIG_NAME = "reep.yml"
#: The environment variable that names the config file.
ENV_CONFIG = "REEP_CONFIG"
#: The environment variables that give the database URL when ``reep.yml`` has none.
ENV_DATABASE_URLS = ("REEP_DATABASE_URL", "DATABASE_URL")

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TOP_KEYS = frozenset({"version", "database", "run_log", "tables"})
_TABLE_KEYS = frozenset(
    {
        "table",
        "schema",
        "entity_type",
        "primary_key",
        "reep_id",
        "status",
        "provider_ids",
    }
)
_EXTRAS = {
    "yaml": "pip install 'reep[db]'",
    "sqlalchemy": "pip install 'reep[db]' (or reep[postgres], reep[mysql] or "
    "reep[mssql])",
}


def require(module: str) -> ModuleType:
    """Import an optional dependency of the database sync.

    Args:
        module: ``"yaml"`` or ``"sqlalchemy"``.

    Returns:
        The module.

    Raises:
        ConfigError: If the module is not installed.
    """
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ConfigError(
            f"The database sync needs the {module!r} package.",
            hint=f"Install it with: {_EXTRAS.get(module, 'pip install reep[db]')}",
        ) from exc


@dataclass(frozen=True, slots=True)
class ProviderColumn:
    """A column that holds one provider's IDs.

    Attributes:
        provider: The provider slug, for example ``"transfermarkt"``.
        namespace: The provider's namespace, for example ``"spieler"``.
        column: The column name.
    """

    provider: str
    namespace: str
    column: str

    @property
    def key(self) -> str:
        """Return ``provider/namespace``."""
        return f"{self.provider}/{self.namespace}"


@dataclass(frozen=True, slots=True)
class TableConfig:
    """One table entry of ``reep.yml``.

    Attributes:
        name: The entry's name in ``reep.yml`` (used by ``--table``).
        table: The table name.
        entity_type: The entity type of the table's rows, for example ``"player"``.
        primary_key: The columns that identify a row.
        reep_id: The column for the Reep ID, or ``None``.
        status: The column that the sync fills with each row's status, or ``None``.
        provider_ids: The provider ID columns.
        schema: The table's schema, or ``None`` for the default schema.
    """

    name: str
    table: str
    entity_type: str
    primary_key: tuple[str, ...]
    reep_id: str | None = None
    status: str | None = None
    provider_ids: tuple[ProviderColumn, ...] = ()
    schema: str | None = None

    @property
    def qualified_name(self) -> str:
        """Return ``schema.table``, or the table name."""
        return f"{self.schema}.{self.table}" if self.schema else self.table

    def columns_for(self, target: str) -> list[ProviderColumn]:
        """Return the provider columns that a sync target names.

        Args:
            target: A provider (``"fbref"``) or ``provider/namespace``.

        Returns:
            The matching columns.
        """
        provider, _, namespace = target.partition("/")
        return [
            pc
            for pc in self.provider_ids
            if pc.provider == provider and (not namespace or pc.namespace == namespace)
        ]


@dataclass(frozen=True, slots=True)
class Config:
    """The contents of a ``reep.yml`` file.

    Attributes:
        tables: The table entries.
        database: The database URL as written in the file. It can name environment
            variables as ``${NAME}``; :meth:`database_url` expands them.
        run_log: The file that each run appends a JSON line to, or ``None`` for the
            default file in the platform log directory.
        log_runs: ``False`` when the file sets ``run_log: false``.
        path: The file the config came from, or ``None``.
    """

    tables: tuple[TableConfig, ...]
    database: str | None = field(default=None, repr=False)
    run_log: Path | None = None
    log_runs: bool = True
    path: Path | None = None

    def database_url(self, env: Mapping[str, str] | None = None) -> str:
        """Return the database URL with environment variables expanded.

        Args:
            env: The environment; ``os.environ`` when ``None``.

        Returns:
            The URL. It can contain a password: do not print it.

        Raises:
            ConfigError: If no URL is set or a named variable is not set.
        """
        source = os.environ if env is None else env
        if self.database:

            def expand(match: re.Match[str]) -> str:
                name = match.group(1)
                value = source.get(name, "")
                if not value:
                    raise ConfigError(
                        f"reep.yml names the environment variable {name}, which is "
                        "not set.",
                        hint=f"Set {name} to the database URL, for example "
                        "postgresql://user:password@host/dbname.",
                    )
                return value

            return _ENV_REF.sub(expand, self.database)
        for name in ENV_DATABASE_URLS:
            value = source.get(name, "").strip()
            if value:
                return value
        raise ConfigError(
            "No database URL.",
            hint="Add 'database: ${DATABASE_URL}' to reep.yml, or set "
            "REEP_DATABASE_URL or DATABASE_URL to a SQLAlchemy URL such as "
            "postgresql://user:password@host/dbname.",
        )

    def select(
        self,
        names: Sequence[str] | None = None,
        *,
        entity_type: str | None = None,
    ) -> list[TableConfig]:
        """Return the table entries that the filters choose.

        Args:
            names: Entry names; ``None`` or empty for all.
            entity_type: Only entries of this entity type.

        Returns:
            The entries, in file order.

        Raises:
            ConfigError: If a name is not in the file, or no entry matches.
        """
        known = {t.name for t in self.tables}
        for name in names or ():
            if name not in known:
                raise ConfigError(
                    f"reep.yml has no table entry {name!r}.",
                    hint=f"Entries: {', '.join(sorted(known))}.",
                )
        chosen = [
            t
            for t in self.tables
            if (not names or t.name in names)
            and (entity_type is None or t.entity_type == entity_type)
        ]
        if not chosen:
            raise ConfigError(
                "No table entry matches the filters.",
                hint=f"Entries: {', '.join(sorted(known))}.",
            )
        return chosen


def load_config(path: str | Path) -> Config:
    """Read and check a ``reep.yml`` file.

    Args:
        path: The file.

    Returns:
        The config.

    Raises:
        ConfigError: If the file is missing or not valid.
    """
    yaml = require("yaml")
    file = Path(path).expanduser()
    if not file.is_file():
        raise ConfigError(
            f"No config file at {file}.",
            hint="Run 'reep init' to write an example reep.yml, or pass --config.",
        )
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # str(exc) quotes the source line, which can hold a database password.
        raise ConfigError(f"{file} is not valid YAML: {_yaml_problem(exc)}") from None
    return parse_config(data, path=file)


def _yaml_problem(exc: Exception) -> str:
    """Return a YAML error's kind and position, without any text from the file.

    PyYAML's messages quote the file: the source line, and in ``problem`` a tag or
    token. Either can hold a database password.
    """
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    kind = type(exc).__name__
    if mark is not None:
        return f"{kind} at line {mark.line + 1}, column {mark.column + 1}"
    return kind


def parse_config(data: object, *, path: Path | None = None) -> Config:
    """Check the parsed contents of a ``reep.yml`` file.

    Args:
        data: The parsed YAML.
        path: The file it came from, for messages and relative paths.

    Returns:
        The config.

    Raises:
        ConfigError: If the contents are not valid.
    """
    where = str(path) if path else "reep.yml"
    top = _mapping(data, where)
    _check_keys(top, _TOP_KEYS, where)
    version = top.get("version")
    if version != CONFIG_VERSION:
        raise ConfigError(
            f"{where}: version must be {CONFIG_VERSION}.",
            hint="Add 'version: 1' at the top of the file.",
        )
    database = top.get("database")
    if database is not None and not (isinstance(database, str) and database.strip()):
        raise ConfigError(f"{where}: database must be a URL string.")
    run_log: Path | None = None
    log_runs = True
    raw_log = top.get("run_log")
    if raw_log is False:
        log_runs = False
    elif isinstance(raw_log, str) and raw_log.strip():
        run_log = Path(raw_log).expanduser()
        if not run_log.is_absolute() and path is not None:
            run_log = path.parent / run_log
    elif raw_log is not None:
        raise ConfigError(f"{where}: run_log must be a file path or false.")
    tables_raw = _mapping(top.get("tables"), f"{where}: tables")
    if not tables_raw:
        raise ConfigError(f"{where}: tables has no entries.")
    tables = tuple(
        _table(name, value, f"{where}: tables entry {index}")
        for index, (name, value) in enumerate(tables_raw.items(), start=1)
    )
    return Config(
        tables=tables,
        database=database.strip() if isinstance(database, str) else None,
        run_log=run_log,
        log_runs=log_runs,
        path=path,
    )


def _table(name: str, raw: object, where: str) -> TableConfig:
    entry = _mapping(raw, where)
    _check_keys(entry, _TABLE_KEYS, where)
    table = _name(entry.get("table"), f"{where}.table", required=True) or ""
    schema = _name(entry.get("schema"), f"{where}.schema")
    entity_type = entry.get("entity_type")
    if entity_type not in ENTITY_TYPES:
        raise ConfigError(
            f"{where}.entity_type must be one of: {', '.join(ENTITY_TYPES)}."
        )
    pk_raw = entry.get("primary_key")
    if isinstance(pk_raw, list):
        items: list[object] = list(pk_raw)
        primary_key = tuple(
            _name(v, f"{where}.primary_key", required=True) or "" for v in items
        )
    else:
        primary_key = (_name(pk_raw, f"{where}.primary_key", required=True) or "",)
    if not primary_key or len(set(primary_key)) != len(primary_key):
        raise ConfigError(f"{where}.primary_key must name distinct columns.")
    reep_id = _name(entry.get("reep_id"), f"{where}.reep_id")
    status = _name(entry.get("status"), f"{where}.status")
    provider_ids = _provider_ids(entry.get("provider_ids"), f"{where}.provider_ids")
    if reep_id is None and not provider_ids:
        raise ConfigError(
            f"{where} needs reep_id, provider_ids, or both.",
            hint="reep_id names the column for the Reep ID; provider_ids maps "
            "provider/namespace to the columns that hold provider IDs.",
        )
    if status is not None and reep_id is None:
        raise ConfigError(f"{where}.status needs reep_id: it records the Reep ID sync.")
    provider_cols = [pc.column for pc in provider_ids]
    if len(set(provider_cols)) != len(provider_cols):
        raise ConfigError(f"{where}.provider_ids names a column more than once.")
    owned = [c for c in (reep_id, status) if c is not None]
    for column in owned:
        if column in primary_key or column in provider_cols:
            raise ConfigError(
                f"{where}: reep_id and status columns must not have another role.",
                hint="reep_id and status need their own columns.",
            )
    if reep_id is not None and reep_id == status:
        raise ConfigError(f"{where}: reep_id and status must be different columns.")
    return TableConfig(
        name=name,
        table=table,
        entity_type=str(entity_type),
        primary_key=primary_key,
        reep_id=reep_id,
        status=status,
        provider_ids=provider_ids,
        schema=schema,
    )


def _provider_ids(raw: object, where: str) -> tuple[ProviderColumn, ...]:
    if raw is None:
        return ()
    entries = _mapping(raw, where)
    out: list[ProviderColumn] = []
    for key, column in entries.items():
        provider, sep, namespace = str(key).strip().lower().partition("/")
        if not sep or not _SLUG.match(provider) or not _SLUG.match(namespace):
            raise ConfigError(
                f"{where}: each key must be provider/namespace.",
                hint="Use keys such as transfermarkt/spieler or fbref/person. Run "
                "'reep providers' to list them.",
            )
        name = _name(column, f"{where} column", required=True)
        out.append(ProviderColumn(provider, namespace, name or ""))
    return tuple(out)


def _mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(
            f"{where} must be a mapping of keys to values.",
            hint="Run 'reep init' for an example file.",
        )
    return {str(k): v for k, v in value.items()}


def _check_keys(
    entry: Mapping[str, object], allowed: frozenset[str], where: str
) -> None:
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key; use only the allowed fields.",
            hint=f"Allowed keys: {', '.join(sorted(allowed))}.",
        )


def _name(value: object, where: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ConfigError(f"{where} is required.")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"{where} must be a column or table name.",
            hint="Put names that YAML reads as numbers or booleans (on, yes, 1) in "
            "quotes.",
        )
    return value.strip()


_TEMPLATE = """\
# reep.yml: the tables that 'reep sync' fills and 'reep review' checks.
# Reference: https://reep.football/docs/python/dbsync.html
version: 1

# The database, as a SQLAlchemy URL. ${NAME} reads an environment variable, so
# that the password stays out of this file. Without this key, reep reads
# REEP_DATABASE_URL, then DATABASE_URL. Examples, with the extra to install:
#   postgresql://user:password@host/dbname       pip install 'reep[postgres]'
#   mysql+pymysql://user:password@host/dbname    pip install 'reep[mysql]'
#   mssql+pymssql://user:password@host/dbname    pip install 'reep[mssql]'
#   sqlite:///path/to/file.db                    pip install 'reep[db]'
database: ${DATABASE_URL}

# Each run appends one JSON line (time, release stamp, counts) to a log file in
# the platform log directory. Set a path here, or false to turn the log off.
# run_log: reep-runs.jsonl

tables:
  players:              # a name for this entry: reep sync reep --table players
    table: players      # the table
    # schema: public    # its schema, when it is not the default one
    entity_type: player # player, coach, referee, team, competition, season,
                        # stage or match
    primary_key: id     # the primary key: a column, or a list such as [a, b]
    reep_id: reep_id    # a text column for the Reep ID (16 characters or more)
    # status: reep_status  # a text column for each row's sync status
    # provider/namespace: column ('reep providers' lists the pairs). reep reads
    # these columns to find the Reep ID; 'reep sync fbref' fills empty ones.
    provider_ids:
      transfermarkt/spieler: tm_id
      fbref/person: fbref_id
"""


def config_template() -> str:
    """Return a commented example ``reep.yml``.

    Returns:
        The file contents.
    """
    return _TEMPLATE
