"""Keep the SDK's local catalogue current.

The catalogue is a DuckDB file in the SDK cache directory. :func:`sync` downloads a
release from the public bucket and builds the catalogue from it. :func:`import_release`
builds the catalogue from release files that are already on disk.

Profiles:

* ``"slim"``: the ``bridges``, ``entities`` and ``redirects`` tables. These answer
  every lookup except alias search.
* ``"full"``: every CSV table in the release.

``with_aliases=True`` adds the ``aliases`` table to the slim profile, which improves
name search.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from reep import _store, _v4, changelog
from reep._version import __version__
from reep.config import Settings
from reep.errors import (
    ChecksumMismatchError,
    ConfigError,
    NotFoundError,
    ReplayError,
    UnsupportedReleaseError,
)
from reep.release import (
    ReleaseClient,
    ReleaseManifest,
    sha256_file,
    validate_stamp,
    with_source_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    import duckdb

    from reep.release import ProgressCallback

__all__ = [
    "MAX_REPLAY_STEPS",
    "PROFILES",
    "CatalogState",
    "SyncResult",
    "import_release",
    "local_fingerprint",
    "profile_tables",
    "state",
    "sync",
]

logger = logging.getLogger(__name__)

#: The catalogue profiles.
PROFILES: tuple[str, ...] = ("slim", "full")

#: The most releases that one sync replays before it downloads a snapshot.
MAX_REPLAY_STEPS = 30

_TABLE_ROLES = frozenset({"canonical_csv", "overlay"})
_METHODS = frozenset({"auto", "snapshot", "replay"})
_SDK_SOURCES = frozenset({"download", "import", "replay"})
_DERIVED_TABLES = frozenset({"entity_search", "release_metadata"})


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What a catalogue sync or import did.

    Attributes:
        path: The catalogue file.
        profile: The catalogue profile.
        method: ``"snapshot"``, ``"replay"``, ``"up_to_date"`` or ``"import"``.
        stamp_before: The release stamp before the sync, if a catalogue existed.
        stamp_after: The release stamp after the sync.
        downloaded_bytes: The number of bytes downloaded.
        seconds: How long the sync took.
        notes: Information about the sync, for example why it did not replay the
            changelog.
    """

    path: Path
    profile: str
    method: str
    stamp_before: str | None
    stamp_after: str | None
    downloaded_bytes: int
    seconds: float
    notes: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """Return ``True`` when the sync changed the catalogue."""
        return self.method != "up_to_date"


@dataclass(frozen=True, slots=True)
class CatalogState:
    """The state of the local catalogue file.

    Attributes:
        path: The catalogue file.
        stamp: The release stamp.
        profile: The profile.
        source: How the catalogue was built.
        tables: The tables in the catalogue.
        built_at: When the catalogue was built.
        bridge_attributes_stamp: The release that the bridge ``rung`` and
            ``upstream_status`` values come from.
        import_fingerprint: For an imported catalogue, a digest of the source
            files' names, sizes and modification times.
    """

    path: Path
    stamp: str | None
    profile: str | None
    source: str | None
    tables: tuple[str, ...]
    built_at: str | None
    bridge_attributes_stamp: str | None
    import_fingerprint: str | None = None
    schema_version: str | None = None
    manifest_sha256: str | None = None


def state(settings: Settings | None = None) -> CatalogState | None:
    """Return the state of the local catalogue, or ``None`` if there is none.

    Args:
        settings: The settings; read from the environment when ``None``.

    Returns:
        The state, or ``None``.
    """
    settings = settings or Settings.from_env()
    path = settings.catalog_path
    if not path.is_file():
        return None
    con = _store.connect(path, read_only=True)
    try:
        meta = _store.read_metadata(con)
        tables = tuple(sorted(_store.table_names(con)))
    finally:
        con.close()
    return CatalogState(
        path=path,
        stamp=meta.get(_store.META_STAMP),
        profile=meta.get(_store.META_PROFILE),
        source=meta.get(_store.META_SOURCE),
        tables=tables,
        built_at=meta.get(_store.META_BUILT_AT),
        bridge_attributes_stamp=meta.get(_store.META_BRIDGE_ATTRS),
        import_fingerprint=meta.get(_store.META_IMPORT_FINGERPRINT),
        schema_version=meta.get(_store.META_SCHEMA),
        manifest_sha256=meta.get(_store.META_MANIFEST_HASH),
    )


def local_fingerprint(source: Path) -> str:
    """Return a digest of the release files in a directory.

    The digest covers each file's name, size and modification time. It changes
    when a file is added, removed or replaced.

    Args:
        source: The directory.

    Returns:
        The hex digest.
    """
    digest = hashlib.sha256()
    for directory in (source, source / "csv"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.csv.gz")) + sorted(
            directory.glob("release.json")
        ):
            info = path.stat()
            digest.update(f"{path.name}:{info.st_size}:{info.st_mtime_ns};".encode())
    return digest.hexdigest()


def profile_tables(
    manifest: ReleaseManifest, profile: str, *, with_aliases: bool = False
) -> list[str]:
    """Return the tables of a profile that the release has.

    Args:
        manifest: The release manifest.
        profile: ``"slim"`` or ``"full"``.
        with_aliases: Add the ``aliases`` table to the slim profile.

    Returns:
        The table names.
    """
    _check_profile(profile)
    if profile == "full":
        return [
            name
            for name in manifest.tables
            if (entry := manifest.table_file(name)) is not None
            and (entry.role in _TABLE_ROLES or entry.role is None)
        ]
    tables = list(_store.SLIM_TABLES)
    if with_aliases and manifest.schema_version == _v4.SCHEMA:
        tables.extend(["provider_aliases", "overlay_aliases"])
    if with_aliases and manifest.table_file("aliases") is not None:
        tables.append("aliases")
    return tables


def sync(
    profile: str | None = None,
    *,
    stamp: str | None = None,
    with_aliases: bool | None = None,
    method: str = "auto",
    force: bool = False,
    keep_downloads: bool = False,
    max_replay_steps: int = MAX_REPLAY_STEPS,
    settings: Settings | None = None,
    client: ReleaseClient | None = None,
    progress: Callable[[str, int, int | None], None] | None = None,
) -> SyncResult:
    """Bring the local catalogue to a release.

    When a catalogue exists, the sync first tries to replay the changelog chain from
    the catalogue's release to the target release. The replay downloads the
    changelogs and the small tables again, and keeps the 158 MB bridges file off
    the network. When the replay is not possible, the sync downloads a snapshot;
    ``SyncResult.notes`` says why.

    Args:
        profile: ``"slim"`` or ``"full"``. ``None`` keeps the current catalogue's
            profile, or uses ``"slim"`` when there is no catalogue.
        stamp: The release to sync to; ``None`` means the current release.
        with_aliases: Add aliases to a slim catalogue. ``None`` keeps the current
            choice.
        method: ``"auto"`` (replay when possible, else snapshot), ``"snapshot"``
            or ``"replay"`` (replay or raise ``ReplayError``).
        force: Rebuild the catalogue from a snapshot even when it is current.
        keep_downloads: Keep the downloaded release files after the build.
        max_replay_steps: The most releases that one replay walks back.
        settings: The settings; read from the environment when ``None``.
        client: The release client to use.
        progress: Called with ``(file_name, bytes_done, bytes_total)``.

    Returns:
        What the sync did.

    Raises:
        ConfigError: If ``profile`` or ``method`` is not valid.
        NetworkError: If a download fails.
        ChecksumMismatchError: If a file does not match the manifest.
        ReplayError: If ``method`` is ``"replay"`` and the replay is not possible.
        UnsupportedReleaseError: If the release format is not supported.
    """
    started = time.monotonic()
    if method not in _METHODS:
        raise ConfigError(
            f"Unknown sync method {method!r}.",
            hint="Use 'auto', 'snapshot' or 'replay'.",
        )
    settings = settings or Settings.from_env()
    before = state(settings)
    chosen_profile = profile or (before.profile if before else None) or "slim"
    _check_profile(chosen_profile)
    aliases = (
        with_aliases
        if with_aliases is not None
        else bool(before and "aliases" in before.tables)
    )
    notes: list[str] = []
    downloaded = 0
    with _client(client, settings) as rc:
        manifest = rc.manifest(stamp)
        wanted = profile_tables(manifest, chosen_profile, with_aliases=aliases)
        _check_existing_identity(before, manifest)
        if method == "replay" and manifest.schema_version == _v4.SCHEMA:
            raise ReplayError("v4 download replay is unavailable; use a snapshot.")
        if (
            not force
            and before is not None
            and before.stamp == manifest.stamp
            and (
                manifest.schema_version != _v4.SCHEMA
                or before.manifest_sha256 == manifest.identity_sha256
            )
            and before.profile == chosen_profile
            and set(wanted) <= set(before.tables)
        ):
            return SyncResult(
                path=settings.catalog_path,
                profile=chosen_profile,
                method="up_to_date",
                stamp_before=before.stamp,
                stamp_after=manifest.stamp,
                downloaded_bytes=0,
                seconds=time.monotonic() - started,
            )
        if method != "snapshot" and not force:
            blocker = _replay_blocker(before, manifest, chosen_profile, wanted)
            if blocker is not None and method == "replay":
                raise ReplayError(f"Cannot replay: {blocker}.")
            if blocker is None and before is not None:
                attempt = _Replay(
                    rc,
                    manifest,
                    before,
                    wanted,
                    settings=settings,
                    profile=chosen_profile,
                    max_steps=max_replay_steps,
                    progress=progress,
                )
                try:
                    steps = attempt.run()
                except ReplayError as exc:
                    downloaded += attempt.downloaded
                    if method == "replay":
                        raise
                    logger.info("Replay not possible (%s); downloading a snapshot", exc)
                    notes.append(f"snapshot instead of replay: {exc.message}")
                else:
                    return SyncResult(
                        path=settings.catalog_path,
                        profile=chosen_profile,
                        method="replay",
                        stamp_before=before.stamp,
                        stamp_after=manifest.stamp,
                        downloaded_bytes=attempt.downloaded,
                        seconds=time.monotonic() - started,
                        notes=(f"replayed {steps} changelog(s)",),
                    )
                finally:
                    if not keep_downloads:
                        attempt.remove_downloads()
            elif blocker is not None and before is not None:
                notes.append(f"snapshot: {blocker}")
        downloaded += _snapshot(
            rc,
            manifest,
            wanted,
            settings=settings,
            profile=chosen_profile,
            keep_downloads=keep_downloads,
            progress=progress,
        )
    return SyncResult(
        path=settings.catalog_path,
        profile=chosen_profile,
        method="snapshot",
        stamp_before=before.stamp if before else None,
        stamp_after=manifest.stamp,
        downloaded_bytes=downloaded,
        seconds=time.monotonic() - started,
        notes=tuple(notes),
    )


def _check_existing_identity(
    before: CatalogState | None, manifest: ReleaseManifest
) -> None:
    """Preserve one immutable descriptor identity per observed release stamp."""
    if (
        before is not None
        and before.stamp == manifest.stamp
        and before.manifest_sha256 is not None
        and before.manifest_sha256 != manifest.identity_sha256
    ):
        raise UnsupportedReleaseError(
            "A release descriptor changed under the same stamp."
        )


def _replay_blocker(
    before: CatalogState | None,
    target: ReleaseManifest,
    profile: str,
    wanted: list[str],
) -> str | None:
    """Return why a replay is not possible, or ``None`` when it is."""
    if target.schema_version == _v4.SCHEMA:
        return "v4 download replay is unavailable"
    if before is not None and before.schema_version == _v4.SCHEMA:
        return "replay cannot cross release schemas"
    if before is None:
        return "there is no local catalogue"
    if before.stamp is None:
        return "the local catalogue has no release stamp"
    if before.source not in _SDK_SOURCES:
        return "the local catalogue was not built by the SDK"
    if before.profile != profile:
        return "the profile changed"
    data_tables = set(before.tables) - _DERIVED_TABLES
    if set(wanted) != data_tables:
        return "the table set changed"
    if target.stamp <= before.stamp:
        return "the target release is not newer than the local catalogue"
    if target.table_file("bridges") is None:
        return "the release has no bridges table"
    return None


def import_release(
    source: Path,
    *,
    profile: str = "slim",
    with_aliases: bool = False,
    stamp: str | None = None,
    settings: Settings | None = None,
    client: ReleaseClient | None = None,
) -> SyncResult:
    """Build the local catalogue from release files that are already on disk.

    The directory can hold the files flat (``bridges.csv.gz``) or in a ``csv/``
    subdirectory, as in the release. The import checks the files against a
    manifest:

    * a ``release.json`` in the directory, when there is one; otherwise
    * the manifest of ``stamp`` from the release bucket, when ``stamp`` is given.

    Without a manifest, the files are not checked and the catalogue has no stamp,
    so a later :func:`sync` downloads a full snapshot.

    Args:
        source: The directory with the release files.
        profile: ``"slim"`` or ``"full"``.
        with_aliases: Add the aliases table to a slim catalogue.
        stamp: The release the files come from, used to fetch its manifest.
        settings: The settings; read from the environment when ``None``.
        client: The release client to use when ``stamp`` is given.

    Returns:
        What the import did.

    Raises:
        ConfigError: If the directory lacks a required table.
        ChecksumMismatchError: If a file does not match the manifest.
        UnsupportedReleaseError: If the release format is not supported.
    """
    started = time.monotonic()
    _check_profile(profile)
    settings = settings or Settings.from_env()
    if not source.is_dir():
        raise ConfigError(f"{source} is not a directory.")
    before = state(settings)
    manifest: ReleaseManifest | None = None
    local_manifest = source / "release.json"
    if local_manifest.is_file():
        if local_manifest.stat().st_size > _v4.METADATA_LIMIT:
            raise UnsupportedReleaseError("Release descriptor exceeds metadata bound.")
        body = local_manifest.read_bytes()
        manifest = ReleaseManifest.from_json(_v4.loads(body, "release.json"))
        manifest = replace(manifest, identity_sha256=hashlib.sha256(body).hexdigest())
        if manifest.schema_version == _v4.SCHEMA:
            if len(body) > _v4.METADATA_LIMIT:
                raise UnsupportedReleaseError(
                    "Release descriptor exceeds metadata bound."
                )
            raw_manifest = source / "manifest.json"
            if (
                not raw_manifest.is_file()
                or raw_manifest.stat().st_size > _v4.METADATA_LIMIT
            ):
                raise UnsupportedReleaseError(
                    "v4 import requires the bound manifest.json."
                )
            manifest = with_source_manifest(manifest, raw_manifest.read_bytes())
        if stamp is not None and manifest.stamp != stamp:
            raise ConfigError(
                f"{local_manifest} is for release {manifest.stamp}, not {stamp}."
            )
    elif stamp is not None:
        validate_stamp(stamp)
        with _client(client, settings) as rc:
            manifest = rc.manifest(stamp)
    if manifest is not None:
        _check_existing_identity(before, manifest)
    files = _local_tables(source)
    if manifest is not None:
        wanted = [
            t
            for t in profile_tables(manifest, profile, with_aliases=with_aliases)
            if manifest.schema_version == _v4.SCHEMA
            or t in files
            or t in _store.SLIM_TABLES
        ]
    else:
        wanted = list(_store.SLIM_TABLES)
        if profile == "full":
            wanted = sorted(files)
        elif with_aliases and "aliases" in files:
            wanted.append("aliases")
    missing = [t for t in wanted if t not in files]
    if missing:
        raise ConfigError(
            f"{source} lacks {', '.join(f'{t}.csv.gz' for t in missing)}.",
            hint="Download the tables from https://reep.football/downloads.",
        )
    tables = {t: files[t] for t in wanted}
    expected_rows: dict[str, int] = {}
    if manifest is not None:
        for table, path in tables.items():
            entry = manifest.table_file(table)
            if entry is None:
                raise UnsupportedReleaseError(
                    f"Release {manifest.stamp} does not list {table}.csv.gz."
                )
            actual = sha256_file(path)
            if actual != entry.sha256 or path.stat().st_size != entry.size:
                raise ChecksumMismatchError(
                    f"{path} does not match release {manifest.stamp} "
                    f"(SHA-256 {actual}, expected {entry.sha256}).",
                    hint="Check that every file comes from the same release.",
                )
            if entry.rows is not None:
                expected_rows[table] = entry.rows
    else:
        logger.warning(
            "No release manifest for %s; the files are not checked and the "
            "catalogue has no release stamp.",
            source,
        )
    _store.build_store(
        _store.StoreSpec(
            tables=tables,
            stamp=manifest.stamp if manifest else None,
            schema_version=manifest.schema_version if manifest else None,
            profile=profile,
            source="import",
            generated_at=manifest.generated_at if manifest else None,
            tier=manifest.tier if manifest else None,
            expected_rows=expected_rows,
            extra_meta={
                _store.META_IMPORT_FINGERPRINT: local_fingerprint(source),
                **_manifest_metadata(manifest),
            },
        ),
        settings.catalog_path,
    )
    notes = () if manifest else ("files not checked: no release manifest",)
    return SyncResult(
        path=settings.catalog_path,
        profile=profile,
        method="import",
        stamp_before=before.stamp if before else None,
        stamp_after=manifest.stamp if manifest else None,
        downloaded_bytes=0,
        seconds=time.monotonic() - started,
        notes=notes,
    )


def _manifest_metadata(manifest: ReleaseManifest | None) -> dict[str, str]:
    if manifest is None:
        return {}
    result = (
        {_store.META_MANIFEST_HASH: manifest.identity_sha256}
        if manifest.identity_sha256
        else {}
    )
    if manifest.schema_version == _v4.SCHEMA:
        source = manifest.raw["source"]
        result.update(
            {
                "publication_channel": "downloads",
                "source_generation_id": source["generation_id"],
                "source_generation_manifest_sha256": source["manifest_sha256"],
                "candidate_sha256": manifest.raw["candidate_sha256"],
                "policy_sha256": manifest.raw["policy_sha256"],
            }
        )
    return result


def _snapshot(
    rc: ReleaseClient,
    manifest: ReleaseManifest,
    tables: list[str],
    *,
    settings: Settings,
    profile: str,
    keep_downloads: bool,
    progress: Callable[[str, int, int | None], None] | None,
) -> int:
    download_dir = settings.downloads_dir / manifest.stamp
    paths: dict[str, Path] = {}
    expected_rows: dict[str, int] = {}
    downloaded = 0
    for table in tables:
        entry = manifest.table_file(table)
        if entry is None:
            raise UnsupportedReleaseError(
                f"Release {manifest.stamp} has no {table} table."
            )
        paths[table] = rc.download(
            entry,
            download_dir / entry.name,
            progress=_named(progress, entry.name),
        )
        downloaded += entry.size
        if entry.rows is not None:
            expected_rows[table] = entry.rows
    _store.build_store(
        _store.StoreSpec(
            tables=paths,
            stamp=manifest.stamp,
            schema_version=manifest.schema_version,
            profile=profile,
            source="download",
            generated_at=manifest.generated_at,
            tier=manifest.tier,
            expected_rows=expected_rows,
            extra_meta=_manifest_metadata(manifest),
        ),
        settings.catalog_path,
    )
    if not keep_downloads:
        shutil.rmtree(download_dir, ignore_errors=True)
    return downloaded


class _Replay:
    """One replay of the changelog chain onto the local catalogue."""

    def __init__(
        self,
        rc: ReleaseClient,
        target: ReleaseManifest,
        before: CatalogState,
        wanted: list[str],
        *,
        settings: Settings,
        profile: str,
        max_steps: int,
        progress: Callable[[str, int, int | None], None] | None,
    ) -> None:
        self.rc = rc
        self.target = target
        self.before = before
        self.wanted = wanted
        self.settings = settings
        self.profile = profile
        self.max_steps = max_steps
        self.progress = progress
        self.downloaded = 0
        self._download_dirs: set[Path] = set()

    def run(self) -> int:
        """Replay the chain and swap the new catalogue in; return the step count."""
        before_stamp = self.before.stamp
        if before_stamp is None:  # pragma: no cover - _replay_blocker checks this
            raise ReplayError("The local catalogue has no release stamp.")
        dest = self.settings.catalog_path
        tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
        try:
            con = _store.connect(tmp, read_only=False)
            try:
                steps = self._build(con, dest, before_stamp)
                con.execute("CHECKPOINT")
            finally:
                con.close()
            _store.replace_file(tmp, dest)
        finally:
            _store.remove_db_files(tmp)
        return steps

    def remove_downloads(self) -> None:
        """Remove the files that the replay downloaded."""
        for directory in self._download_dirs:
            shutil.rmtree(directory, ignore_errors=True)

    def _build(
        self, con: duckdb.DuckDBPyConnection, dest: Path, before_stamp: str
    ) -> int:
        con.execute(f"SET temp_directory = '{_sql_text(str(dest.parent))}'")
        con.execute(f"ATTACH '{_sql_text(str(dest))}' AS old (READ_ONLY)")
        con.execute("CREATE TABLE reep_bridges_work AS SELECT * FROM old.bridges")
        prior_redirects = {
            str(src): str(dst)
            for src, dst in con.execute(
                "SELECT from_id, to_id FROM old.redirects "
                "WHERE reason = 'merged' AND coalesce(to_id, '') <> ''"
            ).fetchall()
        }
        con.execute("DETACH old")
        redirect_steps = self._stage_chain(con, before_stamp)
        for index in reversed(range(len(redirect_steps))):
            changelog.apply_staged_bridge_changes(
                con, "reep_bridges_work", f"reep_step_{index}"
            )
        replayed = prior_redirects
        for events in reversed(redirect_steps):
            replayed = changelog.replay_redirects(replayed, events)
        self._reload_tables(con)
        con.execute(
            "CREATE TABLE bridges AS SELECT * FROM reep_bridges_work "
            "ORDER BY provider, namespace, external_id"
        )
        con.execute("DROP TABLE reep_bridges_work")
        self._check(con, replayed)
        _store.build_entity_search(con)
        _store.write_metadata(
            con,
            {
                _store.META_STAMP: self.target.stamp,
                _store.META_SCHEMA: self.target.schema_version,
                _store.META_TIER: self.target.tier,
                _store.META_GENERATED_AT: self.target.generated_at,
                _store.META_SDK_VERSION: __version__,
                _store.META_STORE_VERSION: _store.STORE_VERSION,
                _store.META_PROFILE: self.profile,
                _store.META_SOURCE: "replay",
                _store.META_BUILT_AT: _store.now_utc(),
                _store.META_BRIDGE_ATTRS: self.before.bridge_attributes_stamp,
                _store.META_REPLAYED_FROM: before_stamp,
                _store.META_REPLAY_STEPS: str(len(redirect_steps)),
            },
        )
        return len(redirect_steps)

    def _stage_chain(
        self, con: duckdb.DuckDBPyConnection, before_stamp: str
    ) -> list[list[dict[str, str]]]:
        """Walk back from the target to the local release; stage each changelog.

        Returns:
            The redirect events of each changelog, newest first.
        """
        bridges = self.target.table_file("bridges")
        budget = bridges.size if bridges is not None else 0
        manifest = self.target
        spent = 0
        redirect_steps: list[list[dict[str, str]]] = []
        while True:
            if len(redirect_steps) >= self.max_steps:
                raise ReplayError(
                    f"The local catalogue is more than {self.max_steps} releases "
                    "behind."
                )
            entry = manifest.files.get("changelog.json.gz")
            if entry is None:
                raise ReplayError(f"Release {manifest.stamp} has no changelog.")
            spent += entry.size
            if spent >= budget:
                raise ReplayError(
                    "The changelog chain is larger than the bridges snapshot."
                )
            directory = self.settings.downloads_dir / manifest.stamp
            self._download_dirs.add(directory)
            path = self.rc.download(
                entry,
                directory / entry.name,
                progress=_named(self.progress, f"{manifest.stamp}/{entry.name}"),
            )
            self.downloaded += entry.size
            log = changelog.Changelog.read(path)
            if log.stamp != manifest.stamp:
                raise ReplayError(
                    f"The changelog of {manifest.stamp} names release {log.stamp}."
                )
            if log.prior_stamp is None:
                raise ReplayError(f"The changelog of {manifest.stamp} has no prior.")
            changelog.stage_bridge_changes(con, log, f"reep_step_{len(redirect_steps)}")
            redirect_steps.append(log.redirect_events)
            if log.prior_stamp == before_stamp:
                return redirect_steps
            if log.prior_stamp < before_stamp:
                raise ReplayError(f"The changelog chain skips release {before_stamp}.")
            try:
                manifest = self.rc.manifest(log.prior_stamp)
            except NotFoundError as exc:
                raise ReplayError(
                    f"Release {log.prior_stamp} is no longer on the release bucket."
                ) from exc

    def _reload_tables(self, con: duckdb.DuckDBPyConnection) -> None:
        directory = self.settings.downloads_dir / self.target.stamp
        self._download_dirs.add(directory)
        for table in self.wanted:
            if table == "bridges":
                continue
            entry = self.target.table_file(table)
            if entry is None:
                raise ReplayError(f"Release {self.target.stamp} has no {table} table.")
            path = self.rc.download(
                entry,
                directory / entry.name,
                progress=_named(self.progress, entry.name),
            )
            self.downloaded += entry.size
            _store.load_table(con, table, path)

    def _check(
        self, con: duckdb.DuckDBPyConnection, replayed_redirects: dict[str, str]
    ) -> None:
        expected_rows = {
            table: entry.rows
            for table in self.wanted
            if (entry := self.target.table_file(table)) is not None
            and entry.rows is not None
        }
        try:
            _store.check_store(con, expected_rows)
        except UnsupportedReleaseError as exc:
            raise ReplayError(
                f"The replayed catalogue does not match release "
                f"{self.target.stamp}: {exc.message}"
            ) from exc
        actual = {
            str(src): str(dst)
            for src, dst in con.execute(
                "SELECT from_id, to_id FROM redirects "
                "WHERE reason = 'merged' AND coalesce(to_id, '') <> ''"
            ).fetchall()
        }
        if actual != replayed_redirects:
            raise ReplayError(
                "The replayed merge redirects do not match the release's redirects."
            )
        orphans = _store.scalar_int(
            con,
            "SELECT count(*) FROM bridges b ANTI JOIN entities e USING (reep_id)",
        )
        if orphans:
            raise ReplayError(
                f"{orphans} replayed bridge(s) name entities that the release lacks."
            )


def _local_tables(source: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for directory in (source, source / "csv"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.csv.gz")):
            name = path.name.removesuffix(".csv.gz")
            if _store.known_tables([name]):
                found.setdefault(name, path)
    return found


def _named(
    progress: Callable[[str, int, int | None], None] | None, name: str
) -> ProgressCallback | None:
    if progress is None:
        return None

    def callback(done: int, total: int | None) -> None:
        progress(name, done, total)

    return callback


def _check_profile(profile: str) -> None:
    if profile not in PROFILES:
        raise ConfigError(
            f"Unknown catalogue profile {profile!r}.",
            hint="Use 'slim' or 'full'.",
        )


@contextmanager
def _client(
    client: ReleaseClient | None, settings: Settings
) -> Iterator[ReleaseClient]:
    """Use the caller's client, or create one and close it afterwards."""
    if client is not None:
        yield client
        return
    with ReleaseClient(settings.releases_url) as owned:
        yield owned


def _sql_text(value: str) -> str:
    return value.replace("'", "''")
