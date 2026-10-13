"""The data of ``reep status``, shared by the command line and the MCP server."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from reep import auth, catalog
from reep.release import ReleaseClient

if TYPE_CHECKING:
    from reep.config import Settings

__all__ = ["status_data"]


def status_data(
    settings: Settings, *, check_latest: bool = False
) -> tuple[dict[str, Any], str | None]:
    """Return the local catalogue, the API key source and the backend in use.

    The API key itself is not included.

    Args:
        settings: The settings.
        check_latest: Also read the current release stamp from the release bucket.

    Returns:
        The status data and the release stamp of the local catalogue.
    """
    state = catalog.state(settings)
    catalog_data: dict[str, Any] | None = None
    if state is not None:
        age = None
        if state.built_at:
            built = datetime.fromisoformat(state.built_at)
            age = round((datetime.now(UTC) - built).total_seconds() / 86400, 1)
        catalog_data = {
            "path": str(state.path),
            "stamp": state.stamp,
            "profile": state.profile,
            "source": state.source,
            "built_at": state.built_at,
            "age_days": age,
            "bridge_attributes_stamp": state.bridge_attributes_stamp,
            "tables": list(state.tables),
        }
    duck: dict[str, Any] | None = None
    if settings.duckdb_path is not None:
        duck = {
            "path": str(settings.duckdb_path),
            "exists": settings.duckdb_path.is_file(),
        }
    key_source = auth.key_source(settings)
    if settings.duckdb_path is not None or state is not None:
        backend: str | None = "local"
    elif key_source:
        backend = "api"
    else:
        backend = None
    latest: str | None = None
    update: bool | None = None
    if check_latest:
        with ReleaseClient(settings.releases_url) as rc:
            latest = rc.latest().stamp
        update = bool(state and state.stamp != latest)
    data = {
        "backend": backend,
        "catalog": catalog_data,
        "duckdb_path": duck,
        "api": {"key_source": key_source, "base_url": settings.api_url},
        "releases_url": settings.releases_url,
        "latest_stamp": latest,
        "update_available": update,
    }
    return data, state.stamp if state else None
