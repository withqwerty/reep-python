"""JSON Schemas of the ``reep`` command's JSON output (``reep schema``)."""

from __future__ import annotations

from typing import Any

from reep.cli.output import SCHEMA_VERSION
from reep.dbsync._plan import OUTCOMES
from reep.models import ENTITY_TYPES, IdStatus, ResolutionStatus

__all__ = ["COMMANDS", "envelope_schema", "error_schema"]

_STR = {"type": "string"}
_OPT_STR = {"type": ["string", "null"]}
_OPT_INT = {"type": ["integer", "null"]}
_REEP_ID = {"type": "string", "pattern": "^r[cglmopst][0-9a-f]{14}$"}
_OPT_REEP_ID = {"type": ["string", "null"], "pattern": "^r[cglmopst][0-9a-f]{14}$"}


def _object(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
    }


PROVIDER_ID = _object({"provider": _STR, "namespace": _STR, "external_id": _STR})

BRIDGE = _object(
    {
        "provider": _STR,
        "namespace": _STR,
        "external_id": _STR,
        "reep_id": _REEP_ID,
        "rung": _OPT_STR,
        "upstream_status": {"enum": ["retired", "removed", None]},
    }
)

RESOLUTION = _object(
    {
        "query": PROVIDER_ID,
        "status": {"enum": [s.value for s in ResolutionStatus]},
        "reep_id": _OPT_REEP_ID,
        "entity_type": {"enum": [*ENTITY_TYPES, None]},
        "label": _OPT_STR,
        "entity_status": _OPT_STR,
        "bridge": {"oneOf": [BRIDGE, {"type": "null"}]},
        "redirected_from": _OPT_REEP_ID,
        "withdrawn_reason": _OPT_STR,
        "candidates": {"type": "array", "items": _REEP_ID},
        "release_stamp": _OPT_STR,
    }
)

ID_STATE = _object(
    {
        "reep_id": _REEP_ID,
        "status": {"enum": [s.value for s in IdStatus]},
        "current_id": _OPT_REEP_ID,
        "current_status": _OPT_STR,
        "reason": _OPT_STR,
        "entity_type": {"enum": [*ENTITY_TYPES, None]},
        "label": _OPT_STR,
        "release_stamp": _OPT_STR,
    }
)

ENTITY = _object(
    {
        "reep_id": _REEP_ID,
        "entity_type": {"enum": list(ENTITY_TYPES)},
        "status": _STR,
        "label": _OPT_STR,
        "gender": _OPT_STR,
        "country": _OPT_STR,
        "corroboration_grade": _OPT_STR,
        "corroboration_count": _OPT_INT,
        "bridges": {"type": "array", "items": BRIDGE},
        "aliases": {"type": "array", "items": _STR},
        "redirected_from": _OPT_REEP_ID,
        "release_stamp": _OPT_STR,
    }
)

TRANSLATION = _object(
    {
        "resolution": RESOLUTION,
        "to": _STR,
        "targets": {"type": "array", "items": BRIDGE},
    }
)

ENTITY_MATCH = _object(
    {
        "reep_id": _REEP_ID,
        "entity_type": _STR,
        "label": _OPT_STR,
        "status": _OPT_STR,
        "bridge_count": {"type": "integer"},
        "rank": {"type": "integer", "minimum": 0, "maximum": 3},
        "gender": _OPT_STR,
        "country": _OPT_STR,
    }
)

PROVIDER_NAMESPACE = _object(
    {
        "provider": _STR,
        "namespace": _OPT_STR,
        "bridge_count": {"type": "integer"},
        "entity_types": {"type": "array", "items": _STR},
    }
)

SYNC_RESULT = _object(
    {
        "path": _STR,
        "profile": _STR,
        "method": {"enum": ["snapshot", "replay", "up_to_date", "import"]},
        "stamp_before": _OPT_STR,
        "stamp_after": _OPT_STR,
        "downloaded_bytes": {"type": "integer"},
        "seconds": {"type": "number"},
        "notes": {"type": "array", "items": _STR},
    }
)

STATUS = _object(
    {
        "backend": {"enum": ["local", "api", None]},
        "catalog": {"type": ["object", "null"]},
        "duckdb_path": {"type": ["object", "null"]},
        "api": _object({"key_source": _OPT_STR, "base_url": _STR}),
        "releases_url": _STR,
        "latest_stamp": _OPT_STR,
        "update_available": {"type": ["boolean", "null"]},
    }
)

AUTH_STATUS = _object(
    {
        "key_source": _OPT_STR,
        "key": _OPT_STR,
        "plan": _OPT_STR,
        "expires_at": _OPT_STR,
        "days_remaining": _OPT_INT,
        "credits_remaining": _OPT_INT,
    },
    required=["key_source", "key"],
)


_COUNT = {"type": "integer", "minimum": 0}

TARGET_REPORT = _object(
    {
        "table": _STR,
        "entity_type": {"enum": list(ENTITY_TYPES)},
        "target": _STR,
        "column": _STR,
        "rows": _COUNT,
        "outcomes": _object(dict.fromkeys(OUTCOMES, _COUNT)),
        "planned": _COUNT,
        "written": _COUNT,
        "status_planned": _COUNT,
        "status_written": _COUNT,
        "needs_review": _COUNT,
    }
)

DB_SYNC_REPORT = _object(
    {
        "command": {"enum": ["sync", "review"]},
        "apply": {"type": "boolean"},
        "release_stamp": _OPT_STR,
        "database": _STR,
        "targets": {"type": "array", "items": TARGET_REPORT},
        "outcomes": _object(dict.fromkeys(OUTCOMES, _COUNT)),
        "planned": _COUNT,
        "written": _COUNT,
        "needs_review": _COUNT,
        "review_file": _OPT_STR,
        "run_log": _OPT_STR,
        "seconds": {"type": "number"},
        "notes": {"type": "array", "items": _STR},
    }
)


def _many(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": item}


def _one_or_many(item: dict[str, Any]) -> dict[str, Any]:
    return {"oneOf": [item, _many(item)]}


#: The ``data`` schema of each command's JSON output.
COMMANDS: dict[str, dict[str, Any]] = {
    "status": STATUS,
    "providers": _many(PROVIDER_NAMESPACE),
    "resolve": _one_or_many(RESOLUTION),
    "current": _one_or_many(ID_STATE),
    "translate": TRANSLATION,
    "search": _many(ENTITY_MATCH),
    "show": ENTITY,
    "catalog sync": SYNC_RESULT,
    "catalog import": SYNC_RESULT,
    "auth status": AUTH_STATUS,
    "auth login": _object({"stored_in": _STR, "verified": {"type": "boolean"}}),
    "auth logout": _object({"removed_from": _many(_STR)}),
    "init": _object({"path": _STR}),
    "skill install": _object({"path": _STR, "changed": {"type": "boolean"}}),
    "sync": DB_SYNC_REPORT,
    "review": DB_SYNC_REPORT,
}


def envelope_schema(command: str) -> dict[str, Any]:
    """Return the JSON Schema of a command's ``--json`` output.

    Args:
        command: The command name, for example ``"resolve"`` or ``"catalog sync"``.

    Returns:
        The schema.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"reep {command} --json",
        "type": "object",
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "command": {"const": command},
            "release_stamp": _OPT_STR,
            "backend": {"enum": ["local", "api", None]},
            "data": COMMANDS[command],
            "truncated": {"type": "boolean"},
            "next_offset": _OPT_INT,
        },
        "required": [
            "schema_version",
            "command",
            "release_stamp",
            "backend",
            "data",
            "truncated",
            "next_offset",
        ],
    }


def error_schema() -> dict[str, Any]:
    """Return the JSON Schema of an error on stderr."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "reep error",
        "type": "object",
        "properties": {
            "error": _object(
                {"code": _STR, "message": _STR, "hint": _OPT_STR},
                required=["code", "message", "hint"],
            )
        },
        "required": ["error"],
    }
