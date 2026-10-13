from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from reep.catalog import sync
from reep.config import Settings
from reep.register import Register
from reep.release import ReleaseClient
from tests.support.fixture_release import STAMP_1, STAMP_2, FixtureBucket, build_bucket
from tests.support.httpserver import FileServer

ENV_VARS = (
    "REEP_API_KEY",
    "REEP_DUCKDB_PATH",
    "REEP_CACHE_DIR",
    "REEP_CONFIG_DIR",
    "REEP_BASE_URL",
    "REEP_RELEASES_URL",
    "REEP_LOG_DIR",
    "REEP_DATABASE_URL",
    "DATABASE_URL",
    "REEP_CONFIG",
)


def no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def bucket(tmp_path_factory: pytest.TempPathFactory) -> FixtureBucket:
    return build_bucket(tmp_path_factory.mktemp("bucket"))


@pytest.fixture(scope="session")
def _session_server(bucket: FixtureBucket) -> Iterator[FileServer]:
    server = FileServer(bucket.root).start()
    yield server
    server.stop()


@pytest.fixture
def server(_session_server: FileServer, bucket: FixtureBucket) -> Iterator[FileServer]:
    _session_server.reset()
    yield _session_server
    _session_server.reset()
    bucket.point_latest(STAMP_2)


@pytest.fixture
def client(server: FileServer) -> Iterator[ReleaseClient]:
    with ReleaseClient(server.url, sleep=no_sleep, timeout=10) as rc:
        yield rc


def make_settings(
    cache_dir: Path, releases_url: str = "http://127.0.0.1:9"
) -> Settings:
    return Settings(
        api_key=None,
        duckdb_path=None,
        cache_dir=cache_dir,
        config_dir=cache_dir / "config",
        releases_url=releases_url,
    )


@pytest.fixture
def settings(tmp_path: Path, server: FileServer) -> Settings:
    return make_settings(tmp_path / "cache", server.url)


@pytest.fixture(scope="module")
def store_s1(
    tmp_path_factory: pytest.TempPathFactory,
    _session_server: FileServer,
) -> Settings:
    """A slim catalogue with aliases at STAMP_1, shared by a test module."""
    settings = make_settings(tmp_path_factory.mktemp("store"), _session_server.url)
    with ReleaseClient(_session_server.url, sleep=no_sleep) as rc:
        sync(stamp=STAMP_1, with_aliases=True, settings=settings, client=rc)
    return settings


@pytest.fixture
def reg(store_s1: Settings) -> Iterator[Register]:
    with Register.local(settings=store_s1) as register:
        yield register
