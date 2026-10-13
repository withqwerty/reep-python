"""Tests against the live public release. Run them with ``pytest -m network``.

They download about 210 MB. Point ``--basetemp`` at a disk with space.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from reep import Register, catalog
from reep.changelog import Changelog
from reep.config import Settings
from reep.models import IdStatus, ResolutionStatus
from reep.release import ReleaseClient

pytestmark = pytest.mark.network

# Reep IDs are stable across releases, so these values hold for later releases.
PALMER = "rp53af22bbeaa667"
MERGED_FROM = "rc00cfa6784c6c23"
MERGED_INTO = "rc6c1f16f6b8b702"


@pytest.fixture(scope="module")
def live_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    cache = tmp_path_factory.mktemp("live-cache")
    settings = Settings.from_env({"REEP_CACHE_DIR": str(cache)})
    result = catalog.sync(settings=settings)
    assert result.method == "snapshot"
    return settings


def test_live_slim_store_resolves_and_redirects(live_settings: Settings) -> None:
    with Register.local(settings=live_settings) as reg:
        info = reg.release
        assert info.stamp is not None
        assert info.source == "download"
        res = reg.resolve("transfermarkt/spieler/568177")
        assert res.status is ResolutionStatus.FOUND
        assert res.reep_id == PALMER
        state = reg.current(MERGED_FROM)
        assert state.status is IdStatus.REDIRECTED
        assert state.current_id == MERGED_INTO
        assert reg.translate("transfermarkt/spieler/568177", "fbref").targets


def test_live_sync_is_up_to_date_afterwards(live_settings: Settings) -> None:
    result = catalog.sync(settings=live_settings)
    assert result.method == "up_to_date"


def test_cache_directory_is_used(live_settings: Settings) -> None:
    assert live_settings.catalog_path.is_file()
    assert Path(live_settings.cache_dir) in live_settings.catalog_path.parents


def _difference(a: Path, b: Path, sql: str) -> int:
    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{a}' AS a (READ_ONLY)")
        con.execute(f"ATTACH '{b}' AS b (READ_ONLY)")
        left = sql.format(db="a")
        right = sql.format(db="b")
        row = con.execute(
            f"SELECT (SELECT count(*) FROM ({left} EXCEPT {right})) + "
            f"(SELECT count(*) FROM ({right} EXCEPT {left}))"
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    return int(row[0])


def test_live_replay_matches_a_fresh_build(
    live_settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> None:
    fresh_state = catalog.state(live_settings)
    assert fresh_state is not None
    assert fresh_state.stamp is not None
    target = fresh_state.stamp
    scratch = tmp_path_factory.mktemp("changelog")
    with ReleaseClient() as rc:
        entry = rc.manifest(target).file("changelog.json.gz")
        prior = Changelog.read(rc.download(entry, scratch / entry.name)).prior_stamp
    assert prior is not None
    settings = Settings.from_env(
        {"REEP_CACHE_DIR": str(tmp_path_factory.mktemp("replay-cache"))}
    )
    catalog.sync(stamp=prior, settings=settings)
    result = catalog.sync(stamp=target, settings=settings)
    assert result.method == "replay", result.notes
    for sql in (
        "SELECT provider, namespace, external_id, reep_id FROM {db}.bridges",
        "SELECT * FROM {db}.entities",
        "SELECT from_id, coalesce(to_id, ''), reason FROM {db}.redirects",
    ):
        assert _difference(settings.catalog_path, live_settings.catalog_path, sql) == 0
