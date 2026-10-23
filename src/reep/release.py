"""Download and verify files from the public Reep release bucket.

A release is a directory on the bucket: ``releases/<stamp>/``. The pointer
``releases/latest.json`` names the current release and the SHA-256 of its manifest,
``release.json``. The manifest lists every file with its SHA-256 and size.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self, TypeAlias

import httpx

from reep import _v4
from reep._version import USER_AGENT
from reep.config import DEFAULT_RELEASES_URL
from reep.errors import (
    ChecksumMismatchError,
    NetworkError,
    NotFoundError,
    UnsupportedReleaseError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from types import TracebackType

__all__ = [
    "SUPPORTED_MANIFEST_VERSIONS",
    "SUPPORTED_SCHEMA_VERSIONS",
    "LatestPointer",
    "ProgressCallback",
    "ReleaseClient",
    "ReleaseFile",
    "ReleaseManifest",
    "sha256_file",
    "validate_stamp",
    "with_source_manifest",
]

logger = logging.getLogger(__name__)

#: Manifest versions that this version of reep can read.
SUPPORTED_MANIFEST_VERSIONS = frozenset({1, 4})
#: Release schema versions that this version of reep can read.
SUPPORTED_SCHEMA_VERSIONS = frozenset({"bridge-register-v1", _v4.SCHEMA})

_LATEST_SCHEMA = "reep-public-release-latest-v1"
_STAMP = re.compile(r"^\d{8}T\d{6}Z$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_CHUNK = 1 << 20
_UPGRADE_HINT = "Upgrade the reep package: pip install --upgrade reep"

#: A progress callback: ``(bytes_done, bytes_total)``; the total can be ``None``.
ProgressCallback: TypeAlias = "Callable[[int, int | None], None]"


def validate_stamp(stamp: str) -> str:
    """Return ``stamp`` if it is a release stamp such as ``20261005T180536Z``.

    Args:
        stamp: The stamp to check.

    Returns:
        The stamp.

    Raises:
        ValueError: If the stamp has the wrong form.
    """
    try:
        if _STAMP.fullmatch(stamp):
            datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
        else:
            _v4.stamp_order(stamp)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"{stamp!r} is not a release stamp "
            "(expected 20261005T180536Z or 2026-10-23.1)."
        ) from exc
    return stamp


def _valid_legacy_stamp(value: object) -> bool:
    if not isinstance(value, str) or not _STAMP.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return False
    return True


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file.

    Args:
        path: The file.

    Returns:
        The hex digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class LatestPointer:
    """The contents of ``releases/latest.json``.

    Attributes:
        stamp: The current release stamp.
        manifest_key: The object key of the release manifest.
        manifest_sha256: The SHA-256 of the release manifest.
        generated_at: When the pointer was written.
    """

    stamp: str
    manifest_key: str
    manifest_sha256: str
    generated_at: str | None = None
    manifest_size: int | None = None
    selection: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ReleaseFile:
    """One file in a release manifest.

    Attributes:
        name: The path inside the release, for example ``"csv/bridges.csv.gz"``.
        object_key: The object key on the bucket.
        sha256: The SHA-256 hex digest.
        size: The size in bytes.
        rows: The number of data rows, for CSV tables.
        role: The manifest role, for example ``"canonical_csv"``.
    """

    name: str
    object_key: str
    sha256: str
    size: int
    rows: int | None = None
    role: str | None = None


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """A parsed ``release.json``.

    Attributes:
        stamp: The release stamp.
        schema_version: The release schema version.
        manifest_version: The manifest format version.
        generated_at: When the release was generated.
        tier: The publication tier, for example ``"public_bridge_v1"``.
        files: The files, by their path inside the release.
        raw: The manifest as parsed JSON.
    """

    stamp: str
    schema_version: str
    manifest_version: int
    generated_at: str | None
    tier: str | None
    files: Mapping[str, ReleaseFile]
    raw: Mapping[str, Any] = field(repr=False, compare=False)
    identity_sha256: str | None = None

    @classmethod
    def from_json(cls, data: object) -> ReleaseManifest:
        """Parse and check a manifest.

        Args:
            data: The parsed JSON document.

        Returns:
            The manifest.

        Raises:
            UnsupportedReleaseError: If the manifest has an unknown version or an
                unexpected shape.
        """
        if not isinstance(data, dict):
            raise UnsupportedReleaseError("release.json is not a JSON object.")
        if data.get("schema") == _v4.SCHEMA:
            parsed = _v4.descriptor(data)
            return cls(
                stamp=parsed["stamp"],
                schema_version=_v4.SCHEMA,
                manifest_version=4,
                generated_at=parsed["generated_at"],
                tier=None,
                files={
                    name: ReleaseFile(name, ref["key"], ref["sha256"], ref["bytes"])
                    for name, ref in parsed["files"].items()
                },
                raw=parsed,
            )
        if "schema" in data:
            raise UnsupportedReleaseError(
                "Unsupported versioned release descriptor schema."
            )
        manifest_version = data.get("manifest_version")
        if type(manifest_version) is not int or manifest_version != 1:
            raise UnsupportedReleaseError(
                f"release.json has manifest_version {manifest_version!r}, which this "
                "version of reep does not support.",
                hint=_UPGRADE_HINT,
            )
        schema_version = data.get("schema_version")
        if schema_version != "bridge-register-v1":
            raise UnsupportedReleaseError(
                f"The release schema {schema_version!r} is not supported by this "
                "version of reep.",
                hint=_UPGRADE_HINT,
            )
        stamp = data.get("stamp")
        if not _valid_legacy_stamp(stamp):
            raise UnsupportedReleaseError(
                f"release.json has no valid stamp: {stamp!r}."
            )
        raw_files = data.get("files")
        if not isinstance(raw_files, dict):
            raise UnsupportedReleaseError("release.json has no files object.")
        files: dict[str, ReleaseFile] = {}
        for name, entry in raw_files.items():
            files[name] = _parse_file_entry(name, entry, str(stamp))
        generated_at = data.get("generated_at")
        tier = data.get("tier") or data.get("projection_mode")
        return cls(
            stamp=str(stamp),
            schema_version=schema_version,
            manifest_version=manifest_version,
            generated_at=generated_at if isinstance(generated_at, str) else None,
            tier=tier if isinstance(tier, str) else None,
            files=files,
            raw=data,
        )

    def file(self, name: str) -> ReleaseFile:
        """Return the manifest entry for a file.

        Args:
            name: The path inside the release, for example ``"changelog.json.gz"``.

        Returns:
            The entry.

        Raises:
            UnsupportedReleaseError: If the manifest does not list the file.
        """
        try:
            return self.files[name]
        except KeyError:
            raise UnsupportedReleaseError(
                f"Release {self.stamp} does not list {name}."
            ) from None

    def table_file(self, table: str) -> ReleaseFile | None:
        """Return the CSV file of a table, or ``None`` if the release has none.

        Args:
            table: The table name, for example ``"bridges"``.

        Returns:
            The entry, or ``None``.
        """
        prefix = "" if self.schema_version == _v4.SCHEMA else "csv/"
        return self.files.get(f"{prefix}{table}.csv.gz")

    @property
    def tables(self) -> tuple[str, ...]:
        """Return the names of the CSV tables in the release."""
        if self.schema_version == _v4.SCHEMA:
            return tuple(sorted(_v4.TABLES))
        names = [
            name.removeprefix("csv/").removesuffix(".csv.gz")
            for name in self.files
            if name.startswith("csv/") and name.endswith(".csv.gz")
        ]
        return tuple(sorted(names))


def _parse_file_entry(name: str, entry: object, stamp: str) -> ReleaseFile:
    if not isinstance(entry, dict):
        raise UnsupportedReleaseError(f"release.json entry {name!r} is not an object.")
    sha = entry.get("sha256")
    size = entry.get("bytes")
    if not isinstance(sha, str) or not _SHA256.match(sha):
        raise UnsupportedReleaseError(f"release.json entry {name!r} has no SHA-256.")
    if type(size) is not int or size < 0:
        raise UnsupportedReleaseError(f"release.json entry {name!r} has no size.")
    object_key = entry.get("object_key")
    if not isinstance(object_key, str) or not object_key:
        object_key = f"releases/{stamp}/{name}"
    rows = entry.get("rows")
    role = entry.get("role")
    return ReleaseFile(
        name=name,
        object_key=object_key,
        sha256=sha,
        size=size,
        rows=rows if isinstance(rows, int) else None,
        role=role if isinstance(role, str) else None,
    )


class ReleaseClient:
    """Read release pointers and manifests, and download verified files.

    The client uses one ``httpx.Client``. Close it with :meth:`close`, or use the
    client as a context manager.

    Args:
        base_url: The bucket URL; the default is the public Reep bucket.
        http: An ``httpx.Client`` to use instead of a new one.
        timeout: The read timeout in seconds.
        retries: How many times to retry a failed request.
        backoff: The first retry delay in seconds; each retry doubles it.
        sleep: The function that waits between retries.

    Example:
        >>> with ReleaseClient() as client:  # doctest: +SKIP
        ...     manifest = client.manifest()
        ...     manifest.stamp
        '20261005T180536Z'
    """

    def __init__(
        self,
        base_url: str = DEFAULT_RELEASES_URL,
        *,
        http: httpx.Client | None = None,
        timeout: float = 60.0,
        retries: int = 3,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the client."""
        self.base_url = base_url.rstrip("/")
        self._owns_http = http is None
        self._http = http or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=15.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep

    def __enter__(self) -> Self:
        """Return the client."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the client."""
        self.close()

    def close(self) -> None:
        """Close the HTTP client if this object created it."""
        if self._owns_http:
            self._http.close()

    def url(self, object_key: str) -> str:
        """Return the URL of an object on the bucket.

        Args:
            object_key: The object key, for example ``"releases/latest.json"``.

        Returns:
            The URL.
        """
        return f"{self.base_url}/{object_key.lstrip('/')}"

    def latest(self) -> LatestPointer:
        """Read ``releases/latest.json``.

        Returns:
            The pointer to the current release.

        Raises:
            NetworkError: If the request fails.
            UnsupportedReleaseError: If the pointer has an unexpected form.
        """
        data = _loads(
            self._get_bytes("releases/latest.json", max_bytes=_v4.METADATA_LIMIT),
            "latest.json",
        )
        if not isinstance(data, dict):
            raise UnsupportedReleaseError("latest.json is not a JSON object.")
        if data.get("schema") == _v4.SELECTOR:
            selected = _v4.selection(data)
            ref = selected["release"]
            return LatestPointer(
                selected["stamp"],
                ref["key"],
                ref["sha256"],
                selected["generated_at"],
                ref["bytes"],
                selected,
            )
        if "schema" in data:
            raise UnsupportedReleaseError(
                "Unsupported versioned release selector schema."
            )
        if data.get("schema_version") != _LATEST_SCHEMA:
            raise UnsupportedReleaseError(
                f"latest.json has schema {data.get('schema_version')!r}, which this "
                "version of reep does not support.",
                hint=_UPGRADE_HINT,
            )
        stamp = data.get("stamp")
        sha = data.get("manifest_sha256")
        if not _valid_legacy_stamp(stamp):
            raise UnsupportedReleaseError("latest.json has no valid stamp.")
        if not isinstance(sha, str) or not _SHA256.match(sha):
            raise UnsupportedReleaseError("latest.json has no manifest checksum.")
        key = data.get("manifest_object_key")
        generated_at = data.get("generated_at")
        return LatestPointer(
            stamp=str(stamp),
            manifest_key=(
                key
                if isinstance(key, str) and key
                else f"releases/{stamp}/release.json"
            ),
            manifest_sha256=sha,
            generated_at=generated_at if isinstance(generated_at, str) else None,
        )

    def manifest(self, stamp: str | None = None) -> ReleaseManifest:
        """Read and check a release manifest.

        Args:
            stamp: The release stamp. ``None`` reads the current release through
                ``latest.json`` and checks the manifest against its checksum.

        Returns:
            The manifest.

        Raises:
            NotFoundError: If the bucket does not have the release.
            NetworkError: If the request fails.
            ChecksumMismatchError: If the manifest does not match ``latest.json``.
            UnsupportedReleaseError: If the manifest has an unknown version.
        """
        pointer = None
        if stamp is None:
            pointer = self.latest()
            body = self._get_bytes(
                pointer.manifest_key,
                max_bytes=pointer.manifest_size or _v4.METADATA_LIMIT,
            )
            if pointer.manifest_size is not None and len(body) != pointer.manifest_size:
                raise ChecksumMismatchError(
                    "Release descriptor byte count differs from selection."
                )
            actual = hashlib.sha256(body).hexdigest()
            if actual != pointer.manifest_sha256:
                raise ChecksumMismatchError(
                    f"release.json for {pointer.stamp} has SHA-256 {actual}, but "
                    f"latest.json expects {pointer.manifest_sha256}.",
                    hint="The release may be changing. Try again in a few minutes.",
                )
            manifest = ReleaseManifest.from_json(_loads(body, pointer.manifest_key))
            expected_stamp = pointer.stamp
        else:
            validate_stamp(stamp)
            key = f"releases/{stamp}/release.json"
            body = self._get_bytes(key, max_bytes=_v4.METADATA_LIMIT)
            manifest = ReleaseManifest.from_json(_loads(body, key))
            expected_stamp = stamp
        if manifest.stamp != expected_stamp:
            raise UnsupportedReleaseError(
                f"release.json names stamp {manifest.stamp}, expected {expected_stamp}."
            )
        if manifest.schema_version == _v4.SCHEMA:
            parsed = _v4.descriptor(
                dict(manifest.raw), pointer.selection if pointer else None
            )
            if pointer is not None and pointer.selection is None:
                raise UnsupportedReleaseError(
                    "A legacy pointer cannot select a v4 descriptor."
                )
            ref = parsed["files"]["manifest.json"]
            raw = self._get_bytes(ref["key"], max_bytes=_v4.METADATA_LIMIT)
            manifest = with_source_manifest(manifest, raw)
        elif pointer is not None and pointer.selection is not None:
            raise UnsupportedReleaseError(
                "A v4 selector cannot select a legacy manifest."
            )
        return replace(manifest, identity_sha256=hashlib.sha256(body).hexdigest())

    def download(
        self,
        release_file: ReleaseFile,
        dest: Path,
        *,
        progress: ProgressCallback | None = None,
    ) -> Path:
        """Download a file and check its SHA-256.

        An existing file with the right checksum is not downloaded again. A partial
        download (``<dest>.part``) resumes with an HTTP range request.

        Args:
            release_file: The manifest entry.
            dest: The local path to write.
            progress: Called with ``(bytes_done, bytes_total)`` while downloading.

        Returns:
            The local path.

        Raises:
            NetworkError: If the download fails after the retries.
            ChecksumMismatchError: If the file does not match the manifest.
        """
        if dest.exists():
            if (
                dest.stat().st_size == release_file.size
                and sha256_file(dest) == release_file.sha256
            ):
                if progress:
                    progress(release_file.size, release_file.size)
                return dest
            dest.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        url = self.url(release_file.object_key)
        attempt = 0
        while True:
            try:
                self._stream_to(url, part, release_file.size, progress)
                break
            except (httpx.TransportError, _RetryableStatusError) as exc:
                if attempt >= self._retries:
                    raise NetworkError(
                        f"Download of {release_file.name} failed: {exc}",
                        hint="Check the network connection and run the command "
                        "again; the download resumes where it stopped.",
                    ) from exc
                delay = self._delay(attempt, exc)
                logger.warning(
                    "Download of %s failed (%s); retrying in %.1f s",
                    release_file.name,
                    exc,
                    delay,
                )
                self._sleep(delay)
                attempt += 1
        actual = sha256_file(part)
        if actual != release_file.sha256:
            part.unlink()
            raise ChecksumMismatchError(
                f"{release_file.name} has SHA-256 {actual}, but the manifest "
                f"expects {release_file.sha256}.",
                hint="Run the command again. If the error repeats, report it at "
                "https://github.com/withqwerty/reep-python/issues.",
            )
        part.replace(dest)
        return dest

    def fetch_bytes(self, object_key: str) -> bytes:
        """Download a small object into memory.

        Args:
            object_key: The object key.

        Returns:
            The body.

        Raises:
            NotFoundError: If the object does not exist.
            NetworkError: If the request fails.
        """
        return self._get_bytes(object_key)

    def _stream_to(
        self,
        url: str,
        part: Path,
        total: int,
        progress: ProgressCallback | None,
    ) -> None:
        offset = part.stat().st_size if part.exists() else 0
        if offset > total:
            part.unlink()
            offset = 0
        if offset == total and total > 0:
            return
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with self._http.stream("GET", url, headers=headers) as response:
            if response.status_code == 404:
                raise NotFoundError(f"{url} does not exist on the release bucket.")
            if response.status_code in _RETRY_STATUSES:
                raise _RetryableStatusError(response)
            if response.status_code == 416:
                part.unlink(missing_ok=True)
                raise _RetryableStatusError(response)
            if response.status_code not in (200, 206):
                raise NetworkError(f"GET {url} returned HTTP {response.status_code}.")
            mode = "ab" if response.status_code == 206 else "wb"
            done = offset if response.status_code == 206 else 0
            with part.open(mode) as fh:
                for chunk in response.iter_bytes():
                    if done + len(chunk) > total:
                        raise ChecksumMismatchError(
                            "Download exceeds its declared byte count."
                        )
                    fh.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        if done != total:
            raise httpx.ReadError(f"received {done} of {total} bytes")

    def _get_bytes(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        url = self.url(object_key)
        attempt = 0
        while True:
            try:
                with self._http.stream("GET", url) as response:
                    if response.status_code == 404:
                        raise NotFoundError(
                            f"{url} does not exist on the release bucket."
                        )
                    if response.status_code in _RETRY_STATUSES:
                        raise _RetryableStatusError(response)
                    if response.status_code != 200:
                        raise NetworkError(
                            f"GET {url} returned HTTP {response.status_code}."
                        )
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        if (
                            max_bytes is not None
                            and len(content) + len(chunk) > max_bytes
                        ):
                            raise UnsupportedReleaseError(
                                "Release metadata exceeds its byte bound."
                            )
                        content.extend(chunk)
                    return bytes(content)
            except (httpx.TransportError, _RetryableStatusError) as exc:
                if attempt >= self._retries:
                    raise NetworkError(
                        f"GET {url} failed: {exc}",
                        hint="Check the network connection and try again.",
                    ) from exc
                self._sleep(self._delay(attempt, exc))
                attempt += 1

    def _get_json(self, object_key: str) -> object:
        return _loads(self._get_bytes(object_key), object_key)

    def _delay(self, attempt: int, exc: Exception) -> float:
        if isinstance(exc, _RetryableStatusError):
            retry_after = exc.retry_after
            if retry_after is not None:
                return min(retry_after, 60.0)
        return float(self._backoff * (2**attempt))


class _RetryableStatusError(Exception):
    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code} from {response.request.url}")
        raw = response.headers.get("Retry-After", "")
        self.retry_after: float | None = float(raw) if raw.isdigit() else None


def _loads(body: bytes, name: str) -> object:
    return _v4.loads(body, name)


def with_source_manifest(manifest: ReleaseManifest, raw: bytes) -> ReleaseManifest:
    """Bind v4 row counts to its exact producer manifest; legacy is unchanged."""
    counts = _v4.source_counts(raw, dict(manifest.raw))
    return replace(
        manifest,
        files={
            name: replace(entry, rows=counts[name.removesuffix(".csv.gz")])
            if name.endswith(".csv.gz")
            else entry
            for name, entry in manifest.files.items()
        },
    )
