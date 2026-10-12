"""Tests against the live public release. Run them with ``pytest -m network``.

They download about 210 MB. Point ``--basetemp`` at a disk with space.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reep import Register, catalog
from reep.config import Settings
from reep.models import IdStatus, ResolutionStatus

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
