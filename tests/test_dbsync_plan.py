"""The planner's edge cases, and the database helpers."""

from __future__ import annotations

import decimal
import uuid
from typing import Any

import pytest

sa = pytest.importorskip("sqlalchemy")

from reep.dbsync import REVIEW_OUTCOMES, STATUS_VALUES, parse_config  # noqa: E402
from reep.dbsync._database import (  # noqa: E402
    column_kind,
    make_engine,
    masked_url,
)
from reep.dbsync._plan import (  # noqa: E402
    OUTCOMES,
    Cell,
    ColumnKind,
    Planner,
    Row,
    Target,
    _convert,
    read_cell,
)
from reep.errors import ConfigError  # noqa: E402
from reep.models import (  # noqa: E402
    Bridge,
    IdState,
    IdStatus,
    ProviderId,
    Resolution,
    ResolutionStatus,
)

P1 = "rp00000000000001"
P2 = "rp00000000000002"
COACH = "rc00000000000001"
TM = ProviderId("transfermarkt", "spieler", "1")
TM2 = ProviderId("transfermarkt", "spieler", "2")


def _table(**changes: Any) -> Any:
    entry: dict[str, Any] = {
        "table": "players",
        "entity_type": "player",
        "primary_key": "id",
        "reep_id": "reep_id",
        "status": "reep_status",
        "provider_ids": {"transfermarkt/spieler": "tm_id", "fbref/person": "fbref_id"},
    }
    entry.update(changes)
    return parse_config({"version": 1, "tables": {"t": entry}}).tables[0]


def _planner(kind: ColumnKind | None = None, **flags: Any) -> Planner:
    table = _table()
    targets = [Target("reep_id"), Target("fbref_id", table.provider_ids[1])]
    kinds = {"tm_id": ColumnKind("text"), "fbref_id": kind or ColumnKind("text", 32)}
    return Planner(table, targets, kinds, **flags)


def _row(**values: Any) -> Row:
    cells = {
        c: read_cell(values.get(c))
        for c in ("id", "tm_id", "fbref_id", "reep_id", "reep_status")
    }
    return Row(pk=(values.get("id", 1),), cells=cells)


def _found(pid: ProviderId, rid: str, upstream: str | None = None) -> Resolution:
    return Resolution(
        query=pid,
        status=ResolutionStatus.FOUND,
        reep_id=rid,
        entity_type="player",
        entity_status="active",
        bridge=Bridge(provider_id=pid, reep_id=rid, upstream_status=upstream),
    )


def _plan(
    planner: Planner,
    row: Row,
    resolutions: dict[ProviderId, Resolution] | None = None,
    states: dict[str, IdState] | None = None,
    bridges: dict[str, dict[str, list[Bridge]]] | None = None,
) -> dict[str, Any]:
    res = resolutions or {}
    st = states or {}
    ident = planner.identity(row, res, st)
    plans = planner.plan(row, ident, res, st, bridges or {})
    return {p.target.name: p for p in plans}


def _fbref(rid: str, external_id: str, upstream: str | None = None) -> Bridge:
    return Bridge(
        provider_id=ProviderId("fbref", "person", external_id),
        reep_id=rid,
        upstream_status=upstream,
    )


def test_outcome_sets() -> None:
    assert set(REVIEW_OUTCOMES) < set(OUTCOMES)
    assert {"new", "repair", "unchanged", "retired"}.isdisjoint(REVIEW_OUTCOMES)
    assert max(len(s) for s in STATUS_VALUES) <= 16


@pytest.mark.parametrize(
    ("value", "text", "invalid"),
    [
        (None, None, False),
        ("  568177 ", "568177", False),
        ("", "", False),
        (568177, "568177", False),
        (decimal.Decimal("568177"), "568177", False),
        (decimal.Decimal("1.5"), "1.5", True),
        (decimal.Decimal("NaN"), "NaN", True),
        (568177.0, "568177.0", True),
        (True, "True", True),
        (uuid.UUID(int=1), str(uuid.UUID(int=1)), False),
        (b" abc ", "abc", False),
        (b"\xff", "b'\\xff'", True),
        (object, repr(object), True),
    ],
)
def test_read_cell(value: object, text: str | None, invalid: bool) -> None:
    cell = read_cell(value)
    assert (cell.text, cell.invalid) == (text, invalid)
    assert cell.raw is value


def test_convert() -> None:
    integer = ColumnKind("integer", max_int=2**31 - 1)
    assert _convert("568177", integer) == 568177
    assert _convert("p1", integer) is not None
    assert not isinstance(_convert("p1", integer), int)
    assert not isinstance(_convert(str(2**31), integer), int)
    assert not isinstance(_convert("١٢", integer), int)  # non-ASCII digits
    assert _convert(str(uuid.UUID(int=5)), ColumnKind("uuid")) == uuid.UUID(int=5)
    assert not isinstance(_convert("x", ColumnKind("uuid")), uuid.UUID)
    assert _convert("abc", ColumnKind("text", 3)) == "abc"
    assert _convert("abcd", ColumnKind("text", 3)) != "abcd"
    assert _convert("abcd", ColumnKind("text")) == "abcd"


def test_provider_target_with_two_live_ids_is_ambiguous() -> None:
    planner = _planner()
    plans = _plan(
        planner,
        _row(tm_id="1"),
        {TM: _found(TM, P1)},
        bridges={"fbref/person": {P1: [_fbref(P1, "a"), _fbref(P1, "b")]}},
    )
    assert plans["fbref/person"].outcome == "ambiguous"
    assert plans["fbref/person"].write is None
    assert plans["reep"].outcome == "new"


def test_provider_target_removed_upstream() -> None:
    plans = _plan(
        _planner(),
        _row(tm_id="1"),
        {TM: _found(TM, P1)},
        bridges={"fbref/person": {P1: [_fbref(P1, "a", "removed")]}},
    )
    assert plans["fbref/person"].outcome == "upstream_removed"


def test_provider_target_column_mismatch() -> None:
    planner = _planner(ColumnKind("integer", max_int=100))
    plans = _plan(
        planner,
        _row(tm_id="1"),
        {TM: _found(TM, P1)},
        bridges={"fbref/person": {P1: [_fbref(P1, "dc7f8a28")]}},
    )
    assert plans["fbref/person"].outcome == "column_mismatch"
    ok = _plan(
        planner,
        _row(tm_id="1"),
        {TM: _found(TM, P1)},
        bridges={"fbref/person": {P1: [_fbref(P1, "42")]}},
    )
    assert ok["fbref/person"].write is not None
    assert ok["fbref/person"].write.value == 42


def test_filled_provider_cells() -> None:
    fb = ProviderId("fbref", "person", "x")
    base = {TM: _found(TM, P1)}
    withdrawn = Resolution(
        query=fb, status=ResolutionStatus.WITHDRAWN, withdrawn_reason="withheld"
    )
    ambiguous = Resolution(
        query=fb, status=ResolutionStatus.AMBIGUOUS, candidates=(P1, P2)
    )
    planner = _planner()
    plans = _plan(planner, _row(tm_id="1", fbref_id="x"), {**base, fb: withdrawn})
    assert plans["fbref/person"].outcome == "withdrawn"
    plans = _plan(planner, _row(tm_id="1", fbref_id="x"), {**base, fb: ambiguous})
    assert plans["fbref/person"].outcome == "ambiguous"
    plans = _plan(planner, _row(tm_id="1", fbref_id=""), base)
    assert plans["fbref/person"].outcome == "invalid_value"
    coach = Resolution(
        query=fb, status=ResolutionStatus.FOUND, reep_id=COACH, entity_type="coach"
    )
    plans = _plan(planner, _row(fbref_id="x"), {fb: coach})
    assert plans["fbref/person"].outcome == "type_mismatch"


def test_reep_target_ambiguous_and_invalid_sources() -> None:
    planner = _planner()
    ambiguous = Resolution(
        query=TM, status=ResolutionStatus.AMBIGUOUS, candidates=(P1, P2)
    )
    plans = _plan(planner, _row(tm_id="1"), {TM: ambiguous})
    assert plans["reep"].outcome == "ambiguous"
    assert P2 in (plans["reep"].detail or "")
    plans = _plan(planner, _row(tm_id=1.5))
    assert plans["reep"].outcome == "invalid_value"
    assert plans["reep"].detail == "not an ID in tm_id"


def test_two_sources_agree() -> None:
    planner = Planner(
        _table(provider_ids={"transfermarkt/spieler": "tm_id", "opta/person": "x"}),
        [Target("reep_id")],
        {"tm_id": ColumnKind("text"), "x": ColumnKind("text")},
    )
    opta = ProviderId("opta", "person", "p1")
    cells = {
        "id": read_cell(1),
        "tm_id": read_cell("1"),
        "x": read_cell("p1"),
        "reep_id": read_cell(None),
        "reep_status": read_cell(None),
    }
    row = Row(pk=(1,), cells=cells)
    res = {TM: _found(TM, P1), opta: _found(opta, P1)}
    ident = planner.identity(row, res, {})
    assert ident.reep_id == P1
    assert ident.conflict is None


def test_status_write_only_when_it_changes() -> None:
    planner = _planner()
    state = IdState(reep_id=P1, status=IdStatus.ACTIVE, current_id=P1)
    plans = _plan(
        planner,
        _row(reep_id=P1, reep_status="resolved"),
        states={P1: state},
    )
    assert plans["reep"].outcome == "unchanged"
    assert plans["reep"].status_write is None
    plans = _plan(planner, _row(reep_id=P1, reep_status="old"), states={P1: state})
    assert plans["reep"].status_write is not None
    assert plans["reep"].status_write.old == "old"


def test_planner_columns_skip_repeats() -> None:
    table = _table(primary_key="tm_id")
    planner = Planner(table, [Target("reep_id")], {})
    assert planner.columns == ["tm_id", "reep_id", "reep_status", "fbref_id"]
    provider_only = Planner(table, [Target("fbref_id", table.provider_ids[1])], {})
    assert "reep_status" not in provider_only.columns


def test_cell_defaults() -> None:
    assert Cell(None, None).invalid is False


# -- database helpers ------------------------------------------------------------


def test_masked_url() -> None:
    assert masked_url("postgresql://u:pw@h/db") == "postgresql://u:***@h/db"
    assert masked_url("not a url") == "(a URL that is not valid)"


def test_make_engine_driver_fallbacks() -> None:
    engine, shown = make_engine("postgres://u:pw@h/db")
    assert engine.url.drivername == "postgresql"
    assert "pw" not in shown
    engine, _ = make_engine("mysql://u:pw@h/db")
    assert engine.url.drivername == "mysql+pymysql"  # MySQLdb is not installed
    engine, _ = make_engine("mssql://u:pw@h/db")
    assert engine.url.drivername == "mssql+pymssql"  # pyodbc is not installed
    engine, _ = make_engine("sqlite://")
    assert engine.url.drivername == "sqlite"


def test_make_engine_errors() -> None:
    with pytest.raises(ConfigError, match="not valid"):
        make_engine("::nope::")
    with pytest.raises(ConfigError, match="No database driver") as info:
        make_engine("oracle+nodriver://u:pw@h/db")
    assert "pw" not in str(info.value)


@pytest.mark.parametrize(
    ("type_", "kind"),
    [
        (sa.String(16), ColumnKind("text", 16)),
        (sa.Text(), ColumnKind("text")),
        (sa.Integer(), ColumnKind("integer", max_int=2**31 - 1)),
        (sa.BigInteger(), ColumnKind("integer", max_int=2**63 - 1)),
        (sa.SmallInteger(), ColumnKind("integer", max_int=2**15 - 1)),
        (sa.Uuid(), ColumnKind("uuid")),
        (sa.Float(), None),
        (sa.Numeric(10, 0), None),
        (sa.Boolean(), None),
        (sa.Enum("a", "b"), None),
        (sa.Date(), None),
    ],
)
def test_column_kind(type_: Any, kind: ColumnKind | None) -> None:
    assert column_kind(type_) == kind
