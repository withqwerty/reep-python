"""Release 2026-10-19.1 shape: producer checksums bind mapping parquet files only.

The producer manifest of a republished release is stale for files that the
republish re-laid. The descriptor's SHA-256 and size are the authority for every
file; the producer manifest still binds row counts and the mapping parquet files.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING, Any

import duckdb
import pytest

from reep import Register, _v4
from reep.backends.local import LocalBackend
from reep.errors import ChecksumMismatchError, UnsupportedReleaseError
from tests.support.v4_release import HistoricalV4Bucket, V4Bucket, encode, sha

if TYPE_CHECKING:
    from pathlib import Path

TEAM = "rt8df038ffeaed40"
STALE = "0" * 64
#: Files whose producer checksum was stale in 2026-10-19.1, and a file it omits.
UNBOUND = [
    "bridges.csv",
    "bridges.csv.gz",
    "edges.csv",
    "edges.csv.gz",
    "edges.parquet",
    "entities.csv.gz",
    "reep-register.duckdb",
    "upkeep_ledger.json",
    "site-summary.json",
]


@pytest.fixture
def v4(tmp_path: Path) -> V4Bucket:
    return V4Bucket(tmp_path / "release")


def publish_manifest(v4: V4Bucket, manifest: dict[str, Any]) -> None:
    raw = encode(manifest)
    ref = v4.descriptor["files"]["manifest.json"]
    ref.update(sha256=sha(raw), bytes=len(raw))
    v4.descriptor["source_manifest_sha256"] = sha(raw)
    v4.objects[ref["key"]] = raw
    v4.publish_descriptor()


def source_manifest(v4: V4Bucket) -> dict[str, Any]:
    return json.loads((v4.root / "manifest.json").read_text())  # type: ignore[no-any-return]


def test_mapping_parquet_list_is_the_six_mapping_tables() -> None:
    assert _v4.MAPPING_PARQUET == (
        "bridges.parquet",
        "entities.parquet",
        "redirects.parquet",
        "overlay_links.parquet",
        "overlay_aliases.parquet",
        "provider_aliases.parquet",
    )


def test_stale_or_absent_producer_checksums_outside_the_mapping_parquet_pass(
    v4: V4Bucket,
) -> None:
    manifest = source_manifest(v4)
    for name in UNBOUND:
        if name.endswith(".csv.gz") or name.endswith(".json"):
            manifest["checksums_sha256"].pop(name, None)  # the producer omits it
        else:
            manifest["checksums_sha256"][name] = STALE  # the producer is stale
    manifest["checksums_sha256"]["entities.csv.gz"] = STALE
    publish_manifest(v4, manifest)
    with v4.client() as client:
        parsed = client.manifest()
    assert parsed.table_file("entities") is not None
    assert parsed.table_file("entities").rows == 8  # type: ignore[union-attr]


@pytest.mark.parametrize("name", _v4.MAPPING_PARQUET)
def test_mapping_parquet_checksum_mismatch_still_refuses(
    v4: V4Bucket, name: str
) -> None:
    manifest = source_manifest(v4)
    manifest["checksums_sha256"][name] = STALE
    publish_manifest(v4, manifest)
    with (
        v4.client() as client,
        pytest.raises(UnsupportedReleaseError, match=f"source checksum for {name}"),
    ):
        client.manifest()


def test_missing_mapping_parquet_checksum_still_refuses(v4: V4Bucket) -> None:
    manifest = source_manifest(v4)
    del manifest["checksums_sha256"]["bridges.parquet"]
    publish_manifest(v4, manifest)
    with v4.client() as client, pytest.raises(UnsupportedReleaseError):
        client.manifest()


def test_producer_row_counts_stay_bound(v4: V4Bucket) -> None:
    manifest = source_manifest(v4)
    manifest["bridge_count"] = -1
    publish_manifest(v4, manifest)
    with v4.client() as client, pytest.raises(UnsupportedReleaseError, match="row"):
        client.manifest()


@pytest.mark.parametrize("name", [*UNBOUND, "provider_aliases.parquet"])
@pytest.mark.parametrize("change", ["bytes", "size"])
def test_descriptor_digest_and_size_still_bind_every_file(
    v4: V4Bucket, tmp_path: Path, name: str, change: str
) -> None:
    with v4.client() as client:
        entry = client.manifest().files[name]
        raw = v4.objects[entry.object_key]
        if change == "bytes":
            v4.objects[entry.object_key] = bytes([raw[0] ^ 1]) + raw[1:]
        else:
            v4.objects[entry.object_key] = raw + b"x"
        with pytest.raises(ChecksumMismatchError):
            client.download(entry, tmp_path / "out" / name)


@pytest.fixture
def hv4(tmp_path: Path) -> HistoricalV4Bucket:
    return HistoricalV4Bucket(tmp_path / "historical_release")


def published_shape(hv4: HistoricalV4Bucket, tmp_path: Path) -> Path:
    """Copy the fixture DuckDB, then take the 2026-10-19.1 published shape.

    No ``edges`` table, no ``_overlay_aliases_raw``, ``overlay_aliases`` a base
    table, ``entity_search`` and ``matches`` views over the internal tables.
    """
    path = tmp_path / "published.duckdb"
    shutil.copyfile(hv4.root / "reep-register.duckdb", path)
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE oa AS SELECT * FROM overlay_aliases")
        con.execute("DROP VIEW overlay_aliases")
        con.execute("ALTER TABLE oa RENAME TO overlay_aliases")
        con.execute("DROP TABLE _overlay_aliases_raw")
        con.execute("DROP TABLE edges")
    return path


def tables_of(path: Path) -> set[str]:
    with duckdb.connect(str(path), read_only=True) as con:
        return {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }


def test_published_duckdb_shape_opens_and_answers(
    hv4: HistoricalV4Bucket, tmp_path: Path
) -> None:
    path = published_shape(hv4, tmp_path)
    before = sha(path.read_bytes())
    assert "edges" not in tables_of(path)
    assert "_overlay_aliases_raw" not in tables_of(path)
    with Register.local(path) as reg:
        assert reg.resolve("opta:team:TH").reep_id == TEAM
        assert "edges" not in reg.release.tables
        assert "entities" in reg.release.tables
        assert all(not t.startswith("_") for t in reg.release.tables)
        bridge = reg.resolve("opta:team:TH").bridge
        assert bridge is not None
        assert bridge.rung is None
        assert bridge.upstream_status is None
    assert sha(path.read_bytes()) == before


def test_the_full_historical_shape_still_opens(hv4: HistoricalV4Bucket) -> None:
    """Optional means optional: a file with every internal table and edges opens."""
    with Register.local(hv4.root / "reep-register.duckdb") as reg:
        assert reg.resolve("opta:team:TH").reep_id == TEAM
        assert "edges" in reg.release.tables


def test_a_missing_redirects_table_still_refuses(
    hv4: HistoricalV4Bucket, tmp_path: Path
) -> None:
    path = published_shape(hv4, tmp_path)
    with duckdb.connect(str(path)) as con:
        con.execute("DROP TABLE redirects")
    with pytest.raises(UnsupportedReleaseError, match="lacks redirects"):
        LocalBackend(path)


@pytest.mark.parametrize("table", ["provider_aliases", "overlay_links", "entities"])
def test_a_missing_public_table_still_refuses(
    hv4: HistoricalV4Bucket, tmp_path: Path, table: str
) -> None:
    path = published_shape(hv4, tmp_path)
    with duckdb.connect(str(path)) as con:
        if table == "entities":
            con.execute("DROP VIEW entity_search")
            con.execute("DROP VIEW matches")
            con.execute("DROP TABLE _entity_search_idx")
        con.execute(f"DROP TABLE {table}")
    with pytest.raises(UnsupportedReleaseError):
        LocalBackend(path)


def test_a_missing_public_view_still_refuses(
    hv4: HistoricalV4Bucket, tmp_path: Path
) -> None:
    path = published_shape(hv4, tmp_path)
    with duckdb.connect(str(path)) as con:
        con.execute("DROP VIEW matches")
    with pytest.raises(UnsupportedReleaseError, match="inventory"):
        LocalBackend(path)


def test_an_unknown_extra_table_still_refuses(
    hv4: HistoricalV4Bucket, tmp_path: Path
) -> None:
    path = published_shape(hv4, tmp_path)
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE TABLE surprise AS SELECT 1 AS x")
    with pytest.raises(UnsupportedReleaseError, match="inventory"):
        LocalBackend(path)


def test_columns_of_a_present_internal_table_are_still_checked(
    hv4: HistoricalV4Bucket, tmp_path: Path
) -> None:
    path = published_shape(hv4, tmp_path)
    with duckdb.connect(str(path)) as con:
        con.execute("ALTER TABLE _match_dates ADD COLUMN extra VARCHAR")
    with pytest.raises(UnsupportedReleaseError, match="layout"):
        LocalBackend(path)


def test_columns_of_the_optional_edges_table_are_checked_when_present(
    hv4: HistoricalV4Bucket, tmp_path: Path
) -> None:
    path = tmp_path / "with-edges.duckdb"
    shutil.copyfile(hv4.root / "reep-register.duckdb", path)
    with duckdb.connect(str(path)) as con:
        con.execute("ALTER TABLE edges ADD COLUMN extra VARCHAR")
    with pytest.raises(UnsupportedReleaseError, match="layout"):
        LocalBackend(path)
