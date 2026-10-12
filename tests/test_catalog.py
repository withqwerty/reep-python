from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import duckdb
import pytest

from reep import catalog
from reep.config import Settings
from reep.errors import ChecksumMismatchError, ConfigError, UnsupportedReleaseError
from reep.register import Register
from reep.release import ReleaseClient
from tests.support.fixture_release import (
    PALMER,
    STAMP_0,
    STAMP_1,
    STAMP_2,
    FixtureBucket,
    write_latest,
)
from tests.support.httpserver import FileServer


def _rows(path: Path, table: str) -> int:
    con = duckdb.connect(str(path), read_only=True)
    try:
        row = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    finally:
        con.close()
    assert row is not None
    return int(row[0])


def test_sync_downloads_a_slim_snapshot(
    settings: Settings, client: ReleaseClient
) -> None:
    progress: list[str] = []
    result = catalog.sync(
        settings=settings, client=client, progress=lambda n, d, t: progress.append(n)
    )
    assert result.method == "snapshot"
    assert result.changed
    assert result.stamp_before is None
    assert result.stamp_after == STAMP_2
    assert result.profile == "slim"
    assert result.downloaded_bytes > 0
    assert "csv/bridges.csv.gz" in progress
    state = catalog.state(settings)
    assert state is not None
    assert state.stamp == STAMP_2
    assert state.source == "download"
    assert state.bridge_attributes_stamp == STAMP_2
    assert {"bridges", "entities", "redirects", "entity_search"} <= set(state.tables)
    assert "aliases" not in state.tables
    assert not (settings.downloads_dir / STAMP_2).exists()


def test_sync_is_up_to_date_on_the_second_run(
    settings: Settings, client: ReleaseClient, server: FileServer
) -> None:
    catalog.sync(settings=settings, client=client)
    server.reset()
    result = catalog.sync(settings=settings, client=client)
    assert result.method == "up_to_date"
    assert not result.changed
    assert result.downloaded_bytes == 0
    assert not any("csv/" in path for path in server.requests)


def test_force_and_keep_downloads(settings: Settings, client: ReleaseClient) -> None:
    catalog.sync(settings=settings, client=client)
    result = catalog.sync(
        settings=settings, client=client, force=True, keep_downloads=True
    )
    assert result.method == "snapshot"
    assert (settings.downloads_dir / STAMP_2 / "csv" / "bridges.csv.gz").is_file()


def test_full_profile_and_aliases(settings: Settings, client: ReleaseClient) -> None:
    catalog.sync("full", settings=settings, client=client)
    state = catalog.state(settings)
    assert state is not None
    assert state.profile == "full"
    assert {"aliases", "players"} <= set(state.tables)
    # A later sync keeps the profile.
    assert catalog.sync(settings=settings, client=client).method == "up_to_date"


def test_slim_with_aliases_is_kept(settings: Settings, client: ReleaseClient) -> None:
    catalog.sync(settings=settings, client=client, with_aliases=True, stamp=STAMP_1)
    state = catalog.state(settings)
    assert state is not None
    assert "aliases" in state.tables
    catalog.sync(settings=settings, client=client)
    state = catalog.state(settings)
    assert state is not None
    assert state.stamp == STAMP_2
    assert "aliases" in state.tables


def test_unknown_profile(settings: Settings, client: ReleaseClient) -> None:
    with pytest.raises(ConfigError, match="profile"):
        catalog.sync("medium", settings=settings, client=client)


def test_sync_creates_its_own_client(settings: Settings) -> None:
    result = catalog.sync(settings=settings)
    assert result.stamp_after == STAMP_2


def test_row_counts_match_the_manifest(
    settings: Settings, client: ReleaseClient, bucket: FixtureBucket
) -> None:
    catalog.sync(settings=settings, client=client)
    manifest = json.loads((bucket.release_dir(STAMP_2) / "release.json").read_text())
    for table in ("bridges", "entities", "redirects"):
        expected = manifest["row_counts"][f"csv/{table}.csv.gz"]
        assert _rows(settings.catalog_path, table) == expected


def test_old_release_without_bridge_metadata_columns(
    settings: Settings, client: ReleaseClient
) -> None:
    catalog.sync(settings=settings, client=client, stamp=STAMP_0)
    with Register.local(settings=settings) as reg:
        res = reg.resolve("transfermarkt/spieler/568177")
    assert res.reep_id == PALMER
    assert res.bridge is not None
    assert res.bridge.rung is None


def test_state_without_a_catalogue(settings: Settings) -> None:
    assert catalog.state(settings) is None


def _copy_release(bucket: FixtureBucket, stamp: str, dest: Path, *, flat: bool) -> Path:
    source = bucket.release_dir(stamp)
    dest.mkdir(parents=True)
    for path in (source / "csv").glob("*.csv.gz"):
        target = dest / path.name if flat else dest / "csv" / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return dest


@pytest.mark.parametrize("flat", [True, False])
def test_import_with_a_local_manifest(
    settings: Settings, bucket: FixtureBucket, tmp_path: Path, flat: bool
) -> None:
    source = _copy_release(bucket, STAMP_1, tmp_path / "files", flat=flat)
    shutil.copyfile(
        bucket.release_dir(STAMP_1) / "release.json", source / "release.json"
    )
    result = catalog.import_release(source, settings=settings, with_aliases=True)
    assert result.method == "import"
    assert result.stamp_after == STAMP_1
    assert result.notes == ()
    state = catalog.state(settings)
    assert state is not None
    assert state.source == "import"
    assert "aliases" in state.tables
    assert state.import_fingerprint == catalog.local_fingerprint(source)


def test_import_checks_files_against_the_bucket_manifest(
    settings: Settings, bucket: FixtureBucket, tmp_path: Path, client: ReleaseClient
) -> None:
    source = _copy_release(bucket, STAMP_1, tmp_path / "files", flat=True)
    result = catalog.import_release(
        source, settings=settings, stamp=STAMP_1, client=client
    )
    assert result.stamp_after == STAMP_1
    with pytest.raises(ChecksumMismatchError, match="same release"):
        catalog.import_release(source, settings=settings, stamp=STAMP_2, client=client)


def test_import_without_a_manifest(
    settings: Settings, bucket: FixtureBucket, tmp_path: Path
) -> None:
    source = _copy_release(bucket, STAMP_1, tmp_path / "files", flat=True)
    result = catalog.import_release(source, settings=settings, profile="full")
    assert result.stamp_after is None
    assert result.notes
    state = catalog.state(settings)
    assert state is not None
    assert "players" in state.tables


def test_import_errors(
    settings: Settings, bucket: FixtureBucket, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError, match="not a directory"):
        catalog.import_release(tmp_path / "missing", settings=settings)
    source = _copy_release(bucket, STAMP_1, tmp_path / "files", flat=True)
    (source / "bridges.csv.gz").unlink()
    with pytest.raises(ConfigError, match=r"bridges\.csv\.gz"):
        catalog.import_release(source, settings=settings)
    shutil.copyfile(
        bucket.release_dir(STAMP_2) / "release.json", source / "release.json"
    )
    with pytest.raises(ConfigError, match="is for release"):
        catalog.import_release(source, settings=settings, stamp=STAMP_1)


def _rewrite_table(path: Path, transform: dict[str, str]) -> None:
    text = gzip.decompress(path.read_bytes()).decode()
    for old, new in transform.items():
        text = text.replace(old, new)
    path.write_bytes(gzip.compress(text.encode()))


def test_import_refuses_an_unknown_redirect_reason(
    settings: Settings, bucket: FixtureBucket, tmp_path: Path
) -> None:
    source = _copy_release(bucket, STAMP_1, tmp_path / "files", flat=True)
    _rewrite_table(source / "redirects.csv.gz", {"withheld": "teleported"})
    with pytest.raises(UnsupportedReleaseError, match="teleported"):
        catalog.import_release(source, settings=settings)
    assert not settings.catalog_path.exists()
    assert not list(settings.catalog_path.parent.glob("*.tmp"))


def test_import_refuses_an_unknown_entity_status(
    settings: Settings, bucket: FixtureBucket, tmp_path: Path
) -> None:
    source = _copy_release(bucket, STAMP_1, tmp_path / "files", flat=True)
    _rewrite_table(source / "entities.csv.gz", {",retired,": ",archived,"})
    with pytest.raises(UnsupportedReleaseError, match="archived"):
        catalog.import_release(source, settings=settings)


def test_import_refuses_a_table_without_required_columns(
    settings: Settings, bucket: FixtureBucket, tmp_path: Path
) -> None:
    source = _copy_release(bucket, STAMP_1, tmp_path / "files", flat=True)
    _rewrite_table(source / "bridges.csv.gz", {"external_id": "ext"})
    with pytest.raises(UnsupportedReleaseError, match="external_id"):
        catalog.import_release(source, settings=settings)


def test_row_count_mismatch_is_refused(
    settings: Settings, bucket: FixtureBucket, tmp_path: Path
) -> None:
    source = _copy_release(bucket, STAMP_1, tmp_path / "files", flat=True)
    manifest = json.loads((bucket.release_dir(STAMP_1) / "release.json").read_text())
    manifest["files"]["csv/redirects.csv.gz"]["rows"] = 99
    (source / "release.json").write_text(json.dumps(manifest))
    with pytest.raises(UnsupportedReleaseError, match="99"):
        catalog.import_release(source, settings=settings)


def test_failed_build_keeps_the_old_catalogue(
    settings: Settings, client: ReleaseClient, bucket: FixtureBucket, tmp_path: Path
) -> None:
    catalog.sync(settings=settings, client=client)
    source = _copy_release(bucket, STAMP_1, tmp_path / "files", flat=True)
    _rewrite_table(source / "redirects.csv.gz", {"withheld": "teleported"})
    with pytest.raises(UnsupportedReleaseError):
        catalog.import_release(source, settings=settings)
    state = catalog.state(settings)
    assert state is not None
    assert state.stamp == STAMP_2


def test_register_local_with_csv_dir_builds_once(
    settings: Settings, bucket: FixtureBucket, tmp_path: Path
) -> None:
    source = _copy_release(bucket, STAMP_1, tmp_path / "files", flat=True)
    shutil.copyfile(
        bucket.release_dir(STAMP_1) / "release.json", source / "release.json"
    )
    with Register.local(csv_dir=source, settings=settings) as reg:
        assert reg.release.stamp == STAMP_1
        built = reg.release.built_at
    before = settings.catalog_path.stat().st_mtime_ns
    with Register.local(csv_dir=source, settings=settings) as reg:
        assert reg.release.built_at == built
    assert settings.catalog_path.stat().st_mtime_ns == before


def test_latest_pointer_can_move(bucket: FixtureBucket, client: ReleaseClient) -> None:
    write_latest(bucket.root, STAMP_1)
    assert client.latest().stamp == STAMP_1
