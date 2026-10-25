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


def _v4_rows(bucket: Any, table: str) -> tuple[list[str], list[dict[str, str]]]:
    import csv
    import io

    raw = gzip.decompress((bucket.root / f"{table}.csv.gz").read_bytes()).decode()
    reader = csv.DictReader(io.StringIO(raw))
    return list(reader.fieldnames or []), list(reader)


def _v4_write_rows(
    bucket: Any, table: str, header: list[str], values: list[dict[str, Any]]
) -> None:
    import csv
    import io

    from reep import _v4
    from tests.support.v4_release import encode

    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    writer.writerows(values)
    (bucket.root / f"{table}.csv.gz").write_bytes(
        gzip.compress(out.getvalue().encode(), mtime=0)
    )
    manifest = json.loads((bucket.root / "manifest.json").read_text())
    manifest[_v4.COUNT_KEYS[table]] = len(values)
    (bucket.root / "manifest.json").write_bytes(encode(manifest))


def test_v4_change_feed_replays_all_seven_event_types_across_pages(
    tmp_path: Path,
) -> None:
    from tests.support.v4_release import STAMP, V4Bucket, encode

    v4 = V4Bucket(tmp_path / "v4-release")
    settings = make_settings(tmp_path / "v4-cache")
    with v4.client() as client:
        first = catalog.sync(client=client, settings=settings)
        assert first.method == "snapshot"
        assert first.stamp_after == STAMP

    e_hdr, e_rows = _v4_rows(v4, "entities")
    b_hdr, b_rows = _v4_rows(v4, "bridges")
    r_hdr, r_rows = _v4_rows(v4, "redirects")

    # Mutate entities:
    # - label_changed on rt8df038ffeaed40 ("Home United" -> "Home United FC")
    # - entity_withdrawn on rv7bffdbdab61249
    # - entity_added on rp00000000000999
    withdrawn_id = "rv7bffdbdab61249"
    added_id = "rp00000000000999"
    renamed_id = "rt8df038ffeaed40"
    new_e_rows: list[dict[str, str]] = []
    added_entity_template: dict[str, str] = {}
    for row in e_rows:
        if row["reep_id"] == withdrawn_id:
            continue
        if row["reep_id"] == renamed_id:
            row = {**row, "canonical_name": "Home United FC"}
        if not added_entity_template:
            added_entity_template = dict(row)
        new_e_rows.append(row)
    added_entity = {
        **added_entity_template,
        "reep_id": added_id,
        "entity_type": "person",
        "canonical_name": "Added V4 Player",
    }
    new_e_rows.append(added_entity)

    # Mutate bridges:
    # - bridge_removed on any bridge pointing to withdrawn_id
    # - bridge_repointed on transfermarkt/team/TH -> added_id
    # - bridge_added on opta/player/NEW999 -> added_id
    new_b_rows: list[dict[str, str]] = []
    repointed_bridge: dict[str, str] | None = None
    removed_bridges: list[dict[str, str]] = []
    for row in b_rows:
        if row["reep_id"] == withdrawn_id:
            removed_bridges.append(row)
            continue
        if (
            row["provider"] == "transfermarkt"
            and row["namespace"] == "team"
            and row["external_id"] == "TH"
        ):
            row = {**row, "reep_id": added_id, "entity_type": "person"}
            repointed_bridge = row
        new_b_rows.append(row)
    assert repointed_bridge is not None
    added_bridge = {
        **new_b_rows[0],
        "reep_id": added_id,
        "provider": "opta",
        "namespace": "player",
        "external_id": "NEW999",
        "entity_type": "person",
    }
    new_b_rows.append(added_bridge)

    # Mutate redirects:
    # - redirect_added from withdrawn_id -> renamed_id
    added_redirect = {
        "from_id": withdrawn_id,
        "to_id": renamed_id,
        "reason": "merged",
        "created_at": "2026-10-23T12:00:00Z",
    }
    new_r_rows = [*r_rows, added_redirect]

    _v4_write_rows(v4, "entities", e_hdr, new_e_rows)
    _v4_write_rows(v4, "bridges", b_hdr, new_b_rows)
    _v4_write_rows(v4, "redirects", r_hdr, new_r_rows)
    next_stamp = "2026-10-23.2"
    v4.rebuild(next_stamp)

    events: list[dict[str, Any]] = [
        {"seq": 1, "type": "entity_added", "reep_id": added_id, "entity": added_entity},
        {"seq": 2, "type": "entity_withdrawn", "reep_id": withdrawn_id},
        {
            "seq": 3,
            "type": "label_changed",
            "reep_id": renamed_id,
            "current_label": "Home United FC",
        },
        {"seq": 4, "type": "bridge_added", "bridge": added_bridge},
        {"seq": 5, "type": "bridge_repointed", "after": repointed_bridge},
    ]
    seq = 6
    for rb in removed_bridges:
        events.append(
            {
                "seq": seq,
                "type": "bridge_removed",
                "provider": rb["provider"],
                "namespace": rb["namespace"],
                "external_id": rb["external_id"],
            }
        )
        seq += 1
    events.append({"seq": seq, "type": "redirect_added", "redirect": added_redirect})

    feed_doc = {
        "schema": "reep-change-feed/v1",
        "status": "ready",
        "reason": None,
        "previous_release": STAMP,
        "current_release": next_stamp,
        "pages": [
            {
                "page_index": 0,
                "cursor": "page:0000",
                "next_cursor": "page:0001",
                "has_more": True,
                "events": events[:3],
            },
            {
                "page_index": 1,
                "cursor": "page:0001",
                "next_cursor": None,
                "has_more": False,
                "events": events[3:],
            },
        ],
    }
    v4.objects[f"releases/{next_stamp}/change_feed.json"] = encode(feed_doc)

    with v4.client() as client:
        replayed = catalog.sync(client=client, settings=settings)
    assert replayed.method == "replay"
    assert replayed.stamp_before == STAMP
    assert replayed.stamp_after == next_stamp

    fresh_settings = make_settings(tmp_path / "v4-fresh")
    with v4.client() as client:
        catalog.sync(client=client, settings=fresh_settings, method="snapshot")
    _assert_same_data(settings.catalog_path, fresh_settings.catalog_path)


def test_v4_change_feed_unavailable_status_is_explicit(tmp_path: Path) -> None:
    from tests.support.v4_release import V4Bucket, encode

    v4 = V4Bucket(tmp_path / "v4-unavail")
    settings = make_settings(tmp_path / "v4-unavail-cache")
    with v4.client() as client:
        catalog.sync(client=client, settings=settings)

    next_stamp = "2026-10-23.2"
    v4.rebuild(next_stamp)
    v4.objects[f"releases/{next_stamp}/change_feed.json"] = encode(
        {
            "schema": "reep-change-feed/v1",
            "status": "unavailable",
            "reason": "missing_prior_baseline",
            "previous_release": None,
            "current_release": next_stamp,
            "pages": [],
        }
    )
    with v4.client() as client:
        with pytest.raises(ReplayError, match="missing_prior_baseline"):
            catalog.sync(client=client, settings=settings, method="replay")
        fallback = catalog.sync(client=client, settings=settings, method="auto")
    assert fallback.method == "snapshot"
    assert "missing_prior_baseline" in fallback.notes[0]


def test_v4_replay_preserves_provenance_and_clears_stale_manifest_hash(
    tmp_path: Path,
) -> None:
    from tests.support.v4_release import STAMP, V4Bucket

    v4 = V4Bucket(tmp_path / "bucket")
    settings = make_settings(tmp_path / "cache")
    with v4.client() as client:
        catalog.sync(client=client, settings=settings, profile="full")

    con_before = duckdb.connect(str(settings.catalog_path), read_only=True)
    meta_before = dict(
        con_before.execute("SELECT key, value FROM release_metadata").fetchall()
    )
    con_before.close()

    next_stamp = "2026-10-23.2"
    v4.rebuild(next_stamp)
    feed_doc = {
        "schema": "reep-change-feed/v1",
        "status": "ready",
        "channel": "downloads",
        "previous_release": STAMP,
        "current_release": next_stamp,
        "pages": [
            {
                "page_index": 0,
                "cursor": "page:0000",
                "next_cursor": None,
                "has_more": False,
                "events": [
                    {
                        "seq": 1,
                        "type": "bridge_added",
                        "bridge": {
                            "reep_id": "rt8df038ffeaed40",
                            "provider": "sofifa",
                            "namespace": "team",
                            "external_id": "SOFIFA_999",
                            "entity_type": "team",
                        },
                    },
                    {
                        "seq": 2,
                        "type": "bridge_added",
                        "bridge": {
                            "reep_id": "rt8df038ffeaed40",
                            "provider": "whoscored",
                            "namespace": "team",
                            "external_id": "WS_888",
                            "entity_type": "team",
                            "independence_bucket": "OPTA",
                            "route_scope": "structural",
                            "rule_id": "C2_EXACT",
                            "confidence_tier": "0.95",
                            "first_seen_release": "2026-10-20.1",
                            "last_confirmed_release": "2026-10-23.2",
                        },
                    },
                ],
            }
        ],
    }
    res = catalog.replay_change_feed(feed_doc, settings=settings)
    assert res.method == "replay"
    assert res.stamp_after == next_stamp

    con_after = duckdb.connect(str(settings.catalog_path), read_only=True)
    meta_after = dict(
        con_after.execute("SELECT key, value FROM release_metadata").fetchall()
    )
    sofifa_row = con_after.execute(
        "SELECT provider, independence_bucket, route_scope, rule_id, confidence "
        "FROM bridges WHERE external_id = 'SOFIFA_999'"
    ).fetchone()
    ws_row = con_after.execute(
        "SELECT provider, independence_bucket, route_scope, rule_id, "
        "confidence, attached_at "
        "FROM bridges WHERE external_id = 'WS_888'"
    ).fetchone()
    con_after.close()

    assert sofifa_row == ("sofifa", "SPORTMONKS", None, None, None)
    assert ws_row == (
        "whoscored",
        "OPTA",
        "structural",
        "C2_EXACT",
        "0.95",
        "2026-10-23.2",
    )
    assert meta_after.get("source_stamp") == next_stamp
    assert meta_after.get("sdk_manifest_sha256") != meta_before.get(
        "sdk_manifest_sha256"
    )

    with v4.client() as client:
        sync_after = catalog.sync(client=client, settings=settings, profile="full")
    assert sync_after.stamp_after == next_stamp

