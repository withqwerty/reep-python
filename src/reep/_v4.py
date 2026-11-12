"""The versioned public v4 download contract, separate from API selection."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Any

from reep.errors import ChecksumMismatchError, UnsupportedReleaseError

SCHEMA = "reep-public-release/v4"
SELECTOR = "reep-public-selection/v1"
METADATA_LIMIT = 2_097_152
TABLES = (
    "entities",
    "bridges",
    "matches",
    "edges",
    "redirects",
    "overlay_links",
    "overlay_aliases",
    "provider_aliases",
    "entity_search",
)
FILES = frozenset(
    [f"{t}.{ext}" for t in TABLES for ext in ("parquet", "csv", "csv.gz")]
    + [
        "reep-register.duckdb",
        "manifest.json",
        "latest.json",
        "upkeep_ledger.json",
        "site-summary.json",
    ]
)
COUNT_KEYS = dict(
    zip(
        TABLES,
        (
            "entity_count",
            "bridge_count",
            "match_count",
            "edge_count",
            "redirect_count",
            "overlay_link_count",
            "overlay_alias_count",
            "provider_alias_count",
            "entity_search_count",
        ),
        strict=True,
    )
)
#: Public tables that a shipped DuckDB file may omit: edges left the public scope.
OPTIONAL_SHIPPED_TABLES = frozenset({"edges"})
#: Mapping tables whose parquet file the producer manifest checksums bind.
#: Every other file is bound by the descriptor's sha256 and size only: the
#: producer manifest is not re-issued when a republish re-lays a file.
MAPPING_PARQUET = tuple(
    f"{t}.parquet"
    for t in (
        "bridges",
        "entities",
        "redirects",
        "overlay_links",
        "overlay_aliases",
        "provider_aliases",
    )
)
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})\.([1-9]\d{0,19})\Z")


def need(condition: object, message: str) -> None:
    """Reject an unsupported contract rather than attempting another format."""
    if not condition:
        raise UnsupportedReleaseError(f"Invalid v4 download: {message}.")


def stamp_order(value: str) -> tuple[str, int]:
    """Validate and return the calendar date and numeric v4 revision."""
    match = _STAMP.fullmatch(value)
    if match is None:
        raise ValueError("Expected a v4 release stamp such as 2026-10-23.1")
    if date.fromisoformat(match[1]).isoformat() != match[1]:
        raise ValueError("Invalid v4 release date")
    return match[1], int(match[2])


def digest(value: object) -> bool:
    """Whether a value is a lowercase SHA-256 digest."""
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def exact(value: object, keys: set[str], label: str) -> dict[str, Any]:
    """Require exactly the keys declared by the owning contract."""
    if not isinstance(value, dict) or set(value) != keys:
        raise UnsupportedReleaseError(f"Invalid v4 download: {label} schema.")
    return dict(value)


def timestamp(value: object) -> None:
    """Require an explicit timestamp, without inventing a time when absent."""
    need(isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}T", value), "timestamp")
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise UnsupportedReleaseError("Invalid v4 download timestamp.") from exc


def reference(value: object, *, key: str | None = None) -> dict[str, Any]:
    """Validate a bounded, same-bucket immutable object reference."""
    result = exact(value, {"key", "sha256", "bytes"}, "file reference")
    path = result["key"]
    need(
        isinstance(path, str) and re.fullmatch(r"[a-zA-Z0-9_./-]+", path), "object key"
    )
    need(
        all(p and p not in {".", "..", "__proto__"} for p in path.split("/")),
        "object key segments",
    )
    need(key is None or path == key, "object key differs from declared release")
    need(digest(result["sha256"]), "object digest")
    need(
        type(result["bytes"]) is int and 0 < result["bytes"] <= 2**53 - 1,
        "object bytes",
    )
    return result


def selection(value: object) -> dict[str, Any]:
    """Read only the downloads selector; an API selector is never a fallback."""
    result = exact(
        value,
        {
            "schema",
            "channel",
            "stamp",
            "candidate_sha256",
            "policy_sha256",
            "generated_at",
            "activation_requested_at",
            "predecessor_sha256",
            "release",
        },
        "selector",
    )
    need(
        result["schema"] == SELECTOR and result["channel"] == "downloads",
        "selector schema/channel",
    )
    _stamp(result["stamp"])
    need(
        digest(result["candidate_sha256"]) and digest(result["policy_sha256"]),
        "selector identity",
    )
    need(
        result["predecessor_sha256"] is None or digest(result["predecessor_sha256"]),
        "predecessor digest",
    )
    timestamp(result["generated_at"])
    timestamp(result["activation_requested_at"])
    ref = reference(result["release"], key=f"releases/{result['stamp']}/release.json")
    need(ref["bytes"] <= METADATA_LIMIT, "descriptor exceeds metadata bound")
    return result


def descriptor(value: object, selected: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate the actual site publisher's immutable downloads descriptor."""
    result = exact(
        value,
        {
            "schema",
            "channel",
            "stamp",
            "candidate_sha256",
            "policy_sha256",
            "source",
            "generated_at",
            "source_manifest_sha256",
            "files",
        },
        "descriptor",
    )
    need(
        result["schema"] == SCHEMA and result["channel"] == "downloads",
        "descriptor schema/channel",
    )
    _stamp(result["stamp"])
    need(
        all(
            digest(result[k])
            for k in ("candidate_sha256", "policy_sha256", "source_manifest_sha256")
        ),
        "descriptor digest",
    )
    timestamp(result["generated_at"])
    source = exact(
        result["source"],
        {"generation_id", "manifest_sha256", "as_of"},
        "source identity",
    )
    need(
        isinstance(source["generation_id"], str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source["generation_id"]),
        "source generation",
    )
    need(digest(source["manifest_sha256"]), "source manifest digest")
    if source["as_of"] is not None:
        timestamp(source["as_of"])
    if selected is not None:
        need(
            all(
                result[k] == selected[k]
                for k in (
                    "channel",
                    "stamp",
                    "candidate_sha256",
                    "policy_sha256",
                    "generated_at",
                )
            ),
            "selector/descriptor identity mismatch",
        )
    files = exact(result["files"], set(FILES), "public file inventory")
    keys: set[str] = set()
    for name, value in files.items():
        ref = reference(value)
        need(
            ref["key"].startswith(f"releases/{result['stamp']}/objects/")
            and ref["key"].endswith(f"/{name}"),
            "public object path",
        )
        need(ref["key"] not in keys, "repeated object key")
        keys.add(ref["key"])
    need(
        files["manifest.json"]["sha256"] == result["source_manifest_sha256"],
        "source manifest binding",
    )
    return result


def bound_bytes(raw: bytes, ref: dict[str, Any], label: str) -> None:
    """Check the bytes and digest before parsing any referenced document."""
    if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise ChecksumMismatchError(f"{label} differs from its immutable reference.")


def source_counts(raw: bytes, release: dict[str, Any]) -> dict[str, int]:
    """Bind the producer manifest's counts and mapping-table checksums."""
    bound_bytes(raw, release["files"]["manifest.json"], "manifest.json")
    value = loads(raw, "manifest.json")
    need(isinstance(value, dict), "source manifest object")
    need(value.get("release_tag") == release["stamp"], "source release tag")
    need(
        value.get("release_date") == stamp_order(release["stamp"])[0],
        "source release date",
    )
    checksums = value.get("checksums_sha256")
    need(isinstance(checksums, dict), "source checksums")
    for name in MAPPING_PARQUET:
        need(
            checksums.get(name) == release["files"][name]["sha256"],
            f"source checksum for {name}",
        )
    counts: dict[str, int] = {}
    for table, key in COUNT_KEYS.items():
        count = value.get(key)
        need(type(count) is int and count >= 0, f"{table} row count")
        counts[table] = count
    return counts


def loads(raw: bytes, label: str) -> Any:
    """Reject duplicate JSON fields rather than choosing a conflicting value."""
    if label == "latest.json" and len(raw) > 65_536:
        raise UnsupportedReleaseError("Release metadata exceeds its byte bound.")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise UnsupportedReleaseError(
            f"{label} is not valid unambiguous JSON."
        ) from exc


def _stamp(value: object) -> None:
    try:
        need(isinstance(value, str), "stamp")
        stamp_order(str(value))
    except ValueError as exc:
        raise UnsupportedReleaseError("Invalid v4 download stamp.") from exc
