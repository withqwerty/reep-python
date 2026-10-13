"""Run the database sync: read rows, plan each cell, write, report and log."""

from __future__ import annotations

import csv
import dataclasses
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reep.backends.local import LocalBackend
from reep.config import Settings
from reep.dbsync._config import Config, TableConfig, load_config, require
from reep.dbsync._plan import (
    OUTCOMES,
    REVIEW_OUTCOMES,
    CellPlan,
    Planner,
    Row,
    Target,
    Write,
)
from reep.errors import ConfigError, InvalidArgumentError, UnknownProviderError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import sqlalchemy as sa

    from reep.dbsync._database import TableHandle
    from reep.models import Bridge, IdState, ProviderId, Resolution
    from reep.register import Register

__all__ = ["REVIEW_COLUMNS", "SyncReport", "TargetReport", "review", "sync"]

logger = logging.getLogger(__name__)

#: The columns of the review CSV.
REVIEW_COLUMNS = (
    "table",
    "entity_type",
    "target",
    "column",
    "primary_key",
    "value",
    "outcome",
    "reep_id",
    "detail",
    "release_stamp",
)

_PARTITION = 5000
_TARGET = re.compile(r"^(reep|[a-z0-9][a-z0-9_.-]*(/[a-z0-9][a-z0-9_.-]*)?)$")


@dataclass(frozen=True, slots=True)
class TargetReport:
    """The counts for one target column of one table.

    Attributes:
        table: The table entry name.
        entity_type: The table's entity type.
        target: ``"reep"`` or ``provider/namespace``.
        column: The column.
        rows: The number of rows read.
        outcomes: The number of cells with each outcome.
        planned: The cells the run plans to write.
        written: The cells written (0 in a dry run).
        status_planned: The status cells the run plans to write.
        status_written: The status cells written (0 in a dry run).
    """

    table: str
    entity_type: str
    target: str
    column: str
    rows: int
    outcomes: dict[str, int] = field(default_factory=dict)
    planned: int = 0
    written: int = 0
    status_planned: int = 0
    status_written: int = 0

    @property
    def needs_review(self) -> int:
        """Return the number of cells that need review."""
        return sum(n for o, n in self.outcomes.items() if o in REVIEW_OUTCOMES)

    def to_dict(self) -> dict[str, Any]:
        """Return the report as JSON-ready data."""
        return {
            "table": self.table,
            "entity_type": self.entity_type,
            "target": self.target,
            "column": self.column,
            "rows": self.rows,
            "outcomes": {o: self.outcomes.get(o, 0) for o in OUTCOMES},
            "planned": self.planned,
            "written": self.written,
            "status_planned": self.status_planned,
            "status_written": self.status_written,
            "needs_review": self.needs_review,
        }


@dataclass(frozen=True, slots=True)
class SyncReport:
    """The result of :func:`sync` or :func:`review`.

    Attributes:
        command: ``"sync"`` or ``"review"``.
        apply: Whether the run wrote to the database.
        release_stamp: The release of the local catalogue.
        database: The database URL with its password hidden.
        targets: The counts for each table and target column.
        review_file: The review CSV, when the run wrote one.
        run_log: The run log file, when the run wrote to one.
        seconds: How long the run took.
        notes: Messages about the run.
    """

    command: str
    apply: bool
    release_stamp: str | None
    database: str
    targets: tuple[TargetReport, ...]
    review_file: Path | None = None
    run_log: Path | None = None
    seconds: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def planned(self) -> int:
        """Return the number of cells the run plans to write (status cells too)."""
        return sum(t.planned + t.status_planned for t in self.targets)

    @property
    def written(self) -> int:
        """Return the number of cells written (status cells too)."""
        return sum(t.written + t.status_written for t in self.targets)

    @property
    def needs_review(self) -> int:
        """Return the number of cells that need review."""
        return sum(t.needs_review for t in self.targets)

    @property
    def outcomes(self) -> dict[str, int]:
        """Return the number of cells with each outcome, over all targets."""
        total: Counter[str] = Counter()
        for t in self.targets:
            total.update(t.outcomes)
        return {o: total.get(o, 0) for o in OUTCOMES}

    def to_dict(self) -> dict[str, Any]:
        """Return the report as JSON-ready data."""
        return {
            "command": self.command,
            "apply": self.apply,
            "release_stamp": self.release_stamp,
            "database": self.database,
            "targets": [t.to_dict() for t in self.targets],
            "outcomes": self.outcomes,
            "planned": self.planned,
            "written": self.written,
            "needs_review": self.needs_review,
            "review_file": str(self.review_file) if self.review_file else None,
            "run_log": str(self.run_log) if self.run_log else None,
            "seconds": round(self.seconds, 3),
            "notes": list(self.notes),
        }


def sync(
    config: Config | str | Path,
    register: Register,
    targets: Sequence[str] = ("reep",),
    *,
    apply: bool = False,
    repair_redirected: bool = False,
    tables: Sequence[str] | None = None,
    entity_type: str | None = None,
    engine: sa.Engine | None = None,
    run_log: Path | bool | None = None,
    settings: Settings | None = None,
) -> SyncReport:
    """Fill Reep IDs, or other providers' IDs, into the configured tables.

    The sync matches exactly on ``(provider, namespace, external_id)``. It writes a
    cell only while the cell is NULL, so it does not change a value that is already
    there; ``repair_redirected`` is the one exception (see below). Without
    ``apply`` the run writes nothing and reports what it would write.

    Args:
        config: The config, or the path of a ``reep.yml`` file.
        register: A local register (``Register.local()``).
        targets: ``"reep"`` for the Reep ID column, a provider (``"fbref"``) or
            ``provider/namespace`` for provider ID columns.
        apply: Write to the database.
        repair_redirected: Replace stored Reep IDs that merged into another ID
            with the survivor.
        tables: Only these table entries.
        entity_type: Only table entries of this entity type.
        engine: A SQLAlchemy engine to use in place of the config's URL.
        run_log: ``None`` for the config's or the default log file, ``False`` for
            no log, or a file path.
        settings: The settings; read from the environment when ``None``.

    Returns:
        The report.

    Raises:
        ConfigError: If the config, a target or a table does not fit.
        DatabaseError: If the database cannot be read or written.
        InvalidArgumentError: If the register is not a local register.
    """
    cfg = load_config(config) if isinstance(config, (str, Path)) else config
    names = _target_names(targets)
    if not names:
        raise ConfigError(
            "Name at least one target.",
            hint="Use 'reep' for the Reep ID column, or a provider such as 'fbref'.",
        )
    return _run(
        cfg,
        register,
        names,
        command="sync",
        apply=apply,
        repair_redirected=repair_redirected,
        tables=tables,
        entity_type=entity_type,
        engine=engine,
        run_log=run_log,
        settings=settings,
        review_file=None,
        strict_targets=True,
    )


def review(
    config: Config | str | Path,
    register: Register,
    *,
    output: str | Path | None = None,
    targets: Sequence[str] | None = None,
    tables: Sequence[str] | None = None,
    entity_type: str | None = None,
    engine: sa.Engine | None = None,
    run_log: Path | bool | None = None,
    settings: Settings | None = None,
) -> SyncReport:
    """Check the configured tables and report the cells that need review.

    The review writes nothing to the database. It plans every target (the Reep ID
    column and each provider column), counts the outcomes, and writes the cells
    that need review to a CSV file.

    Args:
        config: The config, or the path of a ``reep.yml`` file.
        register: A local register (``Register.local()``).
        output: The CSV file to write, or ``None`` for no file.
        targets: Only these targets; ``None`` for all.
        tables: Only these table entries.
        entity_type: Only table entries of this entity type.
        engine: A SQLAlchemy engine to use in place of the config's URL.
        run_log: ``None`` for the config's or the default log file, ``False`` for
            no log, or a file path.
        settings: The settings; read from the environment when ``None``.

    Returns:
        The report.

    Raises:
        ConfigError: If the config, a target or a table does not fit.
        DatabaseError: If the database cannot be read.
        InvalidArgumentError: If the register is not a local register.
    """
    cfg = load_config(config) if isinstance(config, (str, Path)) else config
    strict = targets is not None
    names = _target_names(targets) if targets is not None else []
    if not names:
        names = ["reep"]
        for table in cfg.tables:
            names.extend(pc.key for pc in table.provider_ids if pc.key not in names)
    return _run(
        cfg,
        register,
        names,
        command="review",
        apply=False,
        repair_redirected=False,
        tables=tables,
        entity_type=entity_type,
        engine=engine,
        run_log=run_log,
        settings=settings,
        review_file=Path(output).expanduser() if output is not None else None,
        strict_targets=strict,
    )


def _target_names(targets: Sequence[str]) -> list[str]:
    out: list[str] = []
    for raw in targets:
        name = raw.strip().lower()
        if not _TARGET.match(name):
            raise ConfigError(
                f"{raw!r} is not a sync target.",
                hint="Use 'reep', a provider such as 'fbref', or provider/namespace "
                "such as 'transfermarkt/spieler'.",
            )
        if name not in out:
            out.append(name)
    return out


def _plan_targets(
    tables: Sequence[TableConfig], names: Sequence[str], *, strict: bool
) -> dict[str, list[Target]]:
    out: dict[str, list[Target]] = {}
    matched: set[str] = set()
    for table in tables:
        chosen: list[Target] = []
        for name in names:
            if name == "reep":
                if table.reep_id is not None:
                    chosen.append(Target(table.reep_id))
                    matched.add(name)
                continue
            for pc in table.columns_for(name):
                if all(t.column != pc.column for t in chosen):
                    chosen.append(Target(pc.column, pc))
                    matched.add(name)
        out[table.name] = chosen
    missing = [n for n in names if n not in matched]
    if strict and missing:
        what = "a reep_id column" if missing[0] == "reep" else f"a {missing[0]} column"
        raise ConfigError(
            f"No chosen table entry has {what}.",
            hint="Add it to reep.yml (reep_id, or provider_ids), or choose other "
            "tables or targets.",
        )
    return out


def _local_backend(register: Register) -> LocalBackend:
    backend = register.backend
    if not isinstance(backend, LocalBackend):
        raise InvalidArgumentError(
            "The database sync reads the local catalogue, not the API.",
            hint="Run 'reep catalog sync' and use Register.local() (in the CLI, leave "
            "out --api).",
        )
    return backend


def _check_pairs(register: Register, tables: Sequence[TableConfig]) -> None:
    for table in tables:
        for pc in table.provider_ids:
            where = f"reep.yml table entry {table.name!r}, provider_ids {pc.key}"
            try:
                namespaces = register.namespaces(pc.provider)
            except UnknownProviderError as exc:
                raise ConfigError(f"{where}: {exc.message}", hint=exc.hint) from exc
            if namespaces and pc.namespace not in namespaces:
                raise ConfigError(
                    f"{where}: {pc.provider} has no namespace {pc.namespace!r}.",
                    hint=f"Valid namespaces for {pc.provider}: "
                    f"{', '.join(namespaces)}.",
                )


@dataclass
class _Tally:
    report: TargetReport
    outcomes: Counter[str] = field(default_factory=Counter)
    writes: list[Write] = field(default_factory=list)
    status_writes: list[Write] = field(default_factory=list)


class _ReviewWriter:
    def __init__(self, path: Path, stamp: str | None) -> None:
        if not path.parent.is_dir():
            raise ConfigError(f"The folder for the review file {path} does not exist.")
        self.path = path
        self.part = path.with_name(path.name + ".part")
        self.stamp = stamp
        self._file = self.part.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(REVIEW_COLUMNS)

    def write(self, table: TableConfig, plan: CellPlan) -> None:
        key = (
            str(plan.pk[0])
            if len(plan.pk) == 1
            else json.dumps([str(v) for v in plan.pk], ensure_ascii=False)
        )
        self._writer.writerow(
            [
                table.name,
                table.entity_type,
                plan.target.name,
                plan.target.column,
                key,
                plan.value if plan.value is not None else "",
                plan.outcome,
                plan.reep_id or "",
                plan.detail or "",
                self.stamp or "",
            ]
        )

    def close(self, *, keep: bool) -> None:
        self._file.close()
        if keep:
            self.part.replace(self.path)
        else:
            self.part.unlink(missing_ok=True)


def _run(
    config: Config,
    register: Register,
    names: Sequence[str],
    *,
    command: str,
    apply: bool,
    repair_redirected: bool,
    tables: Sequence[str] | None,
    entity_type: str | None,
    engine: sa.Engine | None,
    run_log: Path | bool | None,
    settings: Settings | None,
    review_file: Path | None,
    strict_targets: bool,
) -> SyncReport:
    started = time.monotonic()
    backend = _local_backend(register)
    require("sqlalchemy")
    from reep.dbsync import _database as db

    chosen = config.select(tables, entity_type=entity_type)
    plan_targets = _plan_targets(chosen, names, strict=strict_targets)
    chosen = [t for t in chosen if plan_targets[t.name]]
    _check_pairs(register, chosen)
    stamp = register.release.stamp
    if engine is None:
        eng, shown = db.make_engine(config.database_url())
        own_engine = True
    else:
        eng, shown, own_engine = engine, db.masked_url(engine.url), False
    writer = _ReviewWriter(review_file, stamp) if review_file is not None else None
    tallies: list[tuple[TableHandle, Planner, list[_Tally]]] = []
    done = False
    try:
        handles = [db.reflect(eng, t, shown) for t in chosen]
        for handle in handles:
            planner = Planner(
                handle.config,
                plan_targets[handle.config.name],
                handle.kinds,
                repair_redirected=repair_redirected,
            )
            per_target = [
                _Tally(
                    TargetReport(
                        table=handle.config.name,
                        entity_type=handle.config.entity_type,
                        target=t.name,
                        column=t.column,
                        rows=0,
                    )
                )
                for t in planner.targets
            ]
            rows_read = 0
            for rows in db.read_rows(
                eng,
                handle,
                planner.columns,
                partition_size=_PARTITION,
                shown=shown,
            ):
                rows_read += len(rows)
                for plan in _plan_partition(planner, rows, register, backend):
                    tally = per_target[planner.targets.index(plan.target)]
                    tally.outcomes[plan.outcome] += 1
                    if plan.write is not None:
                        tally.writes.append(plan.write)
                    if plan.status_write is not None:
                        tally.status_writes.append(plan.status_write)
                    if writer is not None and plan.outcome in REVIEW_OUTCOMES:
                        writer.write(handle.config, plan)
            for tally in per_target:
                tally.report = TargetReport(
                    table=tally.report.table,
                    entity_type=tally.report.entity_type,
                    target=tally.report.target,
                    column=tally.report.column,
                    rows=rows_read,
                    outcomes={
                        o: tally.outcomes[o] for o in OUTCOMES if tally.outcomes[o]
                    },
                    planned=len(tally.writes),
                    status_planned=len(tally.status_writes),
                )
            tallies.append((handle, planner, per_target))
        reports = _write(db, eng, tallies, apply=apply, shown=shown)
        done = True
    finally:
        if writer is not None:
            writer.close(keep=done)
        if own_engine:
            eng.dispose()
    report = SyncReport(
        command=command,
        apply=apply,
        release_stamp=stamp,
        database=shown,
        targets=tuple(reports),
        review_file=review_file,
        seconds=time.monotonic() - started,
    )
    return _log(report, config, run_log, settings or Settings.from_env())


def _plan_partition(
    planner: Planner,
    rows: Sequence[Row],
    register: Register,
    backend: LocalBackend,
) -> list[CellPlan]:
    pids: list[ProviderId] = sorted(
        {pid for row in rows for pid in planner.sources(row)},
        key=lambda p: (p.provider, p.namespace, p.external_id),
    )
    resolutions: Mapping[ProviderId, Resolution] = dict(
        zip(pids, register.resolve_many(pids), strict=True)
    )
    stored = sorted({s for row in rows if (s := planner.stored_id(row)) is not None})
    states: Mapping[str, IdState] = dict(
        zip(stored, register.current_many(stored), strict=True)
    )
    identities = [planner.identity(row, resolutions, states) for row in rows]
    bridges: dict[str, dict[str, list[Bridge]]] = {}
    for target in planner.provider_targets:
        pc = target.provider_column
        if pc is None:  # pragma: no cover - provider_targets have a provider column
            continue
        need = sorted(
            {
                ident.reep_id
                for row, ident in zip(rows, identities, strict=True)
                if ident.reep_id and row.cells[target.column].text is None
            }
        )
        bridges[target.name] = backend.bridges_many(need, pc.provider, pc.namespace)
    return [
        plan
        for row, ident in zip(rows, identities, strict=True)
        for plan in planner.plan(row, ident, resolutions, states, bridges)
    ]


def _write(
    db: Any,
    engine: sa.Engine,
    tallies: Sequence[tuple[TableHandle, Planner, list[_Tally]]],
    *,
    apply: bool,
    shown: str,
) -> list[TargetReport]:
    groups: list[tuple[TableHandle, str, bool, Sequence[Write]]] = []
    slots: list[tuple[_Tally, str]] = []
    if apply:
        for handle, _planner, per_target in tallies:
            for tally in per_target:
                fills = [w for w in tally.writes if w.old is None]
                replaces = [w for w in tally.writes if w.old is not None]
                for null_guard, writes in ((True, fills), (False, replaces)):
                    if writes:
                        groups.append((handle, tally.report.column, null_guard, writes))
                        slots.append((tally, "written"))
                status_column = handle.config.status
                if status_column is not None:
                    nulls = [w for w in tally.status_writes if w.old is None]
                    others = [w for w in tally.status_writes if w.old is not None]
                    for null_guard, writes in ((True, nulls), (False, others)):
                        if writes:
                            groups.append((handle, status_column, null_guard, writes))
                            slots.append((tally, "status_written"))
    counts = db.apply_writes(engine, groups, shown=shown) if groups else []
    written: dict[int, Counter[str]] = {}
    for (tally, kind), count in zip(slots, counts, strict=True):
        written.setdefault(id(tally), Counter())[kind] += count
    reports: list[TargetReport] = []
    for _handle, _planner, per_target in tallies:
        for tally in per_target:
            got = written.get(id(tally), Counter())
            r = tally.report
            reports.append(
                TargetReport(
                    table=r.table,
                    entity_type=r.entity_type,
                    target=r.target,
                    column=r.column,
                    rows=r.rows,
                    outcomes=r.outcomes,
                    planned=r.planned,
                    written=got["written"],
                    status_planned=r.status_planned,
                    status_written=got["status_written"],
                )
            )
    return reports


def _log(
    report: SyncReport,
    config: Config,
    run_log: Path | bool | None,
    settings: Settings,
) -> SyncReport:
    if run_log is False or (run_log is None and not config.log_runs):
        return report
    if isinstance(run_log, Path):
        path = run_log
    else:
        path = config.run_log or settings.run_log_path
    entry = {
        "time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": str(config.path) if config.path else None,
        **report.to_dict(),
    }
    entry.pop("run_log", None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        note = f"The run log {path} was not written: {exc}"
        logger.warning(note)
        return _replace(report, notes=(*report.notes, note))
    return _replace(report, run_log=path)


def _replace(report: SyncReport, **changes: Any) -> SyncReport:
    return dataclasses.replace(report, **changes)
