from __future__ import annotations

import itertools
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pytest

from reep import Register
from reep.backends.api import ApiBackend
from reep.config import Settings
from reep.errors import (
    ApiError,
    AuthError,
    InvalidArgumentError,
    InvalidReepIdError,
    NetworkError,
    NotFoundError,
    QuotaExceededError,
    RateLimitError,
    UnknownProviderError,
)
from reep.models import IdStatus, ProviderId, ResolutionStatus
from tests.conftest import make_settings, no_sleep
from tests.support.fake_api import BASE, EXPIRED_KEY, OVERLAY_TRIPLE, VALID_KEY, FakeApi
from tests.support.fixture_release import (
    CHELSEA,
    DECORROBORATED,
    MARESCA,
    MERGED,
    MERGED_TO_RETIRED,
    OLD_SEASON,
    PALMER,
    RETIRED,
    STAMP_1,
    UNKNOWN,
    UNPUBLISHED,
    WITHHELD,
    filler_id,
)


@pytest.fixture
def fake() -> FakeApi:
    return FakeApi()


def _backend(fake: FakeApi, key: str = VALID_KEY, **kwargs: object) -> ApiBackend:
    return ApiBackend(key, base_url=BASE, http=fake.client(), sleep=no_sleep, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def api(fake: FakeApi) -> Iterator[Register]:
    with Register(_backend(fake)) as register:
        yield register


def test_release_info_and_providers(api: Register) -> None:
    info = api.release
    assert info.stamp == STAMP_1
    assert info.backend == "api"
    assert info.source == "api"
    assert info.schema_version == "bridge-register-v1"
    providers = {p.provider: p for p in api.providers()}
    assert "wikidata" not in providers
    assert providers["transfermarkt"].namespace is None
    assert providers["transfermarkt"].bridge_count == 7
    assert api.namespaces("transfermarkt") == []


def test_resolve(api: Register) -> None:
    res = api.resolve("transfermarkt/spieler/568177")
    assert res.status is ResolutionStatus.FOUND
    assert res.reep_id == PALMER
    assert res.label == "Cole Palmer"
    assert res.entity_status == "active"
    assert res.bridge is not None
    assert res.bridge.rung is None
    assert res.release_stamp == STAMP_1
    assert api.resolve("transfermarkt/trainer/568177").reep_id == MARESCA


def test_resolve_follows_merges(api: Register) -> None:
    res = api.resolve("whoscored/player/333")
    assert res.reep_id == PALMER
    assert res.redirected_from == MERGED
    assert res.label == "Cole Palmer"
    retired = api.resolve("wyscout/player/777")
    assert retired.reep_id == RETIRED
    assert retired.entity_status == "retired"


def test_resolve_withdrawn_not_found_and_overlay(api: Register) -> None:
    withdrawn = api.resolve("wyscout/player/444")
    assert withdrawn.status is ResolutionStatus.WITHDRAWN
    assert withdrawn.reep_id is None
    assert api.resolve("transfermarkt/spieler/1").status is ResolutionStatus.NOT_FOUND
    overlay = api.resolve(ProviderId(*OVERLAY_TRIPLE))
    assert overlay.status is ResolutionStatus.NOT_FOUND


def test_resolve_many_uses_batches_of_20(api: Register, fake: FakeApi) -> None:
    pids = [ProviderId("filler", "player", f"player-{i}") for i in range(45)]
    results = api.resolve_many(pids)
    assert [r.reep_id for r in results] == [filler_id(i) for i in range(45)]
    assert fake.batch_sizes == [20, 20, 5]


def test_ambiguous_resolution(fake: FakeApi) -> None:
    fake.index["opta", "person", "p1"].append(
        {"reep_id": MARESCA, "entity_type": "coach", "namespace": "person"}
    )
    with Register(_backend(fake)) as api:
        res = api.resolve("opta/person/p1")
    assert res.status is ResolutionStatus.AMBIGUOUS
    assert res.candidates == tuple(sorted((PALMER, MARESCA)))


@pytest.mark.parametrize(
    ("reep_id", "status", "current", "current_status"),
    [
        (PALMER, IdStatus.ACTIVE, PALMER, "active"),
        (OLD_SEASON, IdStatus.RETIRED, OLD_SEASON, "retired"),
        (MERGED, IdStatus.REDIRECTED, PALMER, "active"),
        (MERGED_TO_RETIRED, IdStatus.REDIRECTED, RETIRED, "retired"),
        (UNPUBLISHED, IdStatus.WITHDRAWN, None, None),
        (DECORROBORATED, IdStatus.UNKNOWN, None, None),
        (UNKNOWN, IdStatus.UNKNOWN, None, None),
    ],
)
def test_current(
    api: Register,
    reep_id: str,
    status: IdStatus,
    current: str | None,
    current_status: str | None,
) -> None:
    state = api.current(reep_id)
    assert state.status is status
    assert state.current_id == current
    assert state.current_status == current_status


def test_current_many_uses_batches_of_100(api: Register, fake: FakeApi) -> None:
    ids = [filler_id(i) for i in range(150)]
    states = api.current_many(ids)
    assert all(s.status is IdStatus.ACTIVE for s in states)
    assert fake.batch_sizes == [100, 50]


def test_entity_and_translate(api: Register) -> None:
    entity = api.entity(MERGED)
    assert entity.reep_id == PALMER
    assert entity.redirected_from == MERGED
    assert entity.aliases == ("Cole Jermaine Palmer", "Палмер")
    assert entity.country == "England"
    assert entity.gender is None
    assert [str(b.provider_id) for b in entity.bridges][:2] == [
        "fbref/person/dc7f8a28",
        "fotmob/person/1001",
    ]
    itself = api.entity(MERGED, follow_redirects=False)
    assert itself.status == "redirected"
    assert itself.reep_id == MERGED
    with pytest.raises(NotFoundError):
        api.entity(UNKNOWN)
    targets = api.translate("transfermarkt/spieler/568177", "fbref").targets
    assert [str(t.provider_id) for t in targets] == ["fbref/person/dc7f8a28"]


def test_search(api: Register) -> None:
    matches = api.search("cole palmer")
    assert [m.reep_id for m in matches][:1] == [PALMER]
    assert matches[0].rank == 0
    assert matches[0].status is None
    assert next(m.reep_id for m in api.search("chelsea", entity_type="team")) == CHELSEA
    assert len(api.search("filler", limit=50)) == 20
    assert len(api.search("filler", limit=5, offset=3)) == 5
    with pytest.raises(InvalidArgumentError):
        api.search("ab")


def test_key_status_changes_and_releases_with_etag(fake: FakeApi) -> None:
    backend = _backend(fake)
    assert backend.key_status()["key"]["plan"] == "evaluation"
    assert backend.changes(since=STAMP_1, limit=10)["count"] == 0
    first = backend.releases()
    second = backend.releases()
    assert first == second
    assert first["latest"] == STAMP_1
    assert backend.key_days_remaining == 30
    assert backend.key_expires_at is not None
    backend.close()


def test_resolve_frame_through_the_api(api: Register) -> None:
    df = pd.DataFrame({"tm": ["568177", "1"]})
    out = api.resolve_frame(
        df, provider="transfermarkt", namespace="spieler", column="tm"
    )
    assert out["reep_id"].tolist()[0] == PALMER


def test_register_api_needs_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("reep.auth._keyring", lambda: None)
    settings = make_settings(tmp_path)
    with pytest.raises(AuthError) as exc:
        Register.api(settings=settings)
    assert exc.value.exit_code == 5
    assert exc.value.hint is not None
    assert "reep auth login" in exc.value.hint


def test_register_api_uses_the_environment_key(tmp_path: Path, fake: FakeApi) -> None:
    settings = Settings(
        api_key=VALID_KEY,
        duckdb_path=None,
        cache_dir=tmp_path,
        config_dir=tmp_path,
        api_url=BASE,
    )
    with Register.api(settings=settings, http=fake.client()) as api:
        assert api.resolve("transfermarkt/spieler/568177").reep_id == PALMER
        assert api.backend.name == "api"


@pytest.mark.parametrize(
    ("key", "message"),
    [("wrong-key-000000000", "not valid"), (EXPIRED_KEY, "expired")],
)
def test_auth_errors(fake: FakeApi, key: str, message: str) -> None:
    with Register(_backend(fake, key)) as api, pytest.raises(AuthError, match=message):
        api.providers()


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (402, QuotaExceededError),
        (403, AuthError),
        (500, ApiError),
    ],
)
def test_http_errors(fake: FakeApi, status: int, error: type[Exception]) -> None:
    fake.fail_statuses = [status]
    with pytest.raises(error):
        _backend(fake).providers()


def test_rate_limit_retries_then_gives_up(fake: FakeApi) -> None:
    fake.fail_statuses = [429, 503]
    assert _backend(fake).providers()
    fake.fail_statuses = [429] * 5
    with pytest.raises(RateLimitError):
        _backend(fake, retries=2).providers()


def test_rate_limit_waits_for_the_reset_header(fake: FakeApi) -> None:
    delays: list[float] = []
    state = {"sent": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not state["sent"]:
            state["sent"] = True
            return httpx.Response(
                429,
                json={"error": "rate_limited"},
                headers={"X-Reep-Ratelimit-Reset": "1"},
            )
        return fake.handle(request)

    backend = ApiBackend(
        VALID_KEY,
        base_url=BASE,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=delays.append,
    )
    backend.providers()
    assert delays == [1.0]


def test_network_errors(fake: FakeApi) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused", request=request)

    backend = ApiBackend(
        VALID_KEY,
        base_url=BASE,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=no_sleep,
        retries=2,
    )
    with pytest.raises(NetworkError):
        backend.providers()
    assert calls["n"] == 3


def test_api_side_errors_are_mapped(fake: FakeApi) -> None:
    backend = _backend(fake)
    with pytest.raises(UnknownProviderError, match="nope"):
        backend.resolve_many([ProviderId("nope", "x", "1")])
    with pytest.raises(InvalidReepIdError):
        backend.current_many(["rx00000000000001"])
    with pytest.raises(NotFoundError):
        backend._get_json("/missing")


def test_expiry_warning(fake: FakeApi, caplog: pytest.LogCaptureFixture) -> None:
    fake.days_remaining = 3
    backend = _backend(fake)
    with caplog.at_level(logging.WARNING, logger="reep.backends.api"):
        backend.providers()
        backend.providers()
    warnings = [r for r in caplog.records if "expires in 3 day" in r.getMessage()]
    assert len(warnings) == 1


def test_unexpected_batch_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    backend = ApiBackend(
        VALID_KEY,
        base_url=BASE,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ApiError, match="unexpected"):
        backend.current_many([PALMER])


TRIPLES = [
    "transfermarkt/spieler/568177",
    "transfermarkt/trainer/568177",
    "whoscored/player/333",
    "wyscout/player/777",
    "wyscout/player/444",
    "transfermarkt/spieler/1",
    "fotmob/person/1001",
    "opta/team/t1",
]
IDS = [PALMER, OLD_SEASON, MERGED, MERGED_TO_RETIRED, UNPUBLISHED, UNKNOWN, CHELSEA]


def test_parity_between_local_and_api(reg: Register, api: Register) -> None:
    def resolution_view(r: object) -> tuple[object, ...]:
        return (
            r.status,  # type: ignore[attr-defined]
            r.reep_id,  # type: ignore[attr-defined]
            r.entity_type,  # type: ignore[attr-defined]
            r.label,  # type: ignore[attr-defined]
            r.entity_status,  # type: ignore[attr-defined]
            r.redirected_from,  # type: ignore[attr-defined]
        )

    for local, remote in zip(
        reg.resolve_many(TRIPLES), api.resolve_many(TRIPLES), strict=True
    ):
        assert resolution_view(local) == resolution_view(remote), local.query
    for local_state, remote_state in zip(
        reg.current_many(IDS), api.current_many(IDS), strict=True
    ):
        assert (
            local_state.status,
            local_state.current_id,
            local_state.current_status,
        ) == (
            remote_state.status,
            remote_state.current_id,
            remote_state.current_status,
        ), local_state.reep_id
        if local_state.status is not IdStatus.WITHDRAWN:
            assert local_state.label == remote_state.label
    for rid in (PALMER, MERGED, CHELSEA):
        local_entity, remote_entity = reg.entity(rid), api.entity(rid)
        assert (
            local_entity.reep_id,
            local_entity.entity_type,
            local_entity.status,
            local_entity.label,
            local_entity.aliases,
            local_entity.redirected_from,
        ) == (
            remote_entity.reep_id,
            remote_entity.entity_type,
            remote_entity.status,
            remote_entity.label,
            remote_entity.aliases,
            remote_entity.redirected_from,
        )
        # The local backend also lists the bridges that still name an ID merged
        # into the entity; the API cannot find them.
        own = [b for b in local_entity.bridges if b.reep_id == local_entity.reep_id]
        assert [b.provider_id for b in own] == [
            b.provider_id for b in remote_entity.bridges
        ]
        via = [b for b in local_entity.bridges if b.reep_id != local_entity.reep_id]
        assert all(
            reg.current(b.reep_id).current_id == local_entity.reep_id for b in via
        )
    for query in ("cole palmer", "chelsea"):
        assert [m.reep_id for m in reg.search(query)] == [
            m.reep_id for m in api.search(query)
        ]
    local_t = reg.translate("transfermarkt/spieler/568177", "fotmob")
    remote_t = api.translate("transfermarkt/spieler/568177", "fotmob")
    assert {t.provider_id for t in local_t.targets} == {
        t.provider_id for t in remote_t.targets
    }


def test_known_differences_from_the_local_backend(reg: Register, api: Register) -> None:
    """The API cannot tell removed tombstones apart from unknown IDs."""
    for rid in (DECORROBORATED, WITHHELD):
        assert reg.current(rid).status is IdStatus.WITHDRAWN
        assert api.current(rid).status is IdStatus.UNKNOWN


# -- Redirect chains through the API (Codex review r1) -----------------------
# The API answers a merged ID with its target's entity, one step only.


def test_a_chain_of_merges_ends_at_the_survivor(tmp_path: Path) -> None:
    from tests.support.fixture_release import state_1

    state = state_1()
    targets = {
        MERGED: UNPUBLISHED,
        UNPUBLISHED: MERGED_TO_RETIRED,
        MERGED_TO_RETIRED: PALMER,
    }
    for row in state.redirects:
        if row["from_id"] in targets:
            row.update(to_id=targets[row["from_id"]], reason="merged")
    fake = FakeApi(state=state)
    with Register(_backend(fake)) as api:
        current = api.current(MERGED)
        assert current.status is IdStatus.REDIRECTED
        assert current.current_id == PALMER
        assert api.resolve("whoscored/player/333").reep_id == PALMER
        entity = api.entity(MERGED)
        assert entity.reep_id == PALMER
        assert entity.redirected_from == MERGED


def test_a_redirect_loop_through_the_api_is_withdrawn() -> None:
    from tests.support.fixture_release import state_1

    state = state_1()
    for ent in state.entities:
        if ent["reep_id"] == PALMER:
            ent["status"] = "redirected"
    state.redirects.append({"from_id": PALMER, "to_id": MERGED, "reason": "merged"})
    fake = FakeApi(state=state)
    with Register(_backend(fake)) as api:
        loop = api.current(MERGED)
        assert loop.status is IdStatus.WITHDRAWN
        assert loop.reason == "redirect_loop"
        assert api.resolve("whoscored/player/333").reep_id is None


def test_two_candidates_that_merged_into_one_survivor(tmp_path: Path) -> None:
    from tests.conftest import make_settings
    from tests.support.fixture_release import state_1, write_release

    state = state_1()
    for row in state.redirects:
        if row["from_id"] == UNPUBLISHED:
            row.update(to_id=PALMER, reason="merged")
    state.bridges.append(
        {
            "provider": "whoscored",
            "namespace": "player",
            "external_id": "333",
            "reep_id": UNPUBLISHED,
            "rung": "",
            "upstream_status": "",
        }
    )
    fake = FakeApi(state=state)
    with Register(_backend(fake)) as api:
        remote = api.resolve("whoscored/player/333")
    write_release(tmp_path / "bucket", STAMP_1, state)
    with Register.local(
        csv_dir=tmp_path / "bucket" / "releases" / STAMP_1,
        settings=make_settings(tmp_path / "cache"),
    ) as local:
        here = local.resolve("whoscored/player/333")
    assert remote.status is here.status is ResolutionStatus.FOUND
    assert remote.reep_id == here.reep_id == PALMER


def _chain(hops: int) -> tuple[Any, list[str]]:
    """A release in which ``chains/person/root`` names the start of ``hops`` merges."""
    from tests.support.fixture_release import _bridge, _entity, state_1

    state = state_1()
    ids = [f"rp{0x100000 + i:014x}" for i in range(hops)] + [PALMER]
    state.entities.extend(
        _entity(rid, "player", "redirected", f"Chain {i}")
        for i, rid in enumerate(ids[:-1])
    )
    state.redirects.extend(
        {"from_id": a, "to_id": b, "reason": "merged"}
        for a, b in itertools.pairwise(ids)
    )
    state.bridges.append(_bridge("chains", "person", "root", ids[0], ""))
    return state, ids


def _local(tmp_path: Path, state: Any) -> Register:
    from tests.support.fixture_release import write_release

    write_release(tmp_path / "bucket", STAMP_1, state)
    return Register.local(
        csv_dir=tmp_path / "bucket" / "releases" / STAMP_1,
        settings=make_settings(tmp_path / "cache"),
    )


@pytest.mark.parametrize("hops", [8, 9])
def test_chain_length_limit_matches_the_local_backend(
    tmp_path: Path, hops: int
) -> None:
    state, ids = _chain(hops)
    with (
        _local(tmp_path, state) as local,
        Register(_backend(FakeApi(state=state))) as api,
    ):
        here, there = local.current(ids[0]), api.current(ids[0])
        assert (here.status, here.current_id) == (there.status, there.current_id)
        found, remote = (
            local.resolve("chains/person/root"),
            api.resolve("chains/person/root"),
        )
        assert (found.status, found.reep_id) == (remote.status, remote.reep_id)
        assert local.entity(ids[0]).reep_id == api.entity(ids[0]).reep_id


@pytest.mark.parametrize(
    ("status", "error"), [(401, AuthError), (402, QuotaExceededError), (500, ApiError)]
)
def test_an_error_inside_a_chain_is_raised(status: int, error: type[Exception]) -> None:
    state, ids = _chain(3)
    fake = FakeApi(state=state)
    original = fake._entity

    def failing(rid: str, include: Any) -> Any:
        if rid == ids[1]:
            return status, {"error": "fault", "message": "intermediate entity failed"}
        return original(rid, include)

    fake._entity = failing  # type: ignore[method-assign,assignment]
    with Register(_backend(fake)) as api, pytest.raises(error):
        api.current(ids[0])


def test_a_candidate_whose_target_is_missing_is_not_published(tmp_path: Path) -> None:
    state, ids = _chain(1)
    missing = ids[-1]
    state.entities = [e for e in state.entities if e["reep_id"] != missing]
    state.bridges = [b for b in state.bridges if b["reep_id"] != missing]
    state.aliases = [a for a in state.aliases if a["reep_id"] != missing]
    with (
        _local(tmp_path, state) as local,
        Register(_backend(FakeApi(state=state))) as api,
    ):
        here = local.resolve("chains/person/root")
        there = api.resolve("chains/person/root")
    assert there.status is here.status is ResolutionStatus.WITHDRAWN
    assert there.withdrawn_reason == here.withdrawn_reason == "target_not_published"


def test_a_409_that_is_not_an_ambiguity_is_an_error() -> None:
    fake = FakeApi()
    fake._resolve = lambda *args: (  # type: ignore[method-assign]
        409,
        {"error": "conflict", "message": "lookup index unavailable"},
    )
    with Register(_backend(fake)) as api, pytest.raises(ApiError):
        api.resolve("transfermarkt/spieler/568177")


@pytest.mark.parametrize(
    "lifecycle", ["withdrawn", "unknown", "draft", None, ["active"], False]
)
@pytest.mark.parametrize(
    "surface", ["resolve", "resolve_many", "resolve_ids", "resolve_frame", "current"]
)
def test_nonlive_api_payload_never_returns_an_id(lifecycle: Any, surface: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/providers"):
            return httpx.Response(200, json={"providers": [{"name": "p"}]})
        body: dict[str, Any] = {"reep_id": PALMER, "entity_type": "player"}
        if lifecycle is not None:
            body["lifecycle_state"] = lifecycle
        if surface != "current":
            body = {"results": [body]}
        return httpx.Response(200, json={"results": [{"status": 200, "body": body}]})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        Register(ApiBackend("test-key", http=http, retries=0)) as api,
    ):
        if surface == "current":
            state = api.current(PALMER)
            assert state.status is IdStatus.WITHDRAWN
            assert state.current_id is None
            assert state.reason == "target_not_published"
        elif surface == "resolve_ids":
            assert api.backend.resolve_ids("p", "person", ["1"]) == [
                (None, "withdrawn")
            ]
        elif surface == "resolve_frame":
            frame = api.resolve_frame(
                pd.DataFrame({"pid": ["1"]}),
                provider="p",
                namespace="person",
                column="pid",
            )
            assert frame["reep_id"].tolist() == [None]
            assert frame["reep_status"].tolist() == ["withdrawn"]
        else:
            result = (
                api.resolve("p/person/1")
                if surface == "resolve"
                else api.resolve_many(["p/person/1"])[0]
            )
            assert result.status is ResolutionStatus.WITHDRAWN
            assert result.reep_id is None
            assert result.withdrawn_reason == "target_not_published"


def test_api_translate_many_entities_many_and_telemetry(
    api: Register, fake: FakeApi
) -> None:
    results = api.translate_many(
        ["transfermarkt/spieler/568177", "transfermarkt/spieler/1"],
        to="fbref",
    )
    assert len(results) == 2
    assert [str(t.provider_id) for t in results[0].targets] == ["fbref/person/dc7f8a28"]
    assert results[1].targets == ()

    entities = api.entities_many([PALMER, MERGED, UNKNOWN])
    assert entities[0] is not None
    assert entities[0].reep_id == PALMER
    assert entities[1] is not None
    assert entities[1].reep_id == PALMER
    assert entities[1].redirected_from == MERGED
    assert entities[2] is None

    assert api.key_status()["key"]["plan"] == "evaluation"
    assert api.changes(since=STAMP_1)["count"] == 0
    assert api.releases()["latest"] == STAMP_1
    backend = api.backend
    assert isinstance(backend, ApiBackend)
    assert backend.last_call_meta["release_stamp"] == STAMP_1
    assert backend.last_call_meta["key_days_remaining"] == 30
