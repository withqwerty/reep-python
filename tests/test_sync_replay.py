from __future__ import annotations

import gzip
import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from reep import Register, _store, catalog
from reep import changelog as changelog_module
from reep.config import Settings
from reep.errors import CatalogInUseError, ConfigError, ReplayError
from reep.release import ReleaseClient
from tests.conftest import make_settings, no_sleep
from tests.support.fixture_release import (
    PALMER,
    STAMP_0,
    STAMP_1,
    STAMP_2,
    FixtureBucket,
    write_latest,
)
from tests.support.httpserver import FileServer


@pytest.fixture
def own(
    tmp_path: Path, bucket: FixtureBucket
) -> Iterator[tuple[FixtureBucket, FileServer]]:
    """A copy of the fixture bucket that a test can change, with its own server."""
    root = tmp_path / "bucket"
    shutil.copytree(bucket.root, root)
    server = FileServer(root).start()
    yield FixtureBucket(root), server
    server.stop()


def _client(server: FileServer) -> ReleaseClient:
    return ReleaseClient(server.url, sleep=no_sleep, timeout=10)


def _table(path: Path, sql: str) -> set[tuple[Any, ...]]:
    con = duckdb.connect(str(path), read_only=True)
    try:
        return {tuple(row) for row in con.execute(sql).fetchall()}
    finally:
        con.close()


BRIDGES = "SELECT provider, namespace, external_id, reep_id FROM bridges"
ENTITIES = "SELECT * FROM entities"
REDIRECTS = "SELECT from_id, coalesce(to_id, ''), reason FROM redirects"


def _assert_same_data(a: Path, b: Path) -> None:
    assert _table(a, BRIDGES) == _table(b, BRIDGES)
    assert _table(a, ENTITIES) == _table(b, ENTITIES)
    assert _table(a, REDIRECTS) == _table(b, REDIRECTS)


def _fresh(tmp_path: Path, server: FileServer, stamp: str, **kwargs: Any) -> Settings:
    settings = make_settings(tmp_path / f"fresh-{stamp}", server.url)
    with _client(server) as rc:
        catalog.sync(stamp=stamp, settings=settings, client=rc, **kwargs)
    return settings


def test_replay_gives_the_same_data_as_a_fresh_build(
    tmp_path: Path, server: FileServer
) -> None:
    settings = make_settings(tmp_path / "cache", server.url)
    with _client(server) as rc:
        catalog.sync(stamp=STAMP_1, settings=settings, client=rc)
        result = catalog.sync(settings=settings, client=rc)
    assert result.method == "replay"
    assert result.stamp_before == STAMP_1
    assert result.stamp_after == STAMP_2
    assert result.notes == ("replayed 1 changelog(s)",)
    assert result.downloaded_bytes > 0
    fresh = _fresh(tmp_path, server, STAMP_2)
    _assert_same_data(settings.catalog_path, fresh.catalog_path)
    state = catalog.state(settings)
    assert state is not None
    assert state.source == "replay"
    assert state.stamp == STAMP_2
    assert state.bridge_attributes_stamp == STAMP_1
    assert not settings.downloads_dir.exists() or not any(
        settings.downloads_dir.iterdir()
    )
    with Register.local(settings=settings) as reg:
        added = reg.resolve("understat/player/u1")
        assert added.reep_id == PALMER
        assert added.bridge is not None
        assert added.bridge.rung is None
        kept = reg.resolve("transfermarkt/spieler/568177")
        assert kept.bridge is not None
        assert kept.bridge.rung == "corroborated-mint"
        assert reg.search("new player")[0].label == "New Player"
        assert reg.release.extra["sdk_replayed_from"] == STAMP_1


def test_replay_of_two_releases_from_an_old_format(
    tmp_path: Path, server: FileServer
) -> None:
    settings = make_settings(tmp_path / "cache", server.url)
    with _client(server) as rc:
        catalog.sync(stamp=STAMP_0, settings=settings, client=rc)
        result = catalog.sync(settings=settings, client=rc)
    assert result.method == "replay"
    assert result.notes == ("replayed 2 changelog(s)",)
    fresh = _fresh(tmp_path, server, STAMP_2)
    _assert_same_data(settings.catalog_path, fresh.catalog_path)


def test_replay_with_aliases_and_the_full_profile(
    tmp_path: Path, server: FileServer
) -> None:
    variants: tuple[dict[str, Any], ...] = ({"with_aliases": True}, {"profile": "full"})
    for index, kwargs in enumerate(variants):
        settings = make_settings(tmp_path / f"cache-{index}", server.url)
        with _client(server) as rc:
            catalog.sync(stamp=STAMP_1, settings=settings, client=rc, **kwargs)
            result = catalog.sync(settings=settings, client=rc)
        assert result.method == "replay", result.notes
        fresh = make_settings(tmp_path / f"fresh-{index}", server.url)
        with _client(server) as rc:
            catalog.sync(stamp=STAMP_2, settings=fresh, client=rc, **kwargs)
        _assert_same_data(settings.catalog_path, fresh.catalog_path)
        sql = "SELECT * FROM aliases"
        assert _table(settings.catalog_path, sql) == _table(fresh.catalog_path, sql)


def test_snapshot_method_and_unknown_method(tmp_path: Path, server: FileServer) -> None:
    settings = make_settings(tmp_path / "cache", server.url)
    with _client(server) as rc:
        catalog.sync(stamp=STAMP_1, settings=settings, client=rc)
        assert catalog.sync(settings=settings, client=rc, method="snapshot").method == (
            "snapshot"
        )
        with pytest.raises(ConfigError, match="method"):
            catalog.sync(settings=settings, client=rc, method="delta")


def test_force_downloads_a_snapshot(tmp_path: Path, server: FileServer) -> None:
    settings = make_settings(tmp_path / "cache", server.url)
    with _client(server) as rc:
        catalog.sync(stamp=STAMP_1, settings=settings, client=rc)
        assert (
            catalog.sync(settings=settings, client=rc, force=True).method == "snapshot"
        )


@pytest.mark.parametrize(
    ("setup", "note"),
    [
        ("profile", "the profile changed"),
        ("aliases", "the table set changed"),
        ("older", "not newer"),
    ],
)
def test_blockers_lead_to_a_snapshot(
    tmp_path: Path, server: FileServer, setup: str, note: str
) -> None:
    settings = make_settings(tmp_path / "cache", server.url)
    with _client(server) as rc:
        if setup == "older":
            catalog.sync(settings=settings, client=rc)
            result = catalog.sync(stamp=STAMP_1, settings=settings, client=rc)
        else:
            catalog.sync(stamp=STAMP_1, settings=settings, client=rc)
            kwargs: dict[str, Any] = (
                {"profile": "full"} if setup == "profile" else {"with_aliases": True}
            )
            result = catalog.sync(settings=settings, client=rc, **kwargs)
    assert result.method == "snapshot"
    assert any(note in n for n in result.notes)


def test_replay_method_raises_when_blocked(tmp_path: Path, server: FileServer) -> None:
    settings = make_settings(tmp_path / "cache", server.url)
    with _client(server) as rc:
        with pytest.raises(ReplayError, match="no local catalogue"):
            catalog.sync(settings=settings, client=rc, method="replay")
        catalog.sync(stamp=STAMP_0, settings=settings, client=rc)
        with pytest.raises(ReplayError, match="more than 1"):
            catalog.sync(
                settings=settings, client=rc, method="replay", max_replay_steps=1
            )


def test_an_imported_catalogue_without_a_stamp_gets_a_snapshot(
    tmp_path: Path, server: FileServer, bucket: FixtureBucket
) -> None:
    settings = make_settings(tmp_path / "cache", server.url)
    source = tmp_path / "files"
    shutil.copytree(bucket.release_dir(STAMP_1) / "csv", source)
    catalog.import_release(source, settings=settings)
    with _client(server) as rc:
        result = catalog.sync(settings=settings, client=rc)
    assert result.method == "snapshot"
    assert any("no release stamp" in n for n in result.notes)


def test_an_imported_catalogue_with_a_stamp_replays(
    tmp_path: Path, server: FileServer, bucket: FixtureBucket
) -> None:
    settings = make_settings(tmp_path / "cache", server.url)
    source = tmp_path / "files"
    shutil.copytree(bucket.release_dir(STAMP_1) / "csv", source)
    shutil.copyfile(
        bucket.release_dir(STAMP_1) / "release.json", source / "release.json"
    )
    catalog.import_release(source, settings=settings)
    with _client(server) as rc:
        assert catalog.sync(settings=settings, client=rc).method == "replay"


def test_a_long_chain_gets_a_snapshot(tmp_path: Path, server: FileServer) -> None:
    settings = make_settings(tmp_path / "cache", server.url)
    with _client(server) as rc:
        catalog.sync(stamp=STAMP_0, settings=settings, client=rc)
        result = catalog.sync(settings=settings, client=rc, max_replay_steps=1)
    assert result.method == "snapshot"
    assert any("more than 1 releases behind" in n for n in result.notes)
    assert result.stamp_after == STAMP_2


def _rewrite_changelog(
    own_bucket: FixtureBucket, stamp: str, change: dict[str, Any]
) -> None:
    """Change a changelog and update its manifest entry and the latest pointer."""
    release = own_bucket.release_dir(stamp)
    path = release / "changelog.json.gz"
    doc = json.loads(gzip.decompress(path.read_bytes()))
    doc.update(change)
    path.write_bytes(gzip.compress(json.dumps(doc).encode()))
    manifest_path = release / "release.json"
    manifest = json.loads(manifest_path.read_text())
    entry = manifest["files"]["changelog.json.gz"]
    import hashlib

    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    entry["bytes"] = path.stat().st_size
    manifest_path.write_text(json.dumps(manifest))
    write_latest(own_bucket.root, STAMP_2)


@pytest.mark.parametrize(
    ("change", "note"),
    [
        ({"changelog_version": 2}, "version 2"),
        ({"prior": None}, "has no prior"),
        ({"new": {"stamp": STAMP_1}}, "names release"),
        ({"prior": {"stamp": "20251201T000000Z"}}, "skips release"),
    ],
)
def test_changelog_problems_lead_to_a_snapshot(
    tmp_path: Path,
    own: tuple[FixtureBucket, FileServer],
    change: dict[str, Any],
    note: str,
) -> None:
    own_bucket, own_server = own
    settings = make_settings(tmp_path / "cache", own_server.url)
    with _client(own_server) as rc:
        catalog.sync(stamp=STAMP_1, settings=settings, client=rc)
        _rewrite_changelog(own_bucket, STAMP_2, change)
        result = catalog.sync(settings=settings, client=rc)
    assert result.method == "snapshot"
    assert any(note in n for n in result.notes), result.notes
    state = catalog.state(settings)
    assert state is not None
    assert state.stamp == STAMP_2


def test_a_pruned_release_leads_to_a_snapshot(
    tmp_path: Path, own: tuple[FixtureBucket, FileServer]
) -> None:
    own_bucket, own_server = own
    settings = make_settings(tmp_path / "cache", own_server.url)
    with _client(own_server) as rc:
        catalog.sync(stamp=STAMP_0, settings=settings, client=rc)
        (own_bucket.release_dir(STAMP_1) / "release.json").unlink()
        result = catalog.sync(settings=settings, client=rc)
    assert result.method == "snapshot"
    assert any("no longer on the release bucket" in n for n in result.notes)


def test_a_missing_changelog_leads_to_a_snapshot(
    tmp_path: Path, own: tuple[FixtureBucket, FileServer]
) -> None:
    own_bucket, own_server = own
    settings = make_settings(tmp_path / "cache", own_server.url)
    manifest_path = own_bucket.release_dir(STAMP_2) / "release.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["files"]["changelog.json.gz"]
    manifest_path.write_text(json.dumps(manifest))
    write_latest(own_bucket.root, STAMP_2)
    with _client(own_server) as rc:
        catalog.sync(stamp=STAMP_1, settings=settings, client=rc)
        result = catalog.sync(settings=settings, client=rc)
    assert result.method == "snapshot"
    assert any("has no changelog" in n for n in result.notes)


def test_a_large_chain_gets_a_snapshot(
    tmp_path: Path, own: tuple[FixtureBucket, FileServer]
) -> None:
    own_bucket, own_server = own
    settings = make_settings(tmp_path / "cache", own_server.url)
    manifest_path = own_bucket.release_dir(STAMP_2) / "release.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["changelog.json.gz"]["bytes"] = 10**9
    manifest_path.write_text(json.dumps(manifest))
    write_latest(own_bucket.root, STAMP_2)
    with _client(own_server) as rc:
        catalog.sync(stamp=STAMP_1, settings=settings, client=rc)
        result = catalog.sync(settings=settings, client=rc)
    assert result.method == "snapshot"
    assert any("larger than the bridges snapshot" in n for n in result.notes)


@pytest.mark.parametrize("damage", ["redirects", "count", "orphan"])
def test_replay_checks_catch_drift(
    tmp_path: Path,
    server: FileServer,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    settings = make_settings(tmp_path / "cache", server.url)
    with _client(server) as rc:
        catalog.sync(stamp=STAMP_1, settings=settings, client=rc)
        if damage == "redirects":
            monkeypatch.setattr(
                changelog_module, "replay_redirects", lambda prior, events: {}
            )
        else:
            original = changelog_module.apply_staged_bridge_changes

            def damaged(con: duckdb.DuckDBPyConnection, table: str, name: str) -> None:
                original(con, table, name)
                if damage == "count":
                    con.execute(f"DELETE FROM {table} WHERE provider = 'understat'")
                else:
                    con.execute(
                        f"UPDATE {table} SET reep_id = 'rp0000000000eeee' "
                        "WHERE provider = 'understat'"
                    )

            monkeypatch.setattr(
                changelog_module, "apply_staged_bridge_changes", damaged
            )
        result = catalog.sync(settings=settings, client=rc)
        assert result.method == "snapshot"
        expected = {
            "redirects": "merge redirects",
            "count": "rows",
            "orphan": "name entities",
        }[damage]
        assert any(expected in n for n in result.notes), result.notes
        catalog.sync(stamp=STAMP_1, settings=settings, client=rc, force=True)
        with pytest.raises(ReplayError):
            catalog.sync(settings=settings, client=rc, method="replay")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows locks open files")
def test_replay_while_a_register_is_open(tmp_path: Path, server: FileServer) -> None:
    settings = make_settings(tmp_path / "cache", server.url)
    with _client(server) as rc:
        catalog.sync(stamp=STAMP_1, settings=settings, client=rc)
        with Register.local(settings=settings) as reg:
            assert reg.release.stamp == STAMP_1
            result = catalog.sync(settings=settings, client=rc)
            assert result.method == "replay"
            assert reg.resolve("transfermarkt/spieler/568177").reep_id == PALMER
    with Register.local(settings=settings) as reg:
        assert reg.release.stamp == STAMP_2


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows locks open files")
def test_replace_while_open_on_windows(tmp_path: Path, server: FileServer) -> None:
    settings = make_settings(tmp_path / "cache", server.url)
    with _client(server) as rc:
        catalog.sync(stamp=STAMP_1, settings=settings, client=rc)
        with Register.local(settings=settings), pytest.raises(CatalogInUseError):
            catalog.sync(settings=settings, client=rc, method="snapshot")
    state = catalog.state(settings)
    assert state is not None
    assert state.stamp == STAMP_1


def test_catalog_in_use_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: Path, target: Path) -> Path:
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(CatalogInUseError) as exc:
        _store.replace_file(tmp_path / "a", tmp_path / "b")
    assert exc.value.exit_code == 7
