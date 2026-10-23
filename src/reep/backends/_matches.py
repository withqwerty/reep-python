"""Parameter translation and validation for the hosted match-page contract."""

from __future__ import annotations

import re
from datetime import date as calendar_date
from typing import Any

from reep.errors import ApiError, InvalidArgumentError
from reep.models import ENTITY_TYPE_BY_LETTER

_ID = re.compile(r"(?:r[a-z][0-9a-f]{14}|[a-z]_[0-9a-f]{16})\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
# Legacy canonical prefixes from v4 CANONICAL_PREFIX_TO_ENTITY_TYPES.
# Stage is r_, not g_; competition is l_, not the person/coach prefix c_.
_LEGACY_PREFIX = {
    "match": "m_",
    "team": "t_",
    "competition": "l_",
    "season": "s_",
    "stage": "r_",
}


def _typed_id(value: object, entity_type: str) -> bool:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        return False
    if value[1] == "_":
        return value[:2] == _LEGACY_PREFIX[entity_type]
    return ENTITY_TYPE_BY_LETTER.get(value[1]) == entity_type


def contract_error(detail: str) -> ApiError:
    """Describe a successful HTTP response that violates the match contract."""
    return ApiError(
        f"Invalid /matches response: {detail}.",
        status=200,
        api_code="invalid_match_response",
    )


def positive_int(value: int, name: str, *, zero: bool = False) -> None:
    """Reject non-integer or out-of-range pagination arguments."""
    if type(value) is not int or value < (0 if zero else 1):
        raise InvalidArgumentError(f"{name} must be an integer >= {0 if zero else 1}.")


def _date(value: str) -> bool:
    try:
        return calendar_date.fromisoformat(value).isoformat() == value
    except (ValueError, TypeError):
        return False


def _bounded_string(value: object, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def parameters(
    *,
    season_id: str | None,
    competition_id: str | None,
    team_id: str | None,
    home_team_id: str | None,
    away_team_id: str | None,
    venue_id: str | None,
    date: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
    cursor: str | None,
) -> dict[str, str]:
    """Translate the established SDK keyword names without dropping filters."""
    positive_int(limit, "limit")
    if limit > 100:
        raise InvalidArgumentError("matches_page limit must be <= 100.")
    if venue_id is not None:
        raise InvalidArgumentError(
            "The API does not support a venue ID filter.",
            hint="Use home_team_id or away_team_id for the home/away team filter.",
        )
    if home_team_id is not None and home_team_id == away_team_id:
        raise InvalidArgumentError("Home and away teams must differ.")
    side = home_team_id if home_team_id is not None else away_team_id
    if (
        side is not None
        and team_id is not None
        and team_id not in (home_team_id, away_team_id)
    ):
        raise InvalidArgumentError("team_id conflicts with the home/away filters.")
    team = side if side is not None else team_id
    for name, value in {
        "season_id": season_id,
        "competition_id": competition_id,
        "team_id": team,
        "away_team_id": away_team_id,
    }.items():
        if value is not None and not _bounded_string(value, 512):
            raise InvalidArgumentError(f"{name} must be a non-empty ID string.")
    if not any(value is not None for value in (season_id, competition_id, team)):
        raise InvalidArgumentError("A team, competition or season anchor is required.")
    if date is not None:
        if any(value is not None and value != date for value in (date_from, date_to)):
            raise InvalidArgumentError("date conflicts with date_from/date_to.")
        date_from = date_to = date
    for value in (date_from, date_to):
        if value is not None and not _date(value):
            raise InvalidArgumentError("Dates must use a valid YYYY-MM-DD value.")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise InvalidArgumentError("date_from must not be after date_to.")
    if cursor is not None and not _bounded_string(cursor, 2048):
        raise InvalidArgumentError("cursor must be a non-empty cursor string.")
    values = {
        "season": season_id,
        "competition": competition_id,
        "team": team,
        "venue": "home" if home_team_id is not None else "away" if side else None,
        "opponent": away_team_id if home_team_id is not None else None,
        "from": date_from,
        "to": date_to,
        "limit": str(limit),
        "cursor": cursor,
    }
    return {key: value for key, value in values.items() if value is not None}


def validate_page(body: dict[str, Any], limit: int) -> dict[str, Any]:
    """Validate the entire page; malformed rows are never discarded as misses."""
    generation = body.get("generation")
    if not isinstance(generation, str) or not _HASH.fullmatch(generation):
        raise contract_error("missing or invalid generation")
    rows = body.get("matches")
    if not isinstance(rows, list) or len(rows) > limit:
        raise contract_error("missing or invalid matches array")
    if type(body.get("count")) is not int or body["count"] != len(rows):
        raise contract_error("count does not describe the returned page")
    unresolved = body.get("unresolved")
    if not isinstance(unresolved, list) or any(
        not isinstance(value, str) or not value for value in unresolved
    ):
        raise contract_error("invalid unresolved anchors")
    cursor = body.get("next_cursor")
    if "next_cursor" not in body or (
        cursor is not None
        and (not isinstance(cursor, str) or not cursor or len(cursor) > 2048)
    ):
        raise contract_error("missing or invalid next_cursor")
    if (cursor and not rows) or (unresolved and (rows or cursor)):
        raise contract_error("contradictory empty/unresolved page")
    seen: set[str] = set()
    for row in rows:
        _validate_row(row, generation)
        if row["reep_id"] in seen:
            raise contract_error("duplicate match ID")
        seen.add(row["reep_id"])
    return body


def _validate_row(row: Any, generation: str) -> None:
    if not isinstance(row, dict) or row.get("generation") != generation:
        raise contract_error("match row has missing or different generation")
    if not _typed_id(row.get("reep_id"), "match"):
        raise contract_error("invalid match ID")
    for key, entity_type in {
        "home": "team",
        "away": "team",
        "competition": "competition",
        "season": "season",
        "stage": "stage",
    }.items():
        if key not in row:
            raise contract_error(f"missing {key}")
        value = row[key]
        if value is not None and (
            not isinstance(value, dict)
            or not _typed_id(value.get("reep_id"), entity_type)
            or "label" not in value
            or (value["label"] is not None and not isinstance(value["label"], str))
        ):
            raise contract_error(f"invalid {key}")
    for key in ("home_score", "away_score", "shootout_home", "shootout_away"):
        if key not in row or (
            row[key] is not None and (type(row[key]) is not int or row[key] < 0)
        ):
            raise contract_error(f"invalid {key}")
    for key in ("state", "result_form", "score_basis", "leg", "aggregate_of"):
        if key not in row or (row[key] is not None and not isinstance(row[key], str)):
            raise contract_error(f"invalid {key}")
    if "played_on" not in row or (
        row["played_on"] is not None and not _date(row["played_on"])
    ):
        raise contract_error("invalid played_on")
    bridges = row.get("bridges")
    if not isinstance(bridges, list) or any(
        not isinstance(bridge, dict)
        or any(
            not isinstance(bridge.get(key), str) or not bridge[key]
            for key in ("provider", "namespace", "external_id")
        )
        for bridge in bridges
    ):
        raise contract_error("invalid match bridges")
