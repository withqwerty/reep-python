"""Opt-in cross-repository consumer tests; actual TypeScript handler, no network."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from reep import ApiError, Register
from reep.backends.api import ApiBackend

HOME = "rt00000000000000"
AWAY = "rt00000000000001"
SEASON = "rs00000000000001"


@pytest.fixture
def handler() -> Any:
    site = os.environ.get("REEP_SITE_ROOT")
    node = os.environ.get("REEP_TEST_NODE")
    if not site or not node:
        pytest.skip("set REEP_SITE_ROOT and REEP_TEST_NODE for the actual site handler")
    calls: list[dict[str, Any]] = []
    modes = ["one"]

    def transport(request: httpx.Request) -> httpx.Response:
        mode = modes.pop(0) if len(modes) > 1 else modes[0]
        run = subprocess.run(  # noqa: S603
            [
                node,
                "--import",
                str(Path(site) / "node_modules/tsx/dist/loader.mjs"),
                str(Path(__file__).parent / "support/matches_site.mts"),
            ],
            input=json.dumps({"url": str(request.url), "mode": mode}),
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
            env={
                "PATH": "/usr/bin:/bin",
                "REEP_SITE_ROOT": site,
                "REEP_TEST_LEGACY": os.environ.get("REEP_TEST_LEGACY", "0"),
            },
        )
        response = json.loads(run.stdout)
        calls.append({"url": str(request.url), **response})
        return httpx.Response(
            response["status"],
            headers=response["headers"],
            json=response["body"],
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        yield (
            Register(
                ApiBackend(
                    "synthetic-test-key",
                    base_url="https://fixture/api/v1",
                    http=client,
                    retries=0,
                )
            ),
            calls,
            modes,
        )


def test_actual_handler_one_match_and_freshness(handler: Any) -> None:
    reg, calls, _ = handler
    result = reg.matches(team_id=HOME, season_id=SEASON)
    assert len(result) == 1
    assert result[0]["home_score"] == result[0]["away_score"] == 0
    assert result[0]["played_on"] == "2026-10-23"
    assert result[0]["state"] is None
    page = reg.matches_page(team_id=HOME, season_id=SEASON)
    assert page["generation"] == page["matches"][0]["generation"]
    assert page["api_metadata"]["release_stamp"] == "2026-10-23.10"
    assert page["api_metadata"]["channel"] == (
        None if os.environ.get("REEP_TEST_LEGACY") == "1" else "api"
    )
    assert page["api_metadata"]["source_as_of"] is None
    assert all("releases/latest.json" not in call["reads"] for call in calls)


@pytest.mark.parametrize(
    ("filters", "count"),
    [
        ({"date": "2026-10-23"}, 1),
        ({"date": "2026-10-22"}, 0),
        ({"date_from": "2026-10-22", "date_to": "2026-10-23"}, 1),
        ({"date_from": "2026-10-24"}, 0),
        ({"date_to": "2026-10-22"}, 0),
        ({"home_team_id": HOME}, 1),
        ({"away_team_id": HOME}, 0),
        ({"away_team_id": AWAY}, 1),
        ({"home_team_id": HOME, "away_team_id": AWAY}, 1),
    ],
)
def test_actual_handler_filters(
    handler: Any, filters: dict[str, Any], count: int
) -> None:
    reg, _, _ = handler
    if "home_team_id" not in filters and "away_team_id" not in filters:
        filters["team_id"] = HOME
    assert len(reg.matches(season_id=SEASON, **filters)) == count


def test_actual_handler_cursor_and_offset(handler: Any) -> None:
    reg, calls, modes = handler
    modes[:] = ["many"]
    first = reg.matches_page(team_id=HOME, season_id=SEASON, limit=100)
    assert first["count"] == 100
    assert first["next_cursor"]
    second = reg.matches_page(
        team_id=HOME, season_id=SEASON, limit=100, cursor=first["next_cursor"]
    )
    assert second["count"] == 3
    assert second["next_cursor"] is None
    assert second["generation"] == first["generation"]
    rows = reg.matches(team_id=HOME, season_id=SEASON, limit=3, offset=100)
    assert rows == second["matches"]
    assert len(calls) == 4
    assert all("offset=" not in call["url"] for call in calls)


def test_actual_handler_changed_generation_never_restarts(handler: Any) -> None:
    reg, calls, modes = handler
    modes[:] = ["many", "changed"]
    with pytest.raises(ApiError) as error:
        reg.matches(team_id=HOME, season_id=SEASON, limit=3, offset=100)
    assert error.value.api_code == "stale_cursor"
    assert len(calls) == 2


def test_actual_handler_provider_namespace_and_unresolved(handler: Any) -> None:
    reg, _, _ = handler
    page = reg.matches_page(team_id="opta:team:0000", season_id=SEASON)
    # Exact triple namespace must be resolved by the real handler, not the SDK.
    assert page["unresolved"] == []
    assert page["count"] == 1
    missing = reg.matches_page(team_id="opta:person:0000", season_id=SEASON)
    assert missing["matches"] == []
    assert missing["unresolved"] == ["opta:person:0000"]


def test_actual_handler_malformed_transport_envelope_raises(handler: Any) -> None:
    reg, calls, modes = handler
    modes[:] = ["malformed"]
    with pytest.raises(ApiError) as error:
        reg.matches(team_id=HOME, season_id=SEASON)
    assert error.value.api_code == "invalid_match_response"
    assert calls[0]["status"] == 200
    assert len(calls[0]["body"]["results"]) == 1
