from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
import pyarrow as pa
import pytest

from reep import Register
from reep.config import Settings
from reep.errors import (
    CatalogMissingError,
    InvalidArgumentError,
    InvalidProviderIdError,
    InvalidReepIdError,
    NotFoundError,
    UnknownNamespaceError,
    UnknownProviderError,
    UnsupportedReleaseError,
)
from reep.models import IdStatus, ProviderId, ResolutionStatus
from tests.support.fixture_release import (
    CHELSEA,
    CHELSEA_WOMEN,
    DECORROBORATED,
    MARESCA,
    MERGED,
    MERGED_TO_RETIRED,
    NAMESAKE,
    OLD_SEASON,
    OZIL,
    PALMER,
    RETIRED,
    STAMP_1,
    UNKNOWN,
    UNPUBLISHED,
    WITHHELD,
    FixtureBucket,
)


def test_release_info(reg: Register) -> None:
    info = reg.release
    assert info.stamp == STAMP_1
    assert info.backend == "local"
    assert info.source == "download"
    assert info.profile == "slim"
    assert info.schema_version == "bridge-register-v1"
    assert "entity_search" in info.tables
    assert info.extra["sdk_store_version"] == "1"


def test_resolve_found(reg: Register) -> None:
    res = reg.resolve(ProviderId("transfermarkt", "spieler", "568177"))
    assert res.status is ResolutionStatus.FOUND
    assert res.found
    assert res.reep_id == PALMER
    assert res.entity_type == "player"
    assert res.label == "Cole Palmer"
    assert res.entity_status == "active"
    assert res.bridge is not None
    assert res.bridge.rung == "corroborated-mint"
    assert res.bridge.upstream_status is None
    assert res.redirected_from is None
    assert res.release_stamp == STAMP_1


def test_the_same_number_in_another_namespace_is_another_entity(reg: Register) -> None:
    assert reg.resolve("transfermarkt/trainer/568177").reep_id == MARESCA


def test_resolve_accepts_text_and_tuples(reg: Register) -> None:
    assert reg.resolve("transfermarkt:spieler:568177").reep_id == PALMER
    assert reg.resolve(("transfermarkt", "spieler", "568177")).reep_id == PALMER
    with pytest.raises(InvalidProviderIdError):
        reg.resolve(568177)  # type: ignore[arg-type]


def test_resolve_not_found(reg: Register) -> None:
    res = reg.resolve("transfermarkt/spieler/1")
    assert res.status is ResolutionStatus.NOT_FOUND
    assert res.reep_id is None
    assert not res.found


def test_resolve_follows_a_merge_to_the_survivor(reg: Register) -> None:
    res = reg.resolve("whoscored/player/333")
    assert res.status is ResolutionStatus.FOUND
    assert res.reep_id == PALMER
    assert res.redirected_from == MERGED
    assert res.label == "Cole Palmer"
    assert res.bridge is not None
    assert res.bridge.reep_id == MERGED


def test_resolve_a_merge_into_a_retired_survivor(reg: Register) -> None:
    res = reg.resolve("wyscout/player/777")
    assert res.reep_id == RETIRED
    assert res.redirected_from == MERGED_TO_RETIRED
    assert res.entity_status == "retired"


def test_resolve_withdrawn(reg: Register) -> None:
    res = reg.resolve("wyscout/player/444")
    assert res.status is ResolutionStatus.WITHDRAWN
    assert res.reep_id is None
    assert res.withdrawn_reason == "target_not_published"


def test_resolve_bridge_metadata(reg: Register) -> None:
    retired = reg.resolve("fotmob/person/1001")
    assert retired.bridge is not None
    assert retired.bridge.upstream_status == "retired"
    removed = reg.resolve("skillcorner/player/777")
    assert removed.reep_id == NAMESAKE
    assert removed.bridge is not None
    assert removed.bridge.upstream_status == "removed"
    no_rung = reg.resolve("transfermarkt/spieler/900001")
    assert no_rung.bridge is not None
    assert no_rung.bridge.rung is None


def test_resolve_many_keeps_the_order(reg: Register) -> None:
    results = reg.resolve_many(
        [
            "transfermarkt/spieler/1",
            "transfermarkt/spieler/568177",
            "transfermarkt/trainer/568177",
            "transfermarkt/spieler/568177",
        ]
    )
    assert [r.reep_id for r in results] == [None, PALMER, MARESCA, PALMER]
    assert reg.resolve_many([]) == []


def test_unknown_namespace_names_the_valid_ones(reg: Register) -> None:
    with pytest.raises(UnknownNamespaceError) as exc:
        reg.resolve("transfermarkt/player/568177")
    assert "Did you mean 'spieler'" in exc.value.message
    assert exc.value.hint is not None
    assert "spieler" in exc.value.hint
    assert "trainer" in exc.value.hint


def test_unknown_provider(reg: Register) -> None:
    with pytest.raises(UnknownProviderError, match="transfermarket"):
        reg.resolve("transfermarket/spieler/568177")
    with pytest.raises(UnknownProviderError):
        reg.namespaces("nope")


def test_namespaces_and_providers(reg: Register) -> None:
    assert reg.namespaces("Transfermarkt") == [
        "schiedsrichter",
        "spieler",
        "trainer",
        "verein",
    ]
    pairs = {(p.provider, p.namespace): p for p in reg.providers()}
    assert pairs["transfermarkt", "spieler"].bridge_count == 3
    assert pairs["opta", "person"].entity_types == ("coach", "player")


@pytest.mark.parametrize(
    ("reep_id", "status", "current", "reason"),
    [
        (PALMER, IdStatus.ACTIVE, PALMER, None),
        (OLD_SEASON, IdStatus.RETIRED, OLD_SEASON, None),
        (MERGED, IdStatus.REDIRECTED, PALMER, "merged"),
        (MERGED_TO_RETIRED, IdStatus.REDIRECTED, RETIRED, "merged"),
        (UNPUBLISHED, IdStatus.WITHDRAWN, None, "target_not_published"),
        (DECORROBORATED, IdStatus.WITHDRAWN, None, "de_corroborated"),
        (WITHHELD, IdStatus.WITHDRAWN, None, "withheld"),
        (UNKNOWN, IdStatus.UNKNOWN, None, None),
    ],
)
def test_current(
    reg: Register,
    reep_id: str,
    status: IdStatus,
    current: str | None,
    reason: str | None,
) -> None:
    state = reg.current(reep_id)
    assert state.status is status
    assert state.current_id == current
    assert state.reason == reason
    assert state.entity_type == "player" or state.entity_type == "season"


def test_current_states_of_redirected_ids(reg: Register) -> None:
    state = reg.current(MERGED_TO_RETIRED)
    assert state.current_status == "retired"
    assert reg.current(MERGED).label == "Cole Palmer"


def test_current_many_and_validation(reg: Register) -> None:
    states = reg.current_many([PALMER, UNKNOWN, PALMER])
    assert [s.status for s in states] == [
        IdStatus.ACTIVE,
        IdStatus.UNKNOWN,
        IdStatus.ACTIVE,
    ]
    with pytest.raises(InvalidReepIdError):
        reg.current("568177")


def test_translate(reg: Register) -> None:
    result = reg.translate("transfermarkt/spieler/568177", "fbref")
    assert [str(b.provider_id) for b in result.targets] == ["fbref/person/dc7f8a28"]
    assert result.resolution.reep_id == PALMER
    assert result.to == "fbref"


def test_translate_lists_live_ids_first(reg: Register) -> None:
    result = reg.translate("transfermarkt/spieler/568177", "fotmob")
    assert [b.provider_id.external_id for b in result.targets] == ["1002", "1001"]
    assert result.targets[1].upstream_status == "retired"


def test_translate_through_a_merge(reg: Register) -> None:
    result = reg.translate("whoscored/player/333", "transfermarkt", namespace="spieler")
    assert [b.provider_id.external_id for b in result.targets] == ["568177"]


def test_translate_without_a_match(reg: Register) -> None:
    result = reg.translate("transfermarkt/spieler/1", "fbref")
    assert result.targets == ()
    assert result.resolution.status is ResolutionStatus.NOT_FOUND
    with pytest.raises(UnknownProviderError):
        reg.translate("transfermarkt/spieler/568177", "nope")
    with pytest.raises(UnknownNamespaceError):
        reg.translate("transfermarkt/spieler/568177", "fbref", namespace="player")


def test_entity(reg: Register) -> None:
    entity = reg.entity(PALMER)
    assert entity.entity_type == "player"
    assert entity.gender == "men"
    assert entity.country == "England"
    assert entity.corroboration_grade == "B"
    assert entity.corroboration_count == 2
    assert entity.aliases == ("Cole Jermaine Palmer", "Палмер")
    assert {str(b.provider_id) for b in entity.bridges} >= {
        "transfermarkt/spieler/568177",
        "fbref/person/dc7f8a28",
    }
    assert entity.redirected_from is None


def test_entity_follows_redirects(reg: Register) -> None:
    survivor = reg.entity(MERGED)
    assert survivor.reep_id == PALMER
    assert survivor.redirected_from == MERGED
    itself = reg.entity(MERGED, follow_redirects=False)
    assert itself.reep_id == MERGED
    assert itself.status == "redirected"


def test_entity_not_found(reg: Register) -> None:
    with pytest.raises(NotFoundError):
        reg.entity(UNKNOWN)


def test_search(reg: Register) -> None:
    matches = reg.search("cole palmer")
    assert [m.reep_id for m in matches[:2]] == [PALMER, "rp00000000000002"]
    assert matches[0].rank == 0
    assert matches[0].bridge_count > matches[1].bridge_count
    assert matches[0].gender == "men"


def test_search_ignores_accents_and_uses_aliases(reg: Register) -> None:
    assert [m.reep_id for m in reg.search("Ozil")] == [OZIL]
    assert reg.search("jermaine")[0].reep_id == PALMER
    assert reg.search("jermaine")[0].rank == 3


def test_search_filters_and_pages(reg: Register) -> None:
    teams = reg.search("chelsea", entity_type="team")
    assert {m.reep_id for m in teams} == {CHELSEA, CHELSEA_WOMEN}
    assert {m.gender for m in teams} == {"men", "women"}
    assert reg.search("chelsea", entity_type="match")[0].rank == 1
    first = reg.search("chelsea", limit=1)
    second = reg.search("chelsea", limit=1, offset=1)
    assert first[0].reep_id != second[0].reep_id
    assert reg.search("zzzz") == []


def test_search_labels_with_quotes_and_commas(reg: Register) -> None:
    assert reg.search("nerazzurri")[0].label == 'Inter, "Nerazzurri"'


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": "a"}, "2 characters"),
        ({"query": "ab", "entity_type": "club"}, "entity type"),
        ({"query": "ab", "limit": 0}, "limit"),
        ({"query": "ab", "offset": -1}, "offset"),
    ],
)
def test_search_validation(
    reg: Register, kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(InvalidArgumentError, match=message):
        reg.search(**kwargs)  # type: ignore[arg-type]


def test_resolve_frame_pandas(reg: Register) -> None:
    df = pd.DataFrame({"tm": [568177, 1, None, 900001], "name": list("abcd")})
    df["tm"] = df["tm"].astype("Int64")
    out = reg.resolve_frame(
        df, provider="transfermarkt", namespace="spieler", column="tm"
    )
    assert list(out.columns) == ["tm", "name", "reep_id", "reep_status"]
    assert out["reep_id"].tolist()[0] == PALMER
    statuses = [None if pd.isna(v) else v for v in out["reep_status"].tolist()]
    assert statuses == ["found", "not_found", None, "found"]
    assert list(df.columns) == ["tm", "name"]


def test_resolve_frame_polars_and_arrow(reg: Register) -> None:
    frame = pl.DataFrame({"id": ["568177", " 900001 ", None, "333"]})
    out = reg.resolve_frame(
        frame, provider="transfermarkt", namespace="spieler", column="id"
    )
    assert out["reep_id"].to_list() == [PALMER, NAMESAKE, None, None]
    table = pa.table({"id": ["333", "444"]})
    result = reg.resolve_frame(
        table, provider="whoscored", namespace="player", column="id", into="rid"
    )
    assert result.column("rid").to_pylist() == [PALMER, None]
    assert result.column("reep_status").to_pylist() == ["found", "not_found"]
    withdrawn = reg.resolve_frame(
        pa.table({"id": [444]}), provider="wyscout", namespace="player", column="id"
    )
    assert withdrawn.column("reep_status").to_pylist() == ["withdrawn"]


def test_resolve_frame_errors(reg: Register) -> None:
    df = pd.DataFrame({"tm": [568177.0, 1.0]})
    with pytest.raises(InvalidProviderIdError, match="floating-point"):
        reg.resolve_frame(
            df, provider="transfermarkt", namespace="spieler", column="tm"
        )
    with pytest.raises(InvalidArgumentError, match="no column"):
        reg.resolve_frame(df, provider="transfermarkt", namespace="spieler", column="x")
    with pytest.raises(InvalidArgumentError, match="already has"):
        reg.resolve_frame(
            df, provider="transfermarkt", namespace="spieler", column="tm", into="tm"
        )
    with pytest.raises(InvalidArgumentError, match="does not accept"):
        reg.resolve_frame(
            [1, 2], provider="transfermarkt", namespace="spieler", column="tm"
        )
    with pytest.raises(UnknownNamespaceError):
        reg.resolve_frame(df, provider="transfermarkt", namespace="player", column="tm")
    with pytest.raises(InvalidArgumentError, match="booleans"):
        reg.resolve_frame(
            pd.DataFrame({"x": [True]}),
            provider="transfermarkt",
            namespace="spieler",
            column="x",
        )
    with pytest.raises(InvalidArgumentError, match="holds"):
        reg.resolve_frame(
            pd.DataFrame({"x": [[1]]}),
            provider="transfermarkt",
            namespace="spieler",
            column="x",
        )


def test_shipped_duckdb_file_gives_the_same_answers(
    reg: Register,
    bucket: FixtureBucket,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shipped = bucket.release_dir(STAMP_1) / "duckdb" / "reep-register-v1.duckdb"
    monkeypatch.setenv("REEP_DUCKDB_PATH", str(shipped))
    monkeypatch.setenv("REEP_CACHE_DIR", str(tmp_path))
    with Register.local() as shipped_reg:
        info = shipped_reg.release
        assert info.source == "shipped_duckdb"
        assert info.stamp == STAMP_1
        assert info.profile is None
        assert info.bridge_attributes_stamp == STAMP_1
        for query in (
            "transfermarkt/spieler/568177",
            "whoscored/player/333",
            "wyscout/player/444",
            "transfermarkt/spieler/1",
        ):
            assert shipped_reg.resolve(query) == reg.resolve(query)
        for rid in (PALMER, MERGED, UNPUBLISHED, UNKNOWN):
            assert shipped_reg.current(rid) == reg.current(rid)
        assert shipped_reg.entity(PALMER) == reg.entity(PALMER)
        assert shipped_reg.search("cole palmer") == reg.search("cole palmer")
        assert shipped_reg.providers() == reg.providers()


def test_register_local_with_an_explicit_path(store_s1: Settings) -> None:
    with Register.local(store_s1.catalog_path) as reg:
        assert reg.release.stamp == STAMP_1
        assert reg.backend.name == "local"


def test_missing_catalogue(tmp_path: Path) -> None:
    settings = Settings(
        api_key=None, duckdb_path=None, cache_dir=tmp_path, config_dir=tmp_path
    )
    with pytest.raises(CatalogMissingError) as exc:
        Register.local(settings=settings)
    assert exc.value.hint is not None
    assert "reep catalog sync" in exc.value.hint
    assert exc.value.exit_code == 7


def test_a_duckdb_file_that_is_not_a_store(tmp_path: Path) -> None:
    import duckdb

    path = tmp_path / "other.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t (x INTEGER)")
    con.close()
    with pytest.raises(UnsupportedReleaseError, match="not a Reep store"):
        Register.local(path)


# -- Redirect chains that end without a current ID (Codex review r1) ---------


def _register_from(tmp_path: Path, state: Any) -> Register:
    from tests.conftest import make_settings
    from tests.support.fixture_release import write_release

    write_release(tmp_path / "bucket", STAMP_1, state)
    return Register.local(
        csv_dir=tmp_path / "bucket" / "releases" / STAMP_1,
        settings=make_settings(tmp_path / "cache"),
    )


def test_a_redirect_loop_has_no_current_id(tmp_path: Path) -> None:
    from tests.support.fixture_release import state_1

    state = state_1()
    for ent in state.entities:
        if ent["reep_id"] == PALMER:
            ent["status"] = "redirected"
    state.redirects.append({"from_id": PALMER, "to_id": MERGED, "reason": "merged"})
    with _register_from(tmp_path, state) as reg:
        loop = reg.current(MERGED)
        assert loop.status is IdStatus.WITHDRAWN
        assert loop.current_id is None
        assert loop.reason == "redirect_loop"
        found = reg.resolve("whoscored/player/333")
        assert found.status is ResolutionStatus.WITHDRAWN
        assert found.reep_id is None


def test_a_merge_into_an_id_without_a_survivor_is_withdrawn(tmp_path: Path) -> None:
    from tests.support.fixture_release import state_1

    state = state_1()
    state.redirects = [r for r in state.redirects if r["from_id"] != UNPUBLISHED]
    for row in state.redirects:
        if row["from_id"] == MERGED:
            row["to_id"] = UNPUBLISHED
    with _register_from(tmp_path, state) as reg:
        result = reg.current(MERGED)
        assert result.status is IdStatus.WITHDRAWN
        assert result.current_id is None


def test_a_survivor_lists_the_bridges_of_ids_merged_into_it(reg: Register) -> None:
    # WhoScored 333 still names MERGED, which merged into PALMER.
    entity = reg.entity(PALMER)
    via = [b for b in entity.bridges if b.reep_id != PALMER]
    assert [(str(b.provider_id), b.reep_id) for b in via] == [
        ("whoscored/player/333", MERGED)
    ]
    translated = reg.translate("transfermarkt/spieler/568177", to="whoscored")
    assert [b.provider_id.external_id for b in translated.targets] == ["333"]
    assert translated.targets[0].reep_id == MERGED


def test_a_bridge_to_an_unpublished_id_is_withdrawn(tmp_path: Path) -> None:
    # The bridge names an ID that the release does not publish and does not
    # redirect: no current ID, so no lookup may return it.
    import pandas as pd

    from tests.support.fixture_release import _bridge, state_1

    state = state_1()
    state.bridges.append(_bridge("orphan", "person", "u", UNKNOWN, ""))
    with _register_from(tmp_path, state) as reg:
        result = reg.resolve("orphan/person/u")
        assert result.status is ResolutionStatus.WITHDRAWN
        assert result.reep_id is None
        assert result.withdrawn_reason == "target_not_published"
        frame = reg.resolve_frame(
            pd.DataFrame({"src": ["u"]}),
            provider="orphan",
            namespace="person",
            column="src",
        )
        assert frame["reep_status"].tolist() == ["withdrawn"]
        assert frame["reep_id"].isna().all()


def test_bulk_resolution_decides_like_resolve(tmp_path: Path) -> None:
    from tests.support.fixture_release import _bridge, state_1

    state = state_1()
    state.bridges.extend(
        [
            _bridge("twodead", "person", "x", DECORROBORATED, ""),
            _bridge("twodead", "person", "x", WITHHELD, ""),
            _bridge("twodead", "person", "y", MERGED, ""),
            _bridge("twodead", "person", "y", PALMER, ""),
        ]
    )
    with _register_from(tmp_path, state) as reg:
        for external_id in ("x", "y", "z"):
            one = reg.resolve(f"twodead/person/{external_id}")
            [(rid, status)] = reg.backend.resolve_ids(
                "twodead", "person", [external_id]
            )
            assert (rid, status) == (one.reep_id, one.status.value)


def test_translate_many_and_translate_frame(reg: Register) -> None:
    results = reg.translate_many(
        ["transfermarkt/spieler/568177", "transfermarkt/spieler/1"],
        to="fbref",
    )
    assert len(results) == 2
    assert [str(t.provider_id) for t in results[0].targets] == ["fbref/person/dc7f8a28"]
    assert results[1].targets == ()

    df = pd.DataFrame({"tm": ["568177", "1", None]})
    out = reg.translate_frame(
        df,
        provider="transfermarkt",
        namespace="spieler",
        column="tm",
        to="fbref",
        reep_id_into="reep_id",
        status_into="status",
    )
    assert [None if pd.isna(v) else v for v in out["fbref_id"].tolist()] == [
        "dc7f8a28",
        None,
        None,
    ]
    assert [None if pd.isna(v) else v for v in out["reep_id"].tolist()] == [
        PALMER,
        None,
        None,
    ]
    assert [None if pd.isna(v) else v for v in out["status"].tolist()] == [
        "found",
        "not_found",
        None,
    ]


def test_entities_many_and_namespace_inference(reg: Register) -> None:
    entities = reg.entities_many([PALMER, MERGED, UNKNOWN])
    assert entities[0] is not None
    assert entities[0].reep_id == PALMER
    assert entities[1] is not None
    assert entities[1].reep_id == PALMER
    assert entities[1].redirected_from == MERGED
    assert entities[2] is None

    # Single-namespace provider infers automatically:
    assert reg.resolve("whoscored/333").reep_id == PALMER
    assert reg.resolve(("whoscored", "333")).reep_id == PALMER
    # Multi-namespace provider infers when entity_type disambiguates:
    assert reg.resolve("transfermarkt/568177", entity_type="player").reep_id == PALMER
    assert reg.resolve("transfermarkt/568177", entity_type="coach").reep_id == MARESCA
    with pytest.raises(InvalidProviderIdError, match="three parts"):
        reg.resolve("transfermarkt/568177")

    df = pd.DataFrame({"tm": ["568177"]})
    out = reg.resolve_frame(
        df, provider="transfermarkt", entity_type="player", column="tm"
    )
    assert out["reep_id"].tolist() == [PALMER]

