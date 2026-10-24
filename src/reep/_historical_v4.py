"""The retained bridge-register-v1 / v4_physical_views publication variant."""

from __future__ import annotations

from typing import Any

from reep import _v4
from reep.errors import UnsupportedReleaseError

LAYOUT = "bridge-register-v1/v4_physical_views/1"
MODE = "v4_physical_views"
TABLES = tuple(t for t in _v4.TABLES if t != "entity_search")
ALIASES = {"aliases": "overlay_aliases", "relationships": "edges"}
FILES = frozenset(
    [f"{t}.parquet" for t in TABLES]
    + [f"csv/{t}.csv.gz" for t in (*TABLES, *ALIASES)]
    + [
        "manifest.json",
        "upkeep_ledger.json",
        "site-summary.json",
        "checksums.txt",
        "reep-register.duckdb",
        "duckdb/reep-register-v1.duckdb",
    ]
)
MATCH_COLUMNS = [
    "reep_id",
    "fixture_label",
    "gender",
    "country",
    "match_date_iso",
    "grade",
    "bucket_count",
    "corroboration_buckets",
    "metadata_json",
]
INTERNAL_COLUMNS: dict[str, list[str]] = {
    "_entity_search_idx": [
        "reep_id",
        "alias_blob",
        "bridge_count",
        "alias_count",
    ],
    "_match_dates": ["reep_id", "match_date_iso"],
    "_overlay_aliases_raw": [
        "reep_id",
        "source",
        "qid",
        "name",
        "language",
        "name_source",
        "confidence",
        "link_confidence",
        "dob_check",
    ],
}
INTERNAL_TABLES = frozenset(INTERNAL_COLUMNS)


def need(condition: object, message: str) -> None:
    """Refuse an unsupported historical variant, without legacy fallback."""
    if not condition:
        raise UnsupportedReleaseError(f"Invalid historical v4 release: {message}.")


def valid_stamp(value: object) -> bool:
    """Recognise only the declared date/revision stamp family."""
    if not isinstance(value, str):
        return False
    try:
        _v4.stamp_order(value)
    except ValueError:
        return False
    return True


def selector(value: dict[str, Any]) -> dict[str, Any]:
    """Bind the historical selector to its advertised descriptor object key."""
    need(
        value.get("schema_version") == "reep-public-release-latest-v1"
        and value.get("release_schema_version") == "bridge-register-v1"
        and value.get("release_tier") == "public_bridge_v1",
        "selector contract",
    )
    stamp = value.get("stamp")
    need(valid_stamp(stamp) and value.get("release_tag") == stamp, "selector stamp")
    key = f"releases/{stamp}/release.json"
    need(value.get("manifest_object_key") == key, "descriptor object key")
    need(_v4.digest(value.get("manifest_sha256")), "descriptor digest")
    return value


def descriptor(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the exact historical declaration and immutable file references."""
    need(
        value.get("schema_version") == "bridge-register-v1"
        and type(value.get("manifest_version")) is int
        and value.get("manifest_version") == 1
        and value.get("projection_mode") == MODE,
        "descriptor variant",
    )
    stamp = value.get("stamp")
    need(valid_stamp(stamp) and value.get("release_tag") == stamp, "descriptor stamp")
    need(value.get("tier") == "public_bridge_v1", "publication tier")
    _v4.timestamp(value.get("generated_at"))
    files = value.get("files")
    if not isinstance(files, dict) or set(files) != FILES:
        need(condition=False, message="file inventory")
        raise AssertionError
    counts = value.get("counts")
    if not isinstance(counts, dict) or set(counts) != set(TABLES):
        need(condition=False, message="counts")
        raise AssertionError
    row_counts = value.get("row_counts")
    if not isinstance(row_counts, dict):
        need(condition=False, message="row counts")
        raise AssertionError
    for table in TABLES:
        need(type(counts[table]) is int and counts[table] >= 0, "table count")
    for name, entry in files.items():
        if not isinstance(entry, dict):
            need(condition=False, message="file reference")
            raise AssertionError
        _v4.reference(
            {
                "key": entry.get("object_key"),
                "sha256": entry.get("sha256"),
                "bytes": entry.get("bytes"),
            },
            key=f"releases/{stamp}/{name}",
        )
        table = (
            name.removeprefix("csv/").removesuffix(".csv.gz").removesuffix(".parquet")
        )
        if table in {*TABLES, *ALIASES}:
            expected = counts[ALIASES.get(table, table)]
            need(
                type(entry.get("rows")) is int
                and entry["rows"] == expected
                and type(row_counts.get(name)) is int
                and row_counts[name] == expected,
                "file row count",
            )
    for alias, canonical in ALIASES.items():
        left, right = files[f"csv/{alias}.csv.gz"], files[f"csv/{canonical}.csv.gz"]
        need(
            all(left[k] == right[k] for k in ("sha256", "bytes", "rows")),
            "alias identity",
        )
    need(
        all(
            files["reep-register.duckdb"][k]
            == files["duckdb/reep-register-v1.duckdb"][k]
            for k in ("sha256", "bytes")
        ),
        "DuckDB alias identity",
    )
    return value


def source_counts(raw: bytes, release: dict[str, Any]) -> dict[str, int]:
    """Verify the distinct producer manifest without equating different gzip files."""
    entry = release["files"]["manifest.json"]
    _v4.bound_bytes(raw, entry, "manifest.json")
    value = _v4.loads(raw, "manifest.json")
    if not isinstance(value, dict) or value.get("release_tag") != release["stamp"]:
        need(condition=False, message="source manifest identity")
        raise AssertionError
    need(
        value.get("release_date") == _v4.stamp_order(release["stamp"])[0], "source date"
    )
    checksums = value.get("checksums_sha256")
    if not isinstance(checksums, dict):
        need(condition=False, message="source checksums")
        raise AssertionError
    for name in [
        *(f"{t}.parquet" for t in TABLES),
        "reep-register.duckdb",
        "upkeep_ledger.json",
        "site-summary.json",
    ]:
        need(
            checksums.get(name) == release["files"][name]["sha256"],
            f"source {name} digest",
        )
    counts: dict[str, int] = {}
    for table in TABLES:
        count = value.get(_v4.COUNT_KEYS[table])
        if type(count) is not int or count != release["counts"][table]:
            need(condition=False, message="source count")
            raise AssertionError
        counts[table] = count
    return counts
