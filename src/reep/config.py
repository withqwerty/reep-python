"""Settings from environment variables, and the cache and config directories."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import platformdirs

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_RELEASES_URL",
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "ENV_CACHE_DIR",
    "ENV_CONFIG_DIR",
    "ENV_DUCKDB_PATH",
    "ENV_RELEASES_URL",
    "Settings",
]

#: The public release bucket.
DEFAULT_RELEASES_URL = "https://data.reep.football"
#: The hosted API.
DEFAULT_API_URL = "https://reep.football/api/v1"

ENV_API_KEY = "REEP_API_KEY"
ENV_DUCKDB_PATH = "REEP_DUCKDB_PATH"
ENV_CACHE_DIR = "REEP_CACHE_DIR"
ENV_CONFIG_DIR = "REEP_CONFIG_DIR"
ENV_BASE_URL = "REEP_BASE_URL"
ENV_RELEASES_URL = "REEP_RELEASES_URL"

_APP = "reep"


@dataclass(frozen=True, slots=True)
class Settings:
    """Settings for the SDK, normally read from the environment.

    Attributes:
        api_key: The API key (``REEP_API_KEY``), or ``None``.
        duckdb_path: A DuckDB file to open read-only (``REEP_DUCKDB_PATH``), or
            ``None``.
        cache_dir: The SDK's cache directory (``REEP_CACHE_DIR``; default: the
            platform cache directory).
        config_dir: The SDK's config directory (``REEP_CONFIG_DIR``; default: the
            platform config directory).
        api_url: The API base URL (``REEP_BASE_URL``).
        releases_url: The release bucket URL (``REEP_RELEASES_URL``).
    """

    api_key: str | None
    duckdb_path: Path | None
    cache_dir: Path
    config_dir: Path
    api_url: str = DEFAULT_API_URL
    releases_url: str = DEFAULT_RELEASES_URL

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read the settings from environment variables.

        Args:
            env: The environment to read; ``os.environ`` when ``None``.

        Returns:
            The settings.
        """
        source = os.environ if env is None else env

        def value(name: str) -> str | None:
            raw = source.get(name, "").strip()
            return raw or None

        duckdb_path = value(ENV_DUCKDB_PATH)
        cache_dir = value(ENV_CACHE_DIR)
        config_dir = value(ENV_CONFIG_DIR)
        return cls(
            api_key=value(ENV_API_KEY),
            duckdb_path=Path(duckdb_path).expanduser() if duckdb_path else None,
            cache_dir=(
                Path(cache_dir).expanduser()
                if cache_dir
                else platformdirs.user_cache_path(_APP, appauthor=False)
            ),
            config_dir=(
                Path(config_dir).expanduser()
                if config_dir
                else platformdirs.user_config_path(_APP, appauthor=False)
            ),
            api_url=(value(ENV_BASE_URL) or DEFAULT_API_URL).rstrip("/"),
            releases_url=(value(ENV_RELEASES_URL) or DEFAULT_RELEASES_URL).rstrip("/"),
        )

    def with_cache_dir(self, cache_dir: Path) -> Settings:
        """Return a copy that uses another cache directory.

        Args:
            cache_dir: The cache directory.

        Returns:
            The new settings.
        """
        return replace(self, cache_dir=cache_dir)

    @property
    def catalog_path(self) -> Path:
        """Return the path of the SDK's own catalogue file."""
        return self.cache_dir / "catalog" / "reep.duckdb"

    @property
    def downloads_dir(self) -> Path:
        """Return the directory for release downloads."""
        return self.cache_dir / "downloads"
