from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from reep.errors import InvalidProviderIdError, InvalidReepIdError
from reep.models import (
    Bridge,
    IdState,
    IdStatus,
    ProviderId,
    ReleaseInfo,
    Resolution,
    ResolutionStatus,
    entity_type_of,
    to_jsonable,
    validate_reep_id,
)

slugs = st.from_regex(r"[a-z0-9][a-z0-9_]{0,15}", fullmatch=True)
external_ids = st.text(min_size=1, max_size=30).map(str.strip).filter(bool)


@given(provider=slugs, namespace=slugs, external_id=external_ids)
def test_parse_round_trips_the_text_form(
    provider: str, namespace: str, external_id: str
) -> None:
    pid = ProviderId(provider, namespace, external_id)
    assert ProviderId.parse(str(pid)) == pid


@given(st.integers())
def test_a_bare_integer_is_not_an_external_id(value: int) -> None:
    with pytest.raises(InvalidProviderIdError, match="must be a string"):
        ProviderId("transfermarkt", "spieler", value)  # type: ignore[arg-type]


@given(st.integers())
def test_parse_refuses_a_bare_integer(value: int) -> None:
    with pytest.raises(InvalidProviderIdError):
        ProviderId.parse(value)


@given(provider=slugs, external_id=st.from_regex(r"[0-9]{1,9}", fullmatch=True))
def test_parse_refuses_two_parts(provider: str, external_id: str) -> None:
    with pytest.raises(InvalidProviderIdError, match="three parts"):
        ProviderId.parse(f"{provider}/{external_id}")


def test_parse_accepts_colons_and_keeps_separators_in_the_external_id() -> None:
    assert ProviderId.parse("opta:person:p1") == ProviderId("opta", "person", "p1")
    pid = ProviderId.parse("soccerdonna/spiel/synthetic:ab/c")
    assert pid.external_id == "synthetic:ab/c"


def test_provider_and_namespace_are_normalised() -> None:
    pid = ProviderId(" Transfermarkt ", "SPIELER", " 568177 ")
    assert pid == ProviderId("transfermarkt", "spieler", "568177")
    assert str(pid) == "transfermarkt/spieler/568177"
    assert pid.to_dict() == {
        "provider": "transfermarkt",
        "namespace": "spieler",
        "external_id": "568177",
    }


@pytest.mark.parametrize(
    ("provider", "namespace", "external_id", "message"),
    [
        ("", "spieler", "1", "empty"),
        ("transfermarkt", " ", "1", "empty"),
        ("transfermarkt", "spieler", "", "empty"),
        ("trans fermarkt", "spieler", "1", "slug"),
        ("transfermarkt", "spie/ler", "1", "slug"),
    ],
)
def test_invalid_parts_are_refused(
    provider: str, namespace: str, external_id: str, message: str
) -> None:
    with pytest.raises(InvalidProviderIdError, match=message):
        ProviderId(provider, namespace, external_id)


def test_invalid_provider_id_error_carries_a_hint() -> None:
    with pytest.raises(InvalidProviderIdError) as exc:
        ProviderId("transfermarkt", "spieler", 568177)  # type: ignore[arg-type]
    assert exc.value.hint is not None
    assert "Hint:" in str(exc.value)
    assert exc.value.to_dict()["code"] == "invalid_provider_id"
    assert exc.value.exit_code == 2


@pytest.mark.parametrize("value", ["rp53af22bbeaa667", " rc00cfa6784c6c23 "])
def test_valid_reep_ids(value: str) -> None:
    assert validate_reep_id(value) == value.strip()


@pytest.mark.parametrize(
    "value", ["rz53af22bbeaa667", "rp53af22bbeaa66", "RP53AF22BBEAA667", "568177", 5]
)
def test_invalid_reep_ids(value: object) -> None:
    with pytest.raises(InvalidReepIdError):
        validate_reep_id(value)


@pytest.mark.parametrize(
    ("reep_id", "entity_type"),
    [
        ("rc00000000000001", "coach"),
        ("rg00000000000001", "stage"),
        ("rl00000000000001", "competition"),
        ("rm00000000000001", "match"),
        ("ro00000000000001", "referee"),
        ("rp00000000000001", "player"),
        ("rs00000000000001", "season"),
        ("rt00000000000001", "team"),
    ],
)
def test_entity_type_of(reep_id: str, entity_type: str) -> None:
    assert entity_type_of(reep_id) == entity_type


def test_models_convert_to_json() -> None:
    pid = ProviderId("transfermarkt", "spieler", "568177")
    res = Resolution(
        query=pid,
        status=ResolutionStatus.FOUND,
        reep_id="rp00000000000001",
        bridge=Bridge(pid, "rp00000000000001", rung="name-dob"),
        candidates=("a", "b"),
    )
    data = res.to_dict()
    assert data["status"] == "found"
    assert data["query"] == pid.to_dict()
    assert data["bridge"]["rung"] == "name-dob"
    assert data["candidates"] == ["a", "b"]
    assert res.found
    json.dumps(data)
    state = IdState("rp00000000000001", IdStatus.ACTIVE).to_dict()
    assert state["status"] == "active"
    info = ReleaseInfo(
        stamp="s", schema_version=None, backend="local", source="x", path=Path("/a")
    )
    assert info.to_dict()["path"] == str(Path("/a"))
    assert to_jsonable({"k": (1, 2)}) == {"k": [1, 2]}
