from __future__ import annotations

import json
from pathlib import Path

import pytest

from reep.errors import (
    ChecksumMismatchError,
    NetworkError,
    NotFoundError,
    UnsupportedReleaseError,
)
from reep.release import (
    ReleaseClient,
    ReleaseFile,
    ReleaseManifest,
    sha256_file,
    validate_stamp,
)
from tests.conftest import no_sleep
from tests.support.fixture_release import STAMP_1, STAMP_2, FixtureBucket
from tests.support.httpserver import FileServer


def test_latest_and_manifest(client: ReleaseClient) -> None:
    pointer = client.latest()
    assert pointer.stamp == STAMP_2
    manifest = client.manifest()
    assert manifest.stamp == STAMP_2
    assert manifest.schema_version == "bridge-register-v1"
    assert manifest.tier == "public_bridge_v1"
    assert "bridges" in manifest.tables
    entry = manifest.table_file("bridges")
    assert entry is not None
    assert entry.rows is not None
    assert entry.role == "canonical_csv"
    assert manifest.table_file("nope") is None
    assert manifest.file("changelog.json.gz").name == "changelog.json.gz"
    with pytest.raises(UnsupportedReleaseError):
        manifest.file("missing.txt")


def test_manifest_for_a_stamp(client: ReleaseClient) -> None:
    assert client.manifest(STAMP_1).stamp == STAMP_1


def test_manifest_for_a_missing_stamp(client: ReleaseClient) -> None:
    with pytest.raises(NotFoundError):
        client.manifest("20200101T000000Z")


def test_manifest_checksum_must_match_latest(
    client: ReleaseClient, bucket: FixtureBucket
) -> None:
    latest = bucket.root / "releases" / "latest.json"
    data = json.loads(latest.read_text())
    data["manifest_sha256"] = "0" * 64
    latest.write_text(json.dumps(data))
    with pytest.raises(ChecksumMismatchError):
        client.manifest()


def test_latest_with_an_unknown_schema(
    client: ReleaseClient, bucket: FixtureBucket
) -> None:
    latest = bucket.root / "releases" / "latest.json"
    data = json.loads(latest.read_text())
    data["schema_version"] = "reep-public-release-latest-v9"
    latest.write_text(json.dumps(data))
    with pytest.raises(UnsupportedReleaseError, match="Upgrade"):
        client.latest()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"manifest_version": 2}, "manifest_version"),
        ({"schema_version": "bridge-register-v9"}, "schema"),
        ({"stamp": "nope"}, "stamp"),
        ({"files": []}, "files"),
    ],
)
def test_manifest_shapes_that_are_refused(
    bucket: FixtureBucket, change: dict[str, object], message: str
) -> None:
    data = json.loads((bucket.release_dir(STAMP_1) / "release.json").read_text())
    data.update(change)
    with pytest.raises(UnsupportedReleaseError, match=message):
        ReleaseManifest.from_json(data)


def test_manifest_file_entries_are_checked(bucket: FixtureBucket) -> None:
    data = json.loads((bucket.release_dir(STAMP_1) / "release.json").read_text())
    data["files"]["csv/bridges.csv.gz"]["sha256"] = "xyz"
    with pytest.raises(UnsupportedReleaseError, match="SHA-256"):
        ReleaseManifest.from_json(data)
    with pytest.raises(UnsupportedReleaseError):
        ReleaseManifest.from_json([])


def test_download_checks_the_checksum_and_uses_the_cache(
    client: ReleaseClient, server: FileServer, tmp_path: Path
) -> None:
    manifest = client.manifest(STAMP_1)
    entry = manifest.file("csv/bridges.csv.gz")
    seen: list[tuple[int, int | None]] = []
    dest = client.download(
        entry, tmp_path / "b.csv.gz", progress=lambda d, t: seen.append((d, t))
    )
    assert sha256_file(dest) == entry.sha256
    assert seen[-1] == (entry.size, entry.size)
    key = f"/{entry.object_key}"
    assert server.requests[key] == 1
    client.download(entry, dest)
    assert server.requests[key] == 1


def test_download_resumes_after_a_broken_connection(
    client: ReleaseClient, server: FileServer, tmp_path: Path
) -> None:
    manifest = client.manifest(STAMP_1)
    entry = manifest.file("duckdb/reep-register-v1.duckdb")
    key = f"/{entry.object_key}"
    server.truncate(key, 4096)
    dest = client.download(entry, tmp_path / "r.duckdb")
    assert sha256_file(dest) == entry.sha256
    assert server.requests[key] == 2
    assert server.ranges[key] == [None, "bytes=4096-"]


def test_download_retries_server_errors(
    client: ReleaseClient, server: FileServer, tmp_path: Path
) -> None:
    entry = client.manifest(STAMP_1).file("csv/redirects.csv.gz")
    key = f"/{entry.object_key}"
    server.fail(key, 503, 500)
    client.download(entry, tmp_path / "r.csv.gz")
    assert server.requests[key] == 3


def test_download_gives_up_after_the_retries(
    server: FileServer, tmp_path: Path
) -> None:
    with ReleaseClient(server.url, sleep=no_sleep, retries=1) as rc:
        entry = rc.manifest(STAMP_1).file("csv/redirects.csv.gz")
        server.fail(f"/{entry.object_key}", 503, 503)
        with pytest.raises(NetworkError, match="failed"):
            rc.download(entry, tmp_path / "r.csv.gz")


def test_download_refuses_a_wrong_checksum(
    client: ReleaseClient, tmp_path: Path
) -> None:
    entry = client.manifest(STAMP_1).file("csv/redirects.csv.gz")
    wrong = ReleaseFile(
        name=entry.name,
        object_key=entry.object_key,
        sha256="0" * 64,
        size=entry.size,
    )
    with pytest.raises(ChecksumMismatchError):
        client.download(wrong, tmp_path / "r.csv.gz")
    assert not (tmp_path / "r.csv.gz").exists()
    assert not (tmp_path / "r.csv.gz.part").exists()


def test_download_replaces_a_damaged_cached_file(
    client: ReleaseClient, tmp_path: Path
) -> None:
    entry = client.manifest(STAMP_1).file("csv/redirects.csv.gz")
    dest = tmp_path / "r.csv.gz"
    dest.write_bytes(b"damaged")
    client.download(entry, dest)
    assert sha256_file(dest) == entry.sha256


def test_download_of_a_missing_object(client: ReleaseClient, tmp_path: Path) -> None:
    entry = ReleaseFile("x", "releases/none/x", "0" * 64, 1)
    with pytest.raises(NotFoundError):
        client.download(entry, tmp_path / "x")


def test_get_retries_then_fails(server: FileServer) -> None:
    with ReleaseClient(server.url, sleep=no_sleep, retries=2) as rc:
        server.fail("/releases/latest.json", 502, 429)
        assert rc.latest().stamp == STAMP_2
        server.fail("/releases/latest.json", 500, 500, 500)
        with pytest.raises(NetworkError):
            rc.latest()
        server.fail("/releases/latest.json", 403)
        with pytest.raises(NetworkError, match="403"):
            rc.latest()


def test_retry_after_header_is_honoured(server: FileServer) -> None:
    delays: list[float] = []
    server.faults.headers["/releases/latest.json"] = {"Retry-After": "2"}
    server.fail("/releases/latest.json", 429)
    with ReleaseClient(server.url, sleep=delays.append) as rc:
        rc.latest()
    assert delays == [2.0]


def test_fetch_bytes_and_url(client: ReleaseClient, server: FileServer) -> None:
    body = client.fetch_bytes("releases/latest.json")
    assert json.loads(body)["stamp"] == STAMP_2
    assert client.url("/releases/x") == f"{server.url}/releases/x"


def test_validate_stamp() -> None:
    assert validate_stamp(STAMP_1) == STAMP_1
    with pytest.raises(ValueError, match="release stamp"):
        validate_stamp("2026-01-01")
