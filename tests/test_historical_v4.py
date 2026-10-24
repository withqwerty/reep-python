"""Tests for the retained bridge-register-v1 / v4_physical_views adapter."""

from __future__ import annotations

import csv
import gzip
import io
import json
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from reep import Register, _historical_v4, _v4_store
from reep.backends.local import LocalBackend
from reep.catalog import import_release, state, sync
from reep.errors import ChecksumMismatchError, ReplayError, UnsupportedReleaseError
from reep.models import IdStatus
from reep.release import ReleaseClient
from tests.conftest import make_settings
from tests.support.v4_release import STAMP, HistoricalV4Bucket, encode, sha

TEAM = "rt8df038ffeaed40"
MATCH = "rm8a047e9193a247"
VENUE = "rv7bffdbdab61249"
OLD = "rt00000000000000"
CAPTURED_METADATA_DIR = Path("/Volumes/WQ/tmp/WIT-3630/metadata")


@pytest.fixture
def hv4(tmp_path: Path) -> HistoricalV4Bucket:
    return HistoricalV4Bucket(tmp_path / "historical_release")


def read_csv_rows(
    bucket: HistoricalV4Bucket, table: str
) -> tuple[list[str], list[dict[str, str]]]:
    with gzip.open(bucket.root / f"csv/{table}.csv.gz", "rt", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def write_csv_rows(
    bucket: HistoricalV4Bucket,
    table: str,
    header: list[str],
    values: list[dict[str, Any]],
) -> None:
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    writer.writerows(values)
    (bucket.root / f"csv/{table}.csv.gz").write_bytes(
        gzip.compress(out.getvalue().encode(), mtime=0)
    )
    bucket.rebuild(bucket.stamp)


def test_captured_baseline_metadata_binding_ignores_misleading_selector_fields() -> (
    None
):
    if not CAPTURED_METADATA_DIR.is_dir():
        pytest.skip("captured WIT-3630 metadata directory not present")
    latest_raw = (CAPTURED_METADATA_DIR / "latest.body").read_bytes()
    release_raw = (CAPTURED_METADATA_DIR / "release.body").read_bytes()
    manifest_raw = (CAPTURED_METADATA_DIR / "manifest.body").read_bytes()
    assert len(latest_raw) == 4399
    assert (
        sha(latest_raw)
        == "4bc7d466198fa1107867c4a0ca326663f1cd8005f911b00614ef4682e7abeb0b"
    )
    assert len(release_raw) == 10101
    assert (
        sha(release_raw)
        == "f223ea6a35151bba9ffca1a1e218c3cabf5e2fc732c8bcc4747f98ed6dc374e6"
    )
    assert len(manifest_raw) == 36925
    assert (
        sha(manifest_raw)
        == "f1dae89aa3d48f597eb579ef4284034496670b8726e5f285371ae5efb1c185d3"
    )

    objects = {
        "releases/latest.json": latest_raw,
        "releases/2026-10-23.1/release.json": release_raw,
        "releases/2026-10-23.1/manifest.json": manifest_raw,
    }
    calls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip("/")
        calls.append(key)
        raw = objects.get(key)
        return httpx.Response(
            404 if raw is None else 200, content=raw or b"", request=request
        )

    with ReleaseClient(
        "https://captured",
        http=httpx.Client(transport=httpx.MockTransport(transport)),
        retries=0,
    ) as client:
        pointer = client.latest()
        assert pointer.stamp == "2026-10-23.1"
        assert pointer.manifest_key == "releases/2026-10-23.1/release.json"
        assert pointer.layout == _historical_v4.LAYOUT
        manifest = client.manifest()
        assert manifest.stamp == "2026-10-23.1"
        assert manifest.schema_version == "bridge-register-v1"
        assert manifest.layout == _historical_v4.LAYOUT
        assert manifest.identity_sha256 == sha(release_raw)
        assert manifest.tables == tuple(sorted(_historical_v4.TABLES))
        assert "aliases" not in manifest.tables
        assert "relationships" not in manifest.tables
        entities_entry = manifest.table_file("entities")
        assert entities_entry is not None
        assert entities_entry.object_key == "releases/2026-10-23.1/csv/entities.csv.gz"
        assert entities_entry.rows == 3136215
    assert calls == [
        "releases/latest.json",
        "releases/latest.json",
        "releases/2026-10-23.1/release.json",
        "releases/2026-10-23.1/manifest.json",
    ]
    csv_dir = CAPTURED_METADATA_DIR / "objects/csv"
    if csv_dir.is_dir():
        with duckdb.connect(":memory:") as con:
            for table in _historical_v4.TABLES:
                header = con.execute(
                    "DESCRIBE SELECT * FROM read_csv(?, header = true, "
                    "all_varchar = true, quote = '\"', escape = '\"')",
                    [str(csv_dir / f"{table}.csv.gz")],
                ).fetchall()
                cols = [str(r[0]) for r in header]
                _v4_store.validate_columns(table, cols, historical=True)


def test_captured_shipped_duckdb_read_only_and_literal_queries() -> None:
    path = CAPTURED_METADATA_DIR / "objects/reep-register.duckdb"
    if not path.is_file():
        pytest.skip("captured WIT-3630 shipped DuckDB not present")
    expected_sha = "5531c8f6d723805495575eadf02464e08e8f9347135c8e3ad4fadfeed8ee8bd5"
    assert (
        json.loads((CAPTURED_METADATA_DIR / "release.body").read_text())["files"][
            "reep-register.duckdb"
        ]["sha256"]
        == expected_sha
    )
    assert sha(path.read_bytes()) == expected_sha
    with Register.local(path) as reg:
        info = reg.release
        assert info.stamp is None
        assert info.schema_version == "bridge-register-v1"
        assert info.extra["sdk_release_layout"] == _historical_v4.LAYOUT
        assert info.extra["publication_identity"] == "unverified_local_file"
        assert info.extra["lifecycle"] == "not_published"
        assert all(not table.startswith("_") for table in info.tables)

        resolved = reg.resolve("besoccer:coach:64780")
        assert resolved.found
        assert resolved.reep_id == "c_00a5f88eda91fa81"
        assert resolved.entity_status is None
        assert resolved.bridge is not None
        assert resolved.bridge.rung is None
        assert resolved.bridge.upstream_status is None

        redirected = reg.current("rt305bc2f9915e6b")
        assert redirected.status is IdStatus.REDIRECTED
        assert redirected.current_id == "rtbf1cc0b27e2636"
        assert redirected.current_status is None
        assert redirected.reason is not None
        assert "Al Merreikh" in redirected.reason

        entity = reg.entity("rt305bc2f9915e6b")
        assert entity is not None
        assert entity.reep_id == "rtbf1cc0b27e2636"
        assert entity.label == "Al Merreikh"
        assert entity.status is None
        assert entity.redirected_from == "rt305bc2f9915e6b"

        matches_found = reg.search("Al Merreikh", limit=3)
        assert matches_found[0].reep_id == "rtbf1cc0b27e2636"
        assert matches_found[0].status is None

    backend = LocalBackend(path)
    try:
        rows = backend.matches(date_from="2007-11-18", date_to="2007-11-18", limit=5)
        assert len(rows) == 5
        assert rows[0]["kickoff_date"] == "2007-11-18"
        assert rows[0]["status"] is None
        assert rows[0]["competition_id"] is None
        assert rows[0]["season_id"] is None
        assert rows[0]["home_team_id"] is None
        assert rows[0]["away_team_id"] is None
        assert rows[0]["home_score"] is None
        assert rows[0]["away_score"] is None
        bad_filters: list[dict[str, Any]] = [
            {"status": "active"},
            {"competition_id": "rl00000000000000"},
            {"season_id": "rs00000000000000"},
            {"team_id": "rtbf1cc0b27e2636"},
        ]
        for bad_filter in bad_filters:
            with pytest.raises(UnsupportedReleaseError, match="unavailable"):
                backend.matches(**bad_filter)
    finally:
        backend.close()
    assert sha(path.read_bytes()) == expected_sha
    assert not path.with_name(path.name + ".wal").exists()


@pytest.mark.parametrize("profile", ["slim", "full"])
def test_historical_snapshot_sync_and_import_slim_and_full(
    hv4: HistoricalV4Bucket, tmp_path: Path, profile: str
) -> None:
    settings = make_settings(tmp_path / "cache")
    with hv4.client() as client:
        manifest = client.manifest()
        assert manifest.schema_version == "bridge-register-v1"
        assert manifest.layout == _historical_v4.LAYOUT
        result = sync(profile, with_aliases=True, client=client, settings=settings)
        assert result.method == "snapshot"
        assert (
            sync(profile, with_aliases=True, client=client, settings=settings).method
            == "up_to_date"
        )
        with pytest.raises(ReplayError, match="unavailable"):
            sync(method="replay", client=client, settings=settings)
    assert "releases/2026-10-23.1/csv/aliases.csv.gz" not in hv4.calls
    assert "releases/2026-10-23.1/csv/relationships.csv.gz" not in hv4.calls

    current_state = state(settings)
    assert current_state is not None
    assert current_state.layout == _historical_v4.LAYOUT
    assert "relationships" not in current_state.tables

    with Register.local(settings=settings) as reg:
        resolved = reg.resolve("opta:team:TH")
        assert resolved.found
        assert resolved.reep_id == TEAM
        assert resolved.entity_status is None
        assert reg.current(TEAM).status is IdStatus.PUBLISHED
        assert reg.current(TEAM).current_status is None
        assert reg.entity(VENUE).entity_type == "venue"
        assert reg.search("Home United")[0].reep_id == TEAM
        assert reg.release.stamp == STAMP
        assert reg.release.extra["sdk_release_layout"] == _historical_v4.LAYOUT

    if profile == "full":
        backend = LocalBackend(settings.catalog_path)
        try:
            match_rows = backend.matches(date_from="2026-09-01", date_to="2026-09-30")
            assert len(match_rows) == 1
            assert match_rows[0]["reep_id"] == MATCH
            assert match_rows[0]["season_id"] is None
            assert match_rows[0]["home_team_id"] is None
            assert match_rows[0]["away_team_id"] is None
            assert match_rows[0]["home_score"] is None
            assert match_rows[0]["away_score"] is None
            bad_filters: list[dict[str, Any]] = [
                {"status": "active"},
                {"competition_id": "rl00000000000000"},
                {"season_id": "rs00000000000000"},
                {"team_id": TEAM},
            ]
            for bad_filter in bad_filters:
                with pytest.raises(UnsupportedReleaseError, match="unavailable"):
                    backend.matches(**bad_filter)
        finally:
            backend.close()

    import_settings = make_settings(tmp_path / "imported")
    import_release(
        hv4.root, profile=profile, with_aliases=True, settings=import_settings
    )
    imported_state = state(import_settings)
    assert imported_state is not None
    assert imported_state.layout == _historical_v4.LAYOUT
    assert "relationships" not in imported_state.tables

    before = sha(import_settings.catalog_path.read_bytes())
    (hv4.root / "csv/aliases.csv.gz").write_bytes(b"corrupted-alias")
    with pytest.raises(ChecksumMismatchError):
        import_release(hv4.root, profile=profile, settings=import_settings)
    assert sha(import_settings.catalog_path.read_bytes()) == before


def test_historical_shipped_duckdb_fixture_and_tamper_refusal(
    hv4: HistoricalV4Bucket,
) -> None:
    path = hv4.root / "reep-register.duckdb"
    before = sha(path.read_bytes())
    with Register.local(path) as reg:
        assert reg.resolve("opta:team:TH").reep_id == TEAM
        assert reg.current(TEAM).status is IdStatus.PUBLISHED
        assert reg.release.stamp is None
        assert reg.release.schema_version == "bridge-register-v1"
        assert reg.release.extra["sdk_release_layout"] == _historical_v4.LAYOUT
        assert reg.release.extra["publication_identity"] == "unverified_local_file"
        assert all(not t.startswith("_") for t in reg.release.tables)
    backend = LocalBackend(path)
    try:
        assert len(backend.matches(date_from="2026-09-01", date_to="2026-09-30")) == 1
        with pytest.raises(UnsupportedReleaseError, match="unavailable"):
            backend.matches(team_id=TEAM)
        with pytest.raises(UnsupportedReleaseError, match="unavailable"):
            backend.matches(season_id="rs00000000000000")
    finally:
        backend.close()
    assert sha(path.read_bytes()) == before
    assert not path.with_name(path.name + ".wal").exists()

    with duckdb.connect(str(path)) as con:
        con.execute("ALTER TABLE _match_dates ADD COLUMN extra VARCHAR")
    with pytest.raises(UnsupportedReleaseError, match="layout"):
        LocalBackend(path)


def test_historical_dbsync_records_literal_ids_and_null_lifecycle(
    hv4: HistoricalV4Bucket, tmp_path: Path
) -> None:
    import sqlalchemy as sa

    from reep import dbsync

    settings = make_settings(tmp_path / "cache")
    import_release(hv4.root, settings=settings)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'consumer.db'}")
    config = dbsync.parse_config(
        {
            "version": 1,
            "tables": {
                "teams": {
                    "table": "teams",
                    "entity_type": "team",
                    "primary_key": "id",
                    "reep_id": "reep_id",
                    "status": "lookup_status",
                    "provider_ids": {"opta/team": "opta_id"},
                }
            },
        }
    )
    try:
        with engine.begin() as con:
            con.execute(
                sa.text(
                    "CREATE TABLE teams(id INTEGER PRIMARY KEY,opta_id TEXT,"
                    "reep_id TEXT,lookup_status TEXT,entity_lifecycle TEXT)"
                )
            )
            con.execute(
                sa.text(
                    "INSERT INTO teams VALUES(1,'TH',NULL,NULL,NULL),"
                    "(2,'TH',:rid,NULL,NULL)"
                ),
                {"rid": TEAM},
            )
        with Register.local(settings=settings) as reg:
            report = dbsync.sync(config, reg, apply=True, engine=engine)
            assert report.targets[0].outcomes["new"] == 1
            assert report.targets[0].outcomes["unchanged"] == 1
            assert reg.current(TEAM).current_status is None
        with engine.connect() as con:
            assert con.execute(
                sa.text(
                    "SELECT reep_id,lookup_status,entity_lifecycle "
                    "FROM teams ORDER BY id"
                )
            ).fetchall() == [(TEAM, "resolved", None), (TEAM, "resolved", None)]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    [
        "selector_tier",
        "selector_key",
        "selector_tag",
        "descriptor_mode",
        "descriptor_tier",
        "alias_mismatch",
        "duckdb_alias_mismatch",
        "row_count_mismatch",
        "source_parquet_digest",
        "source_count_mismatch",
        "same_stamp_replacement",
    ],
)
def test_historical_selector_descriptor_and_source_manifest_refusals(
    hv4: HistoricalV4Bucket, tmp_path: Path, mutation: str
) -> None:
    if mutation == "same_stamp_replacement":
        settings = make_settings(tmp_path / "cache")
        with hv4.client() as client:
            sync(client=client, settings=settings)
        hv4.descriptor["generated_at"] = "2026-10-23T10:00:00Z"
        hv4.publish_descriptor()
        with (
            hv4.client() as client,
            pytest.raises(UnsupportedReleaseError, match="same stamp"),
        ):
            sync(client=client, settings=settings)
        return

    if mutation == "selector_tier":
        hv4.selected["release_tier"] = "wrong_tier"
        hv4.objects["releases/latest.json"] = encode(hv4.selected)
    elif mutation == "selector_key":
        hv4.selected["manifest_object_key"] = "releases/2026-10-23.1/manifest.json"
        hv4.objects["releases/latest.json"] = encode(hv4.selected)
    elif mutation == "selector_tag":
        hv4.selected["release_tag"] = "2026-10-23.2"
        hv4.objects["releases/latest.json"] = encode(hv4.selected)
    elif mutation == "descriptor_mode":
        hv4.descriptor["projection_mode"] = "public_bridge_v1"
        hv4.publish_descriptor()
    elif mutation == "descriptor_tier":
        hv4.descriptor["tier"] = "wrong_tier"
        hv4.publish_descriptor()
    elif mutation == "alias_mismatch":
        hv4.descriptor["files"]["csv/aliases.csv.gz"]["rows"] += 1
        hv4.publish_descriptor()
    elif mutation == "duckdb_alias_mismatch":
        hv4.descriptor["files"]["duckdb/reep-register-v1.duckdb"]["bytes"] += 1
        hv4.publish_descriptor()
    elif mutation == "row_count_mismatch":
        hv4.descriptor["row_counts"]["csv/entities.csv.gz"] += 1
        hv4.publish_descriptor()
    elif mutation == "source_parquet_digest":
        manifest = json.loads((hv4.root / "manifest.json").read_text())
        manifest["checksums_sha256"]["entities.parquet"] = "0" * 64
        raw = encode(manifest)
        (hv4.root / "manifest.json").write_bytes(raw)
        entry = hv4.descriptor["files"]["manifest.json"]
        entry.update(sha256=sha(raw), bytes=len(raw))
        hv4.objects[entry["object_key"]] = raw
        hv4.publish_descriptor()
    else:
        manifest = json.loads((hv4.root / "manifest.json").read_text())
        manifest["entity_count"] += 1
        raw = encode(manifest)
        (hv4.root / "manifest.json").write_bytes(raw)
        entry = hv4.descriptor["files"]["manifest.json"]
        entry.update(sha256=sha(raw), bytes=len(raw))
        hv4.objects[entry["object_key"]] = raw
        hv4.publish_descriptor()

    with hv4.client() as client, pytest.raises(UnsupportedReleaseError):
        client.manifest()


@pytest.mark.parametrize(
    "mutation", ["columns", "duplicate_bridge", "dangling_bridge", "cycle_redirect"]
)
def test_historical_bad_table_never_replaces_catalogue(
    hv4: HistoricalV4Bucket, tmp_path: Path, mutation: str
) -> None:
    settings = make_settings(tmp_path / "cache")
    with hv4.client() as client:
        sync("full", client=client, settings=settings)
    before = sha(settings.catalog_path.read_bytes())

    if mutation == "columns":
        header, rows = read_csv_rows(hv4, "matches")
        header.append("season_id")
        for row in rows:
            row["season_id"] = "rs00000000000000"
        out = io.StringIO(newline="")
        writer = csv.DictWriter(out, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        raw = gzip.compress(out.getvalue().encode(), mtime=0)
        (hv4.root / "csv/matches.csv.gz").write_bytes(raw)
        hv4.rebuild("2026-10-23.2")
        # Restore the extra column after rebuild's _sync_aliases_and_duckdb
        (hv4.root / "csv/matches.csv.gz").write_bytes(raw)
        entry = hv4.descriptor["files"]["csv/matches.csv.gz"]
        entry.update(sha256=sha(raw), bytes=len(raw))
        hv4.objects[entry["object_key"]] = raw
        hv4.publish_descriptor()
    elif mutation == "duplicate_bridge":
        header, rows = read_csv_rows(hv4, "bridges")
        rows.append(dict(rows[0]))
        write_csv_rows(hv4, "bridges", header, rows)
        hv4.rebuild("2026-10-23.2")
    elif mutation == "dangling_bridge":
        header, rows = read_csv_rows(hv4, "bridges")
        bad = dict(rows[0])
        bad["external_id"] = "DANGLING"
        bad["reep_id"] = "rt99999999999999"
        rows.append(bad)
        write_csv_rows(hv4, "bridges", header, rows)
        hv4.rebuild("2026-10-23.2")
    else:
        header, rows = read_csv_rows(hv4, "redirects")
        rows.append(
            {
                "from_id": OLD,
                "to_id": OLD,
                "reason": "cycle",
                "created_at": "",
            }
        )
        write_csv_rows(hv4, "redirects", header, rows)
        hv4.rebuild("2026-10-23.2")

    with hv4.client() as client, pytest.raises(UnsupportedReleaseError):
        sync("full", client=client, settings=settings)
    assert sha(settings.catalog_path.read_bytes()) == before
