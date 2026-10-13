"""Decide, for each row, what the database sync writes, keeps or reports.

This module holds no database code. The runner reads rows, asks the register about
their IDs, and passes the answers here.
"""

from __future__ import annotations

import decimal
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from reep.models import (
    ENTITY_TYPE_BY_LETTER,
    REEP_ID_PATTERN,
    IdStatus,
    ProviderId,
    ResolutionStatus,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from reep.dbsync._config import ProviderColumn, TableConfig
    from reep.models import Bridge, IdState, Resolution

__all__ = [
    "OUTCOMES",
    "REVIEW_OUTCOMES",
    "STATUS_VALUES",
    "Cell",
    "CellPlan",
    "ColumnKind",
    "Identity",
    "Planner",
    "Row",
    "Target",
    "Write",
    "read_cell",
]

# Outcomes that need no review.
NEW = "new"
REPAIR = "repair"
UNCHANGED = "unchanged"
RETIRED = "retired"
# Outcomes that need review.
REDIRECTED = "redirected"
WITHDRAWN = "withdrawn"
UNRESOLVED = "unresolved"
UNKNOWN_ID = "unknown_id"
CONFLICT = "conflict"
AMBIGUOUS = "ambiguous"
TYPE_MISMATCH = "type_mismatch"
INVALID_VALUE = "invalid_value"
NO_SOURCE = "no_source"
NO_TARGET = "no_target"
UPSTREAM_RETIRED = "upstream_retired"
UPSTREAM_REMOVED = "upstream_removed"
COLUMN_MISMATCH = "column_mismatch"

#: Every outcome, in report order.
OUTCOMES: tuple[str, ...] = (
    NEW,
    REPAIR,
    UNCHANGED,
    RETIRED,
    REDIRECTED,
    WITHDRAWN,
    UNRESOLVED,
    UNKNOWN_ID,
    CONFLICT,
    AMBIGUOUS,
    TYPE_MISMATCH,
    INVALID_VALUE,
    NO_SOURCE,
    NO_TARGET,
    UPSTREAM_RETIRED,
    UPSTREAM_REMOVED,
    COLUMN_MISMATCH,
)

#: Outcomes that leave a cell for a person to review.
REVIEW_OUTCOMES = frozenset(OUTCOMES) - {NEW, REPAIR, UNCHANGED, RETIRED}

_STATUS = {NEW: "resolved", REPAIR: "resolved", UNCHANGED: "resolved"}

#: The values that the sync writes to a table's status column.
STATUS_VALUES: tuple[str, ...] = (
    "resolved",
    RETIRED,
    REDIRECTED,
    WITHDRAWN,
    UNRESOLVED,
    UNKNOWN_ID,
    CONFLICT,
    AMBIGUOUS,
    TYPE_MISMATCH,
    INVALID_VALUE,
    NO_SOURCE,
)

_UPSTREAM = {"retired": UPSTREAM_RETIRED, "removed": UPSTREAM_REMOVED}


@dataclass(frozen=True, slots=True)
class Target:
    """A column that a run fills: the Reep ID column or a provider ID column.

    Attributes:
        column: The column name.
        provider_column: The provider column, or ``None`` for the Reep ID column.
    """

    column: str
    provider_column: ProviderColumn | None = None

    @property
    def name(self) -> str:
        """Return ``"reep"`` or ``provider/namespace``."""
        return "reep" if self.provider_column is None else self.provider_column.key


@dataclass(frozen=True, slots=True)
class ColumnKind:
    """What a column can hold.

    Attributes:
        kind: ``"text"``, ``"integer"`` or ``"uuid"``.
        length: The most characters a text column holds, or ``None``.
        max_int: The largest value an integer column holds, or ``None``.
    """

    kind: str
    length: int | None = None
    max_int: int | None = None


@dataclass(frozen=True, slots=True)
class Cell:
    """A value read from the database.

    Attributes:
        raw: The value as the driver returned it.
        text: ``None`` for SQL NULL; otherwise the value as text, with surrounding
            whitespace removed (an empty string stays empty).
        invalid: ``True`` when the value cannot be an ID (for example a float).
    """

    raw: Any
    text: str | None
    invalid: bool = False


@dataclass(frozen=True, slots=True)
class Row:
    """One row: its primary key values and the cells the run reads.

    Attributes:
        pk: The primary key values, in ``primary_key`` order.
        cells: The cells by column name.
    """

    pk: tuple[Any, ...]
    cells: dict[str, Cell]


@dataclass(frozen=True, slots=True)
class Identity:
    """The Reep ID of a row, from its stored Reep ID and its provider IDs.

    Attributes:
        reep_id: The row's current Reep ID, or ``None``.
        entity_status: ``"active"`` or ``"retired"`` for ``reep_id``.
        conflict: Why the row's IDs disagree, or ``None``.
        withdrawn: Which of the row's IDs is withdrawn, or ``None``.
        ambiguous: Which of the row's provider IDs names several entities, or
            ``None``.
        sources: The row's valid provider IDs.
        invalid_columns: Provider columns whose values cannot be IDs.
    """

    reep_id: str | None
    entity_status: str | None = None
    conflict: str | None = None
    withdrawn: str | None = None
    ambiguous: str | None = None
    sources: tuple[ProviderId, ...] = ()
    invalid_columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Write:
    """A guarded update of one cell.

    Attributes:
        column: The column.
        pk: The primary key values of the row.
        value: The new value.
        old: ``None`` when the cell must still be NULL; otherwise the value the
            cell must still hold.
    """

    column: str
    pk: tuple[Any, ...]
    value: Any
    old: Any = None


@dataclass(frozen=True, slots=True)
class CellPlan:
    """The decision for one cell.

    Attributes:
        target: The target column.
        pk: The primary key values of the row.
        value: The stored value as text, or ``None``.
        outcome: One of :data:`OUTCOMES`.
        reep_id: The row's Reep ID, when known.
        detail: More about the outcome, for the review report.
        write: The update for the cell, or ``None``.
        status_write: The update for the status column, or ``None``.
    """

    target: Target
    pk: tuple[Any, ...]
    value: str | None
    outcome: str
    reep_id: str | None = None
    detail: str | None = None
    write: Write | None = None
    status_write: Write | None = None


def read_cell(value: object) -> Cell:
    """Return a database value as a :class:`Cell`.

    Args:
        value: The value from the driver.

    Returns:
        The cell.
    """
    if value is None:
        return Cell(None, None)
    if isinstance(value, (bool, float)):
        return Cell(value, repr(value), invalid=True)
    if isinstance(value, str):
        return Cell(value, value.strip())
    if isinstance(value, int):
        return Cell(value, str(value))
    if isinstance(value, decimal.Decimal):
        if value.is_finite() and value == value.to_integral_value():
            return Cell(value, str(int(value)))
        return Cell(value, str(value), invalid=True)
    if isinstance(value, uuid.UUID):
        return Cell(value, str(value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return Cell(value, bytes(value).decode("utf-8").strip())
        except UnicodeDecodeError:
            return Cell(value, repr(value), invalid=True)
    return Cell(value, repr(value), invalid=True)


def _entity_type(reep_id: str) -> str:
    return ENTITY_TYPE_BY_LETTER[reep_id[1]]


_MISMATCH = object()


def _convert(value: str, kind: ColumnKind) -> object:
    if kind.kind == "integer":
        if not (value.isascii() and value.isdigit()):
            return _MISMATCH
        number = int(value)
        if kind.max_int is not None and number > kind.max_int:
            return _MISMATCH
        return number
    if kind.kind == "uuid":
        try:
            return uuid.UUID(value)
        except ValueError:
            return _MISMATCH
    if kind.length is not None and len(value) > kind.length:
        return _MISMATCH
    return value


class Planner:
    """Plans the cells of one table.

    Args:
        table: The table entry.
        targets: The columns to fill.
        kinds: What each provider target column can hold.
        repair_redirected: Replace stored Reep IDs that merged into another.
    """

    def __init__(
        self,
        table: TableConfig,
        targets: Sequence[Target],
        kinds: Mapping[str, ColumnKind],
        *,
        repair_redirected: bool = False,
    ) -> None:
        """Create the planner."""
        self.table = table
        self.targets = tuple(targets)
        self.kinds = dict(kinds)
        self.repair_redirected = repair_redirected
        self.provider_targets = tuple(
            t for t in self.targets if t.provider_column is not None
        )
        self.fills_reep = any(t.provider_column is None for t in self.targets)

    @property
    def columns(self) -> list[str]:
        """Return the columns to read, primary key first, without repeats."""
        names = list(self.table.primary_key)
        for name in (
            self.table.reep_id,
            self.table.status if self.fills_reep else None,
            *(pc.column for pc in self.table.provider_ids),
        ):
            if name is not None and name not in names:
                names.append(name)
        return names

    def sources(self, row: Row) -> list[ProviderId]:
        """Return the row's valid provider IDs.

        Args:
            row: The row.

        Returns:
            One provider ID per filled provider column.
        """
        out: list[ProviderId] = []
        for pc in self.table.provider_ids:
            cell = row.cells[pc.column]
            if cell.text and not cell.invalid:
                out.append(ProviderId(pc.provider, pc.namespace, cell.text))
        return out

    def stored_id(self, row: Row) -> str | None:
        """Return the row's stored Reep ID when it is a valid one.

        Args:
            row: The row.

        Returns:
            The Reep ID, or ``None``.
        """
        if self.table.reep_id is None:
            return None
        cell = row.cells[self.table.reep_id]
        if cell.text and not cell.invalid and REEP_ID_PATTERN.match(cell.text):
            return cell.text
        return None

    def identity(
        self,
        row: Row,
        resolutions: Mapping[ProviderId, Resolution],
        states: Mapping[str, IdState],
    ) -> Identity:
        """Return the row's Reep ID.

        The stored Reep ID (in its current form) and the Reep IDs that the provider
        IDs resolve to must agree. When they do not, the row has a conflict and no
        Reep ID.

        Args:
            row: The row.
            resolutions: The resolution of each provider ID.
            states: The state of each stored Reep ID.

        Returns:
            The identity.
        """
        sources = self.sources(row)
        derived: dict[str, str | None] = {}
        withdrawn: str | None = None
        ambiguous: str | None = None
        for pid in sources:
            res = resolutions[pid]
            if res.status is ResolutionStatus.FOUND and res.reep_id:
                derived.setdefault(res.reep_id, res.entity_status)
            elif res.status is ResolutionStatus.WITHDRAWN and withdrawn is None:
                withdrawn = f"{pid} is withdrawn ({res.withdrawn_reason})"
            elif res.status is ResolutionStatus.AMBIGUOUS and ambiguous is None:
                ambiguous = f"{pid} names {', '.join(res.candidates)}"
        invalid = tuple(
            pc.column for pc in self.table.provider_ids if row.cells[pc.column].invalid
        )
        stored = self.stored_id(row)
        state = states.get(stored) if stored else None
        stored_current: str | None = None
        if state is not None:
            if state.status in (IdStatus.ACTIVE, IdStatus.RETIRED, IdStatus.REDIRECTED):
                stored_current = state.current_id
            elif state.status is IdStatus.WITHDRAWN and withdrawn is None:
                withdrawn = f"{stored} is withdrawn ({state.reason})"
        conflict: str | None = None
        if len(derived) > 1:
            conflict = "the provider IDs give " + " and ".join(sorted(derived))
        single = next(iter(derived)) if len(derived) == 1 else None
        if conflict is None and stored_current and single and stored_current != single:
            conflict = (
                f"the stored Reep ID gives {stored_current}; the provider IDs give "
                f"{single}"
            )
        reep_id = None if conflict else (stored_current or single)
        if reep_id is not None and reep_id == stored_current and state is not None:
            entity_status = state.current_status
        else:
            entity_status = derived.get(single) if single else None
        return Identity(
            reep_id=reep_id,
            entity_status=entity_status,
            conflict=conflict,
            withdrawn=withdrawn,
            ambiguous=ambiguous,
            sources=tuple(sources),
            invalid_columns=invalid,
        )

    def plan(
        self,
        row: Row,
        identity: Identity,
        resolutions: Mapping[ProviderId, Resolution],
        states: Mapping[str, IdState],
        bridges: Mapping[str, Mapping[str, Sequence[Bridge]]],
    ) -> list[CellPlan]:
        """Plan every target cell of a row.

        Args:
            row: The row.
            identity: The row's identity from :meth:`identity`.
            resolutions: The resolution of each provider ID.
            states: The state of each stored Reep ID.
            bridges: For each provider target name, the bridges of each Reep ID.

        Returns:
            One plan per target.
        """
        out: list[CellPlan] = []
        for target in self.targets:
            if target.provider_column is None:
                out.append(self._plan_reep(target, row, identity, states))
            else:
                out.append(
                    self._plan_provider(
                        target,
                        target.provider_column,
                        row,
                        identity,
                        resolutions,
                        bridges.get(target.name, {}),
                    )
                )
        return out

    def _plan_reep(
        self,
        target: Target,
        row: Row,
        ident: Identity,
        states: Mapping[str, IdState],
    ) -> CellPlan:
        cell = row.cells[target.column]
        etype = self.table.entity_type
        write: Write | None = None
        detail: str | None = None
        if cell.text is None:
            if ident.conflict:
                outcome, detail = CONFLICT, ident.conflict
            elif ident.reep_id:
                if _entity_type(ident.reep_id) != etype:
                    outcome = TYPE_MISMATCH
                    detail = f"{ident.reep_id} is a {_entity_type(ident.reep_id)}"
                else:
                    write = Write(target.column, row.pk, ident.reep_id)
                    outcome = RETIRED if ident.entity_status == "retired" else NEW
            elif ident.withdrawn:
                outcome, detail = WITHDRAWN, ident.withdrawn
            elif ident.ambiguous:
                outcome, detail = AMBIGUOUS, ident.ambiguous
            elif ident.sources:
                outcome = UNRESOLVED
                detail = "no bridge for " + ", ".join(str(p) for p in ident.sources)
            elif ident.invalid_columns:
                outcome = INVALID_VALUE
                detail = "not an ID in " + ", ".join(ident.invalid_columns)
            else:
                outcome = NO_SOURCE
        elif cell.invalid or not REEP_ID_PATTERN.match(cell.text):
            outcome, detail = INVALID_VALUE, "not a Reep ID"
        else:
            state = states[cell.text]
            if _entity_type(cell.text) != etype:
                outcome = TYPE_MISMATCH
                detail = f"{cell.text} is a {_entity_type(cell.text)}"
            elif ident.conflict:
                outcome, detail = CONFLICT, ident.conflict
            elif state.status is IdStatus.ACTIVE:
                outcome = UNCHANGED
            elif state.status is IdStatus.RETIRED:
                outcome = RETIRED
            elif state.status is IdStatus.REDIRECTED and state.current_id:
                detail = f"merged into {state.current_id}"
                if self.repair_redirected:
                    outcome = REPAIR
                    write = Write(target.column, row.pk, state.current_id, cell.raw)
                else:
                    outcome = REDIRECTED
            elif state.status is IdStatus.WITHDRAWN:
                outcome, detail = WITHDRAWN, state.reason
            else:
                outcome = UNKNOWN_ID
        return CellPlan(
            target=target,
            pk=row.pk,
            value=cell.text,
            outcome=outcome,
            reep_id=ident.reep_id,
            detail=detail,
            write=write,
            status_write=self._status_write(row, outcome),
        )

    def _status_write(self, row: Row, outcome: str) -> Write | None:
        column = self.table.status
        if column is None:
            return None
        desired = _STATUS.get(outcome, outcome)
        cell = row.cells[column]
        if cell.text == desired:
            return None
        return Write(column, row.pk, desired, cell.raw)

    def _plan_provider(
        self,
        target: Target,
        pc: ProviderColumn,
        row: Row,
        ident: Identity,
        resolutions: Mapping[ProviderId, Resolution],
        bridges: Mapping[str, Sequence[Bridge]],
    ) -> CellPlan:
        cell = row.cells[pc.column]
        etype = self.table.entity_type
        write: Write | None = None
        detail: str | None = None
        if cell.text is None:
            if ident.conflict:
                outcome, detail = CONFLICT, ident.conflict
            elif not ident.reep_id:
                if ident.withdrawn:
                    outcome, detail = WITHDRAWN, ident.withdrawn
                else:
                    outcome, detail = UNRESOLVED, "the row has no Reep ID"
            elif _entity_type(ident.reep_id) != etype:
                outcome = TYPE_MISMATCH
                detail = f"{ident.reep_id} is a {_entity_type(ident.reep_id)}"
            else:
                found = list(bridges.get(ident.reep_id, ()))
                live = [b for b in found if b.upstream_status is None]
                ids = ", ".join(b.provider_id.external_id for b in found)
                if len(live) == 1:
                    external_id = live[0].provider_id.external_id
                    value = _convert(external_id, self.kinds[pc.column])
                    if value is _MISMATCH:
                        outcome = COLUMN_MISMATCH
                        detail = f"{external_id!r} does not fit the column"
                    else:
                        outcome = NEW
                        write = Write(pc.column, row.pk, value)
                elif len(live) > 1:
                    outcome, detail = AMBIGUOUS, f"{pc.key} has {ids}"
                elif found:
                    status = found[0].upstream_status or ""
                    outcome = _UPSTREAM.get(status, UPSTREAM_REMOVED)
                    detail = f"{pc.key} {ids} is {status} at the provider"
                else:
                    outcome = NO_TARGET
        elif cell.invalid or not cell.text:
            outcome, detail = INVALID_VALUE, "not an ID"
        else:
            res = resolutions[ProviderId(pc.provider, pc.namespace, cell.text)]
            if res.status is ResolutionStatus.FOUND and res.reep_id:
                upstream = res.bridge.upstream_status if res.bridge else None
                if _entity_type(res.reep_id) != etype:
                    outcome = TYPE_MISMATCH
                    detail = f"{res.reep_id} is a {_entity_type(res.reep_id)}"
                elif ident.conflict:
                    outcome, detail = CONFLICT, ident.conflict
                elif upstream:
                    outcome = _UPSTREAM.get(upstream, UPSTREAM_REMOVED)
                    detail = f"the ID is {upstream} at the provider"
                else:
                    outcome = UNCHANGED
            elif res.status is ResolutionStatus.WITHDRAWN:
                outcome, detail = WITHDRAWN, res.withdrawn_reason
            elif res.status is ResolutionStatus.AMBIGUOUS:
                outcome, detail = AMBIGUOUS, ", ".join(res.candidates)
            else:
                outcome = UNRESOLVED
        return CellPlan(
            target=target,
            pk=row.pk,
            value=cell.text,
            outcome=outcome,
            reep_id=ident.reep_id,
            detail=detail,
            write=write,
        )
