from __future__ import annotations

from copy import deepcopy
from typing import Any

import httpx
import pytest

from reep import ApiError, InvalidArgumentError, Register
from reep.backends.api import ApiBackend

TEAM = "rt00000000000001"
AWAY = "rt00000000000002"
SEASON = "rs00000000000001"
GENERATION = "a" * 64


def row(n: int = 1, generation: str = GENERATION) -> dict[str, Any]:
    return {
        "reep_id": f"rm{n:014x}",
        "generation": generation,
        "home": {"reep_id": TEAM, "label": "Home"},
        "away": {"reep_id": AWAY, "label": None},
        "competition": None,
        "season": {"reep_id": SEASON, "label": None},
        "stage": None,
        "played_on": "2026-10-23",
        "home_score": 0,
        "away_score": None,
        "state": None,
        "shootout_home": None,
        "shootout_away": None,
        "result_form": None,
        "score_basis": None,
        "leg": None,
        "aggregate_of": None,
        "bridges": [{"provider": "opta", "namespace": "match", "external_id": "a:b"}],
    }


def page(*, n: int = 1, cursor: str | None = None) -> dict[str, Any]:
    return {
        "matches": [row(n)],
        "generation": GENERATION,
        "count": 1,
        "unresolved": [],
        "next_cursor": cursor,
    }


def backend(
    bodies: list[Any], headers: list[dict[str, str]] | None = None
) -> tuple[ApiBackend, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        i = len(requests)
        requests.append(request)
        return httpx.Response(
            200, json=bodies[i], headers=headers[i] if headers else {}, request=request
        )

    client = httpx.Client(transport=httpx.MockTransport(transport))
    return ApiBackend("fixture", http=client, retries=0), requests


def test_list_interface_preserves_nullable_facts_and_translates_dates() -> None:
    api, requests = backend([page()])
    result = Register(api).matches(
        home_team_id=TEAM, away_team_id=AWAY, date="2026-10-23"
    )
    assert result == [row()]
    assert dict(requests[0].url.params) == {
        "team": TEAM,
        "venue": "home",
        "opponent": AWAY,
        "from": "2026-10-23",
        "to": "2026-10-23",
        "limit": "50",
    }


def test_page_keeps_cursor_unresolved_and_independent_api_metadata() -> None:
    body = {
        "matches": [],
        "count": 0,
        "generation": GENERATION,
        "unresolved": ["opta:team:missing"],
        "next_cursor": None,
    }
    api, _ = backend(
        [body, page()],
        [
            {
                "X-Reep-Channel": "api",
                "X-Reep-Release-Stamp": "2026-10-23.10",
                "X-Reep-Selection": "b" * 64,
                "X-Reep-Source-Generation": "source-17",
                "X-Reep-Source-As-Of": "2026-10-23T12:00:00Z",
            },
            {},
        ],
    )
    found = Register(api).matches_page(team_id="opta:team:missing")
    assert found["unresolved"] == ["opta:team:missing"]
    assert found["api_metadata"]["source_generation"] == "source-17"
    assert found["api_metadata"]["release_stamp"] == "2026-10-23.10"
    assert api.last_call_meta["api_metadata"] == found["api_metadata"]
    second = api.matches_page(away_team_id=AWAY)
    assert second["api_metadata"]["source_generation"] is None
    assert found["api_metadata"]["source_generation"] == "source-17"


def test_offset_walks_cursors_with_constant_filters_and_limit() -> None:
    api, requests = backend(
        [page(n=1, cursor="one"), page(n=2, cursor="two"), page(n=3)]
    )
    found = api.matches(team_id=TEAM, season_id=SEASON, limit=1, offset=2)
    assert found == [row(3)]
    assert [r.url.params.get("cursor") for r in requests] == [None, "one", "two"]
    assert all(
        r.url.params["limit"] == "3" and "offset" not in r.url.params for r in requests
    )


@pytest.mark.parametrize(
    "changed",
    ["generation", "selection", "stamp", "source", "as_of", "prepared", "channel"],
)
def test_no_mixed_identity_during_offset(changed: str) -> None:
    second = page(n=2)
    h = {
        "X-Reep-Selection": "a" * 64,
        "X-Reep-Release-Stamp": "2026-10-23.1",
        "X-Reep-Source-Generation": "source-1",
        "X-Reep-Source-As-Of": "2026-10-23T12:00:00Z",
        "X-Reep-Release-Generated-At": "2026-10-23T12:01:00Z",
        "X-Reep-Channel": "api",
    }
    h2 = dict(h)
    if changed == "generation":
        second["generation"] = "b" * 64
        second["matches"][0]["generation"] = "b" * 64
    elif changed == "channel":
        h2.pop("X-Reep-Channel")
    else:
        h2[
            {
                "selection": "X-Reep-Selection",
                "stamp": "X-Reep-Release-Stamp",
                "source": "X-Reep-Source-Generation",
                "as_of": "X-Reep-Source-As-Of",
                "prepared": "X-Reep-Release-Generated-At",
            }[changed]
        ] = "2026-10-23T13:00:00Z" if changed in ("as_of", "prepared") else "changed"
    api, _ = backend([page(cursor="next"), second], [h, h2])
    with pytest.raises(ApiError, match="identity changed"):
        api.matches(team_id=TEAM, offset=1, limit=1)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.pop("matches"),
        lambda p: p.update(matches="bad"),
        lambda p: p.update(matches=[None]),
        lambda p: p.update(matches=[{}]),
        lambda p: p.update(count=True),
        lambda p: p.update(count=0),
        lambda p: p.pop("generation"),
        lambda p: p.update(generation="bad"),
        lambda p: p.update(unresolved="bad"),
        lambda p: p.update(unresolved=["missing"]),
        lambda p: p.pop("next_cursor"),
        lambda p: p.update(next_cursor=5),
        lambda p: p["matches"][0].update(generation="b" * 64),
        lambda p: p["matches"][0].update(home_score=True),
        lambda p: p["matches"][0].update(away_score="0"),
        lambda p: p["matches"][0].update(home={}),
        lambda p: p["matches"][0].update(played_on="2026-02-30"),
        lambda p: p["matches"][0].update(bridges=[{"provider": "opta"}]),
        lambda p: p["matches"].append(deepcopy(p["matches"][0])),
    ],
)
def test_malformed_response_raises_instead_of_empty(mutation: Any) -> None:
    body = page()
    mutation(body)
    api, _ = backend([body])
    with pytest.raises(ApiError) as error:
        api.matches(team_id=TEAM)
    assert error.value.api_code == "invalid_match_response"


@pytest.mark.parametrize("body", [[], None, {}, {"results": []}])
def test_wrong_envelope_raises(body: Any) -> None:
    api, _ = backend([body])
    with pytest.raises(ApiError):
        api.matches(team_id=TEAM)


@pytest.mark.parametrize(
    "args",
    [
        {"venue_id": "rv00000000000001"},
        {"date": "2026-02-30"},
        {"date": "2026-10-23", "date_from": "2026-10-01"},
        {"date_from": "2026-10-24", "date_to": "2026-10-23"},
        {"home_team_id": TEAM, "away_team_id": TEAM},
        {"home_team_id": AWAY},
        {"limit": 0},
        {"limit": True},
        {"offset": -1},
    ],
)
def test_invalid_arguments_fail_before_transport(args: dict[str, Any]) -> None:
    api, requests = backend([])
    with pytest.raises(InvalidArgumentError):
        api.matches(team_id=TEAM, **args)
    assert requests == []


def test_empty_no_progress_and_duplicate_pages_are_errors() -> None:
    body = page(cursor="next")
    body.update(matches=[], count=0)
    api, _ = backend([body])
    with pytest.raises(ApiError, match="contradictory"):
        api.matches(team_id=TEAM)
    api, _ = backend([page(cursor="next"), page()])
    with pytest.raises(ApiError, match="duplicate match across"):
        api.matches(team_id=TEAM, offset=1)
    api, _ = backend([page(cursor="next"), page(n=2, cursor="next")])
    with pytest.raises(ApiError, match="repeated cursor"):
        api.matches(team_id=TEAM, offset=2)


def test_stale_cursor_propagates_without_restart() -> None:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(400, json={"error": "stale_cursor"}, request=request)

    api = ApiBackend(
        "fixture", http=httpx.Client(transport=httpx.MockTransport(transport))
    )
    with pytest.raises(ApiError) as error:
        api.matches_page(team_id=TEAM, cursor="expired")
    assert error.value.api_code == "stale_cursor"
    assert len(requests) == 1


def test_each_concurrent_response_owns_its_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=page(),
            headers={
                "X-Reep-Selection": request.url.params["team"],
            },
            request=request,
        )

    api = ApiBackend(
        "fixture", http=httpx.Client(transport=httpx.MockTransport(transport))
    )
    original_json = api._json

    def interleave(response: httpx.Response) -> dict[str, Any]:
        barrier.wait(timeout=5)
        return original_json(response)

    monkeypatch.setattr(api, "_json", interleave)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(api.matches_page, team_id=team) for team in [TEAM, AWAY]]
        results = [future.result(timeout=10) for future in futures]
    assert [item["api_metadata"]["selection"] for item in results] == [TEAM, AWAY]


def test_local_register_methods_raise_explicit_remote_only_error(reg: Register) -> None:
    with pytest.raises(InvalidArgumentError, match="API backend"):
        reg.matches(team_id=TEAM)
    with pytest.raises(InvalidArgumentError, match="API backend"):
        reg.matches_page(team_id=TEAM)


def test_pagination_limits_no_anchor_and_wrong_channel() -> None:
    api, requests = backend([])
    with pytest.raises(InvalidArgumentError, match="anchor"):
        api.matches()
    with pytest.raises(InvalidArgumentError, match="<= 100"):
        api.matches_page(team_id=TEAM, limit=101)
    with pytest.raises(InvalidArgumentError, match="cursor"):
        api.matches_page(team_id=TEAM, cursor="")
    assert requests == []
    api, _ = backend([page()], [{"X-Reep-Channel": "downloads"}])
    with pytest.raises(ApiError, match="API channel"):
        api.matches(team_id=TEAM)


def test_offset_past_end_and_valid_empty_page() -> None:
    empty = {
        "matches": [],
        "count": 0,
        "generation": GENERATION,
        "unresolved": [],
        "next_cursor": None,
    }
    api, _ = backend([page(), empty])
    assert api.matches(team_id=TEAM, offset=2) == []
    assert api.matches(team_id=TEAM) == []


@pytest.mark.parametrize("explicit_cursor", [False, True])
def test_unresolved_continuation_is_not_partial_success(explicit_cursor: bool) -> None:
    unresolved = {
        "matches": [],
        "count": 0,
        "generation": GENERATION,
        "unresolved": [TEAM],
        "next_cursor": None,
    }
    bodies = [unresolved] if explicit_cursor else [page(cursor="next"), unresolved]
    api, calls = backend(bodies)

    def invoke() -> None:
        if explicit_cursor:
            api.matches_page(team_id=TEAM, cursor="next")
        else:
            api.matches(team_id=TEAM, limit=2)

    with pytest.raises(ApiError, match="became unresolved") as error:
        invoke()
    assert error.value.api_code == "invalid_match_response"
    assert len(calls) == (1 if explicit_cursor else 2)


def test_initial_unresolved_and_clean_empty_terminal_remain_valid() -> None:
    unresolved = {
        "matches": [],
        "count": 0,
        "generation": GENERATION,
        "unresolved": [TEAM],
        "next_cursor": None,
    }
    api, _ = backend([unresolved, unresolved])
    assert api.matches_page(team_id=TEAM)["unresolved"] == [TEAM]
    assert api.matches(team_id=TEAM) == []
    terminal = {**unresolved, "unresolved": []}
    api, _ = backend([page(cursor="next"), terminal, terminal])
    assert api.matches(team_id=TEAM, limit=2) == [row()]
    assert api.matches_page(team_id=TEAM, cursor="next")["matches"] == []


@pytest.mark.parametrize(
    "field", ["reep_id", "home", "away", "competition", "season", "stage"]
)
@pytest.mark.parametrize("person_id", ["rp00000000000001", "p_0000000000000001"])
def test_wrong_entity_type_in_match_page_raises(field: str, person_id: str) -> None:
    body = page()
    body["matches"][0][field] = (
        person_id if field == "reep_id" else {"reep_id": person_id, "label": None}
    )
    api, _ = backend([body])
    with pytest.raises(ApiError) as error:
        api.matches_page(team_id=TEAM)
    assert error.value.api_code == "invalid_match_response"


@pytest.mark.parametrize("legacy", [False, True])
def test_match_reference_prefixes_follow_canonical_type_map(legacy: bool) -> None:
    body = page()
    modern = {
        "reep_id": "rm",
        "home": "rt",
        "away": "rt",
        "competition": "rl",
        "season": "rs",
        "stage": "rg",
    }
    old = {
        "reep_id": "m_",
        "home": "t_",
        "away": "t_",
        "competition": "l_",
        "season": "s_",
        "stage": "r_",
    }
    for index, (field, prefix) in enumerate((old if legacy else modern).items(), 1):
        rid = prefix + format(index, "016x" if legacy else "014x")
        body["matches"][0][field] = (
            rid if field == "reep_id" else {"reep_id": rid, "label": None}
        )
    api, _ = backend([body])
    assert api.matches_page(team_id=TEAM)["matches"] == body["matches"]


@pytest.mark.parametrize(("field", "prefix"), [("stage", "g_"), ("competition", "c_")])
def test_unregistered_legacy_prefix_is_not_guessed(field: str, prefix: str) -> None:
    body = page()
    body["matches"][0][field] = {"reep_id": prefix + "0" * 16, "label": None}
    api, _ = backend([body])
    with pytest.raises(ApiError) as error:
        api.matches_page(team_id=TEAM)
    assert error.value.api_code == "invalid_match_response"
