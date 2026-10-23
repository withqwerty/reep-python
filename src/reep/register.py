"""The ``Register``: the one entry point for lookups.

``Register`` validates arguments, then asks a backend. The local backend reads a
DuckDB file; the API backend calls the hosted service. Both return the same models.
"""

from __future__ import annotations

import difflib
import math
import numbers
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, TypeVar

from reep import auth, catalog
from reep.backends.api import ApiBackend
from reep.backends.local import LocalBackend
from reep.config import Settings
from reep.errors import (
    AuthError,
    InvalidArgumentError,
    InvalidProviderIdError,
    NotFoundError,
    UnknownNamespaceError,
    UnknownProviderError,
)
from reep.models import (
    ENTITY_TYPES,
    Entity,
    EntityMatch,
    IdState,
    ProviderId,
    ProviderNamespace,
    ReleaseInfo,
    Resolution,
    Translation,
    validate_reep_id,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from types import TracebackType

    import httpx

    from reep.backends.base import Backend

__all__ = ["ProviderIdLike", "Register"]

#: Values that the SDK accepts as a provider ID: a ``ProviderId``, the text
#: ``provider/namespace/external_id`` (or ``provider/external_id`` when the
#: namespace is unambiguous), or a three-item / two-item tuple.
ProviderIdLike = ProviderId | str | tuple[str, str, str] | tuple[str, str]

FrameT = TypeVar("FrameT")

_MAX_LIMIT = 1000


class Register:
    """Look up Reep IDs and provider IDs.

    Create a register with :meth:`local` or :meth:`api`, or pass a backend.

    Args:
        backend: The backend that answers lookups.

    Example:
        >>> reg = Register.local()  # doctest: +SKIP
        >>> reg.resolve("transfermarkt/spieler/568177").reep_id  # doctest: +SKIP
        'rp53af22bbeaa667'
    """

    def __init__(self, backend: Backend) -> None:
        """Create the register."""
        self._backend = backend
        self._release: ReleaseInfo | None = None
        self._pairs: dict[str, frozenset[str] | None] | None = None
        self._provider_list: list[ProviderNamespace] | None = None

    @classmethod
    def local(
        cls,
        path: str | Path | None = None,
        *,
        csv_dir: str | Path | None = None,
        settings: Settings | None = None,
    ) -> Self:
        """Open a local register.

        The register reads, in this order of preference:

        1. ``path``, when given;
        2. a catalogue built from ``csv_dir``, when given;
        3. the file named by ``REEP_DUCKDB_PATH``, when set;
        4. the SDK catalogue that ``reep catalog sync`` builds.

        Args:
            path: A DuckDB file: a shipped ``reep-register-v1.duckdb`` file or an SDK
                catalogue.
            csv_dir: A directory of release CSV files. The SDK builds its catalogue
                from them, and builds it again only when the files change.
            settings: The settings; read from the environment when ``None``.

        Returns:
            The register.

        Raises:
            CatalogMissingError: If no DuckDB file exists at the chosen location.
        """
        settings = settings or Settings.from_env()
        if path is not None:
            return cls(LocalBackend(Path(path).expanduser()))
        if csv_dir is not None:
            source = Path(csv_dir).expanduser()
            current = catalog.state(settings)
            fingerprint = catalog.local_fingerprint(source)
            if current is None or current.import_fingerprint != fingerprint:
                catalog.import_release(source, settings=settings)
            return cls(LocalBackend(settings.catalog_path))
        if settings.duckdb_path is not None:
            return cls(LocalBackend(settings.duckdb_path))
        return cls(LocalBackend(settings.catalog_path))

    @classmethod
    def api(
        cls,
        key: str | None = None,
        *,
        base_url: str | None = None,
        settings: Settings | None = None,
        http: httpx.Client | None = None,
    ) -> Self:
        """Open a register that uses the hosted API.

        Args:
            key: The API key. ``None`` uses ``REEP_API_KEY``, then the key stored by
                ``reep auth login``.
            base_url: The API base URL; the default is ``https://reep.football/api/v1``
                (or ``REEP_BASE_URL``).
            settings: The settings; read from the environment when ``None``.
            http: An ``httpx.Client`` to use, for example in tests.

        Returns:
            The register.

        Raises:
            AuthError: If no key is available.
        """
        settings = settings or Settings.from_env()
        api_key = key or auth.load_key(settings)
        if not api_key:
            raise AuthError(
                "No Reep API key.",
                hint="Set REEP_API_KEY, or run 'reep auth login'. Request a key at "
                "https://reep.football/api. The local catalogue needs no key: run "
                "'reep catalog sync'.",
            )
        return cls(
            ApiBackend(api_key, base_url=base_url or settings.api_url, http=http)
        )

    @property
    def backend(self) -> Backend:
        """Return the backend."""
        return self._backend

    @property
    def release(self) -> ReleaseInfo:
        """Return the release that the register reads."""
        if self._release is None:
            self._release = self._backend.release_info()
        return self._release

    def close(self) -> None:
        """Close the backend."""
        self._backend.close()

    def __enter__(self) -> Self:
        """Return the register."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the register."""
        self.close()

    # -- discovery -----------------------------------------------------------

    def providers(self) -> list[ProviderNamespace]:
        """Return every ``(provider, namespace)`` pair with its bridge count.

        Returns:
            The pairs, sorted by provider and namespace.
        """
        if self._provider_list is None:
            self._provider_list = self._backend.providers()
        return list(self._provider_list)

    def namespaces(self, provider: str) -> list[str]:
        """Return the namespaces of a provider.

        Args:
            provider: The provider slug.

        Returns:
            The namespaces, sorted. The list is empty when the backend does not
            report namespaces.

        Raises:
            UnknownProviderError: If the provider is not in the release.
        """
        pairs = self._provider_pairs()
        key = provider.strip().lower()
        if key not in pairs:
            raise self._unknown_provider(key, pairs)
        return sorted(pairs[key] or ())

    # -- lookups -------------------------------------------------------------

    def resolve(
        self, pid: ProviderIdLike, *, entity_type: str | None = None
    ) -> Resolution:
        """Resolve a provider ID to its current Reep ID.

        When the bridge names an ID that merged into another, the result carries
        the survivor in ``reep_id`` and the bridged ID in ``redirected_from``.

        Args:
            pid: The provider ID.
            entity_type: Optional entity type used to infer the namespace when a
                two-part ``provider/external_id`` is given.

        Returns:
            The resolution. Check ``status`` (or ``found``) before using
            ``reep_id``.

        Raises:
            InvalidProviderIdError: If the ID is not a valid triple, or its provider
                or namespace is not in the release.
        """
        return self.resolve_many([pid], entity_type=entity_type)[0]

    def resolve_many(
        self, pids: Iterable[ProviderIdLike], *, entity_type: str | None = None
    ) -> list[Resolution]:
        """Resolve many provider IDs in one call.

        Args:
            pids: The provider IDs.
            entity_type: Optional entity type used to infer the namespace when
                two-part ``provider/external_id`` values are given.

        Returns:
            One resolution per provider ID, in the order given.

        Raises:
            InvalidProviderIdError: If an ID is not valid.
        """
        parsed = [self._coerce_with_inference(p, entity_type=entity_type) for p in pids]
        for pid in {(p.provider, p.namespace) for p in parsed}:
            self._check_pair(*pid)
        return self._backend.resolve_many(parsed)

    def current(self, reep_id: str) -> IdState:
        """Return the state of a stored Reep ID.

        Use this to keep stored Reep IDs current: store ``current_id`` when the
        status is ``REDIRECTED``, and review the row when it is ``WITHDRAWN`` or
        ``UNKNOWN``.

        Args:
            reep_id: The Reep ID.

        Returns:
            The state.

        Raises:
            InvalidReepIdError: If the value is not a Reep ID.
        """
        return self.current_many([reep_id])[0]

    def current_many(self, reep_ids: Iterable[str]) -> list[IdState]:
        """Return the state of many stored Reep IDs.

        Args:
            reep_ids: The Reep IDs.

        Returns:
            One state per ID, in the order given.

        Raises:
            InvalidReepIdError: If a value is not a Reep ID.
        """
        ids = [validate_reep_id(r) for r in reep_ids]
        return self._backend.current_many(ids)

    def translate(
        self,
        pid: ProviderIdLike,
        to: str,
        *,
        namespace: str | None = None,
        entity_type: str | None = None,
    ) -> Translation:
        """Return another provider's IDs for the entity behind a provider ID.

        Args:
            pid: The source provider ID.
            to: The target provider.
            namespace: Return only this namespace of the target provider.
            entity_type: Optional entity type used to infer the source namespace when
                a two-part ``provider/external_id`` is given.

        Returns:
            The translation. ``targets`` is empty when the source does not resolve
            or the entity has no ID at the target provider.

        Raises:
            InvalidProviderIdError: If the source ID, the target provider or the
                target namespace is not valid.
        """
        return self.translate_many(
            [pid], to, namespace=namespace, entity_type=entity_type
        )[0]

    def translate_many(
        self,
        pids: Iterable[ProviderIdLike],
        to: str,
        *,
        namespace: str | None = None,
        entity_type: str | None = None,
    ) -> list[Translation]:
        """Translate many provider IDs to another provider in batched calls.

        Args:
            pids: The source provider IDs.
            to: The target provider.
            namespace: Return only this namespace of the target provider.
            entity_type: Optional entity type used to infer the source namespace when
                two-part ``provider/external_id`` values are given.

        Returns:
            One translation per input provider ID, in the order given.
        """
        sources = [
            self._coerce_with_inference(p, entity_type=entity_type) for p in pids
        ]
        for prov, ns in {(p.provider, p.namespace) for p in sources}:
            self._check_pair(prov, ns)
        target = to.strip().lower()
        pairs = self._provider_pairs()
        if target not in pairs:
            raise self._unknown_provider(target, pairs)
        target_ns = namespace.strip().lower() if namespace is not None else None
        if target_ns is not None:
            self._check_pair(target, target_ns)
        if not sources:
            return []
        resolutions = self._backend.resolve_many(sources)
        resolved_ids = sorted(
            {r.reep_id for r in resolutions if r.found and r.reep_id is not None}
        )
        bridges_by_id = self._backend.bridges_many(resolved_ids, target, target_ns)
        out: list[Translation] = []
        for source, resolution in zip(sources, resolutions, strict=True):
            if not resolution.found or resolution.reep_id is None:
                out.append(Translation(resolution=resolution, to=target))
                continue
            bridges = [
                b
                for b in bridges_by_id.get(resolution.reep_id, ())
                if b.provider_id.provider == target
                and (target_ns is None or b.provider_id.namespace == target_ns)
                and b.provider_id != source
            ]
            bridges.sort(
                key=lambda b: (
                    b.upstream_status is not None,
                    b.provider_id.namespace,
                    b.provider_id.external_id,
                )
            )
            out.append(
                Translation(resolution=resolution, to=target, targets=tuple(bridges))
            )
        return out

    def entity(self, reep_id: str, *, follow_redirects: bool = True) -> Entity:
        """Return an entity with its bridges.

        Args:
            reep_id: The Reep ID.
            follow_redirects: Return the survivor when the ID merged into another.

        Returns:
            The entity.

        Raises:
            InvalidReepIdError: If the value is not a Reep ID.
            NotFoundError: If the release does not have the entity.
        """
        rid = validate_reep_id(reep_id)
        found = self._backend.entity(rid, follow_redirects=follow_redirects)
        if found is None:
            raise NotFoundError(
                f"The release has no entity {rid}.",
                hint="Use current() to check whether the ID was withdrawn.",
            )
        return found

    def entities_many(
        self, reep_ids: Iterable[str], *, follow_redirects: bool = True
    ) -> list[Entity | None]:
        """Return many entities with their bridges and aliases in one batch call.

        Args:
            reep_ids: The Reep IDs.
            follow_redirects: Return the survivor when an ID merged into another.

        Returns:
            One ``Entity`` (or ``None`` if not in the release) per input ID.
        """
        ids = [validate_reep_id(r) for r in reep_ids]
        found = self._backend.entities_many(ids, follow_redirects=follow_redirects)
        return [found.get(rid) for rid in ids]

    def search(
        self,
        query: str,
        *,
        entity_type: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[EntityMatch]:
        """Search entities by name.

        Search is for discovery. Do not use it to match records: names are not
        unique, and the public release has no dates of birth. Match records by
        provider ID with :meth:`resolve`.

        Args:
            query: The name, for example ``"Cole Palmer"``.
            entity_type: Return only this entity type, for example ``"player"``.
            limit: The maximum number of results (1 to 1000).
            offset: The number of results to skip.

        Returns:
            The matches, best first.

        Raises:
            InvalidArgumentError: If an argument is not valid.
        """
        text = query.strip()
        if len(text) < 2:
            raise InvalidArgumentError(
                "The search query needs at least 2 characters.",
                hint="Search by name, for example 'Cole Palmer'.",
            )
        if entity_type is not None and entity_type not in ENTITY_TYPES:
            raise InvalidArgumentError(
                f"Unknown entity type {entity_type!r}.",
                hint=f"Use one of: {', '.join(ENTITY_TYPES)}.",
            )
        if not 1 <= limit <= _MAX_LIMIT:
            raise InvalidArgumentError(f"limit must be between 1 and {_MAX_LIMIT}.")
        if offset < 0:
            raise InvalidArgumentError("offset must be 0 or more.")
        return self._backend.search(
            text, entity_type=entity_type, limit=limit, offset=offset
        )

    def resolve_frame(
        self,
        frame: FrameT,
        *,
        provider: str,
        namespace: str | None = None,
        entity_type: str | None = None,
        column: str,
        into: str = "reep_id",
        status_into: str = "reep_status",
    ) -> FrameT:
        """Add Reep IDs to a pandas or polars DataFrame, or a pyarrow Table.

        The method resolves the distinct values of ``column`` as IDs of
        ``(provider, namespace)`` and returns a new frame of the same type with two
        more columns: ``into`` (the current Reep ID) and ``status_into`` (the
        ``ResolutionStatus`` value). Rows with an empty ID get nulls.

        Args:
            frame: A pandas DataFrame, a polars DataFrame or a pyarrow Table.
            provider: The provider of the IDs in ``column``.
            namespace: The namespace of the IDs in ``column`` (optional when the
                provider has one namespace or ``entity_type`` disambiguates it).
            entity_type: Optional entity type used to infer ``namespace`` when
                ``namespace`` is omitted.
            column: The column that holds the provider IDs.
            into: The name of the new Reep ID column.
            status_into: The name of the new status column.

        Returns:
            A new frame of the same type.

        Raises:
            InvalidArgumentError: If the frame type, a column name or a value is not
                usable.
            InvalidProviderIdError: If the provider or namespace is not in the
                release, or the column holds floating-point numbers.
        """
        prov = provider.strip().lower()
        ns = (
            namespace.strip().lower()
            if namespace is not None
            else self._infer_namespace(prov, entity_type=entity_type)
        )
        self._check_pair(prov, ns)
        kind = _frame_kind(frame)
        names = _frame_columns(frame, kind)
        if column not in names:
            raise InvalidArgumentError(
                f"The frame has no column {column!r}.",
                hint=f"Columns: {', '.join(names[:20])}.",
            )
        for new in (into, status_into):
            if new in names:
                raise InvalidArgumentError(
                    f"The frame already has a column {new!r}.",
                    hint="Pass another name with into= or status_into=.",
                )
        values = [_external_id(v, column) for v in _frame_values(frame, kind, column)]
        distinct = sorted({v for v in values if v is not None})
        answers = dict(
            zip(distinct, self._backend.resolve_ids(prov, ns, distinct), strict=True)
        )
        reep_ids = [answers[v][0] if v is not None else None for v in values]
        statuses = [answers[v][1] if v is not None else None for v in values]
        return _frame_with(frame, kind, {into: reep_ids, status_into: statuses})

    def translate_frame(
        self,
        frame: FrameT,
        *,
        provider: str,
        namespace: str | None = None,
        entity_type: str | None = None,
        column: str,
        to: str,
        to_namespace: str | None = None,
        into: str | None = None,
        reep_id_into: str | None = None,
        status_into: str | None = None,
    ) -> FrameT:
        """Translate a column of provider IDs to another provider's IDs on a DataFrame.

        Args:
            frame: A pandas DataFrame, a polars DataFrame or a pyarrow Table.
            provider: The source provider of the IDs in ``column``.
            namespace: The source namespace (optional when unambiguous or inferred
                via ``entity_type``).
            entity_type: Optional entity type used to infer ``namespace``.
            column: The column that holds the source provider IDs.
            to: The target provider slug.
            to_namespace: Optional target namespace filter.
            into: Name of the new target provider ID column (defaults to
                ``f"{to}_id"``).
            reep_id_into: Optional column name to also write the resolved Reep ID.
            status_into: Optional column name to also write the resolution status.

        Returns:
            A new frame of the same type.
        """
        prov = provider.strip().lower()
        ns = (
            namespace.strip().lower()
            if namespace is not None
            else self._infer_namespace(prov, entity_type=entity_type)
        )
        self._check_pair(prov, ns)
        target = to.strip().lower()
        pairs = self._provider_pairs()
        if target not in pairs:
            raise self._unknown_provider(target, pairs)
        target_ns = to_namespace.strip().lower() if to_namespace is not None else None
        if target_ns is not None:
            self._check_pair(target, target_ns)
        target_col = into or f"{target}_id"
        kind = _frame_kind(frame)
        names = _frame_columns(frame, kind)
        if column not in names:
            raise InvalidArgumentError(
                f"The frame has no column {column!r}.",
                hint=f"Columns: {', '.join(names[:20])}.",
            )
        new_cols = [c for c in (target_col, reep_id_into, status_into) if c is not None]
        for new in new_cols:
            if new in names:
                raise InvalidArgumentError(
                    f"The frame already has a column {new!r}.",
                    hint="Pass another column name.",
                )
        values = [_external_id(v, column) for v in _frame_values(frame, kind, column)]
        distinct = sorted({v for v in values if v is not None})
        translations = self.translate_many(
            [ProviderId(prov, ns, v) for v in distinct],
            target,
            namespace=target_ns,
        )
        by_val: dict[str, Translation] = dict(zip(distinct, translations, strict=True))
        out_cols: dict[str, list[str | None]] = {
            target_col: [
                (
                    by_val[v].targets[0].provider_id.external_id
                    if v is not None and by_val[v].targets
                    else None
                )
                for v in values
            ]
        }
        if reep_id_into is not None:
            out_cols[reep_id_into] = [
                by_val[v].resolution.reep_id if v is not None else None for v in values
            ]
        if status_into is not None:
            out_cols[status_into] = [
                by_val[v].resolution.status.value if v is not None else None
                for v in values
            ]
        return _frame_with(frame, kind, out_cols)

    # -- matches & API operations --------------------------------------------

    def matches(
        self,
        *,
        season_id: str | None = None,
        competition_id: str | None = None,
        team_id: str | None = None,
        home_team_id: str | None = None,
        away_team_id: str | None = None,
        venue_id: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return remote fixtures, retaining the list interface and offset.

        Dates are inclusive. ``venue_id`` is unsupported; use home/away team IDs.
        API pagination fails explicitly if its generation changes. For cursor,
        unresolved-anchor and freshness metadata, use :meth:`matches_page`.
        """
        if not isinstance(self._backend, ApiBackend):
            raise InvalidArgumentError(
                "Register.matches() requires an API backend.",
                hint="Use Register.api() for remote fixture queries.",
            )
        return self._backend.matches(
            season_id=season_id,
            competition_id=competition_id,
            team_id=team_id,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            venue_id=venue_id,
            date=date,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )

    def matches_page(
        self,
        *,
        season_id: str | None = None,
        competition_id: str | None = None,
        team_id: str | None = None,
        home_team_id: str | None = None,
        away_team_id: str | None = None,
        venue_id: str | None = None,
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return a remote fixture page with generation, cursor and API metadata.

        Pass the same filters and ``limit`` with ``next_cursor`` to continue.
        The API may advance independently of downloads. Expired or invalidated
        cursors raise ``ApiError``; no automatic restart combines generations.
        """
        if not isinstance(self._backend, ApiBackend):
            raise InvalidArgumentError("matches_page() requires an API backend.")
        return self._backend.matches_page(
            season_id=season_id,
            competition_id=competition_id,
            team_id=team_id,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            venue_id=venue_id,
            date=date,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            cursor=cursor,
        )

    def changes(
        self,
        *,
        since: str | None = None,
        to: str | None = None,
        event_type: str | None = None,
        provider: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one page of changelog events (API backend only)."""
        fn = getattr(self._backend, "changes", None)
        if fn is None:
            raise InvalidArgumentError("changes() requires the API backend.")
        return fn(  # type: ignore[no-any-return]
            since=since,
            to=to,
            event_type=event_type,
            provider=provider,
            limit=limit,
            cursor=cursor,
        )

    def releases(self) -> dict[str, Any]:
        """Return the release chain (API backend only)."""
        fn = getattr(self._backend, "releases", None)
        if fn is None:
            raise InvalidArgumentError("releases() requires the API backend.")
        return fn()  # type: ignore[no-any-return]

    def key_status(self) -> dict[str, Any]:
        """Return the API key's plan, expiry and remaining quota (API backend only)."""
        fn = getattr(self._backend, "key_status", None)
        if fn is None:
            raise InvalidArgumentError("key_status() requires the API backend.")
        return fn()  # type: ignore[no-any-return]

    def notices(
        self,
        *,
        provider: str,
        status: str = "open",
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return partner bridge-defect notices (API backend only)."""
        fn = getattr(self._backend, "notices", None)
        if fn is None:
            raise InvalidArgumentError("notices() requires the API backend.")
        return fn(  # type: ignore[no-any-return]
            provider=provider,
            status=status,
            limit=limit,
            cursor=cursor,
        )

    def acknowledge_notices(
        self,
        *,
        provider: str,
        ids: Sequence[str],
        disposition: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Acknowledge partner bridge-defect notices (API backend only)."""
        fn = getattr(self._backend, "acknowledge_notices", None)
        if fn is None:
            raise InvalidArgumentError(
                "acknowledge_notices() requires the API backend."
            )
        return fn(  # type: ignore[no-any-return]
            provider=provider,
            ids=ids,
            disposition=disposition,
            note=note,
        )

    # -- validation ----------------------------------------------------------

    def _provider_pairs(self) -> dict[str, frozenset[str] | None]:
        if self._pairs is None:
            pairs: dict[str, set[str] | None] = {}
            for item in self.providers():
                if item.namespace is None:
                    pairs.setdefault(item.provider, None)
                    continue
                existing = pairs.get(item.provider)
                if existing is None:
                    pairs[item.provider] = {item.namespace}
                else:
                    existing.add(item.namespace)
            self._pairs = {
                k: frozenset(v) if v is not None else None for k, v in pairs.items()
            }
        return self._pairs

    def _infer_namespace(self, provider: str, *, entity_type: str | None = None) -> str:
        pairs = self._provider_pairs()
        if provider not in pairs:
            raise self._unknown_provider(provider, pairs)
        rows = [
            p
            for p in self.providers()
            if p.provider == provider and p.namespace is not None
        ]
        if entity_type is not None:
            if entity_type not in ENTITY_TYPES:
                raise InvalidArgumentError(
                    f"Unknown entity type {entity_type!r}.",
                    hint=f"Use one of: {', '.join(ENTITY_TYPES)}.",
                )
            matching = [
                p.namespace
                for p in rows
                if p.namespace is not None and entity_type in p.entity_types
            ]
            if len(matching) == 1:
                return matching[0]
        all_ns = sorted({p.namespace for p in rows if p.namespace is not None})
        if len(all_ns) == 1:
            return all_ns[0]
        if all_ns:
            raise InvalidProviderIdError(
                f"A provider ID for {provider!r} needs three parts: "
                f"provider/namespace/external_id ({provider!r} has multiple "
                f"namespaces: {', '.join(all_ns)}).",
                hint=f"Specify a namespace ({', '.join(all_ns)}) or pass "
                "entity_type= to disambiguate.",
            )
        raise InvalidProviderIdError(
            f"A provider ID for {provider!r} needs three parts: "
            "provider/namespace/external_id.",
            hint="Pass provider/namespace/external_id.",
        )

    def _coerce_with_inference(
        self, value: object, *, entity_type: str | None = None
    ) -> ProviderId:
        if isinstance(value, ProviderId):
            return value
        if isinstance(value, tuple):
            if len(value) == 3:
                return ProviderId(*value)
            if len(value) == 2 and all(isinstance(x, str) for x in value):
                prov = value[0].strip().lower()
                ns = self._infer_namespace(prov, entity_type=entity_type)
                return ProviderId(prov, ns, value[1])
        if isinstance(value, str):
            text = value.strip()
            sep = "/" if "/" in text else (":" if ":" in text else None)
            if sep is not None:
                parts = text.split(sep)
                if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                    prov = parts[0].strip().lower()
                    ns = self._infer_namespace(prov, entity_type=entity_type)
                    return ProviderId(prov, ns, parts[1])
            return ProviderId.parse(value)
        return _coerce(value)

    def _check_pair(self, provider: str, namespace: str) -> None:
        pairs = self._provider_pairs()
        if provider not in pairs:
            raise self._unknown_provider(provider, pairs)
        namespaces = pairs[provider]
        if namespaces is not None and namespace not in namespaces:
            close = difflib.get_close_matches(namespace, sorted(namespaces), n=1)
            suggestion = f" Did you mean {close[0]!r}?" if close else ""
            raise UnknownNamespaceError(
                f"{provider} has no namespace {namespace!r}.{suggestion}",
                hint=(
                    f"Valid namespaces for {provider}: {', '.join(sorted(namespaces))}."
                ),
            )

    @staticmethod
    def _unknown_provider(
        provider: str, pairs: dict[str, frozenset[str] | None]
    ) -> UnknownProviderError:
        close = difflib.get_close_matches(provider, sorted(pairs), n=1)
        suggestion = f" Did you mean {close[0]!r}?" if close else ""
        return UnknownProviderError(
            f"The release has no provider {provider!r}.{suggestion}",
            hint="Run 'reep providers' to list the providers and their namespaces.",
        )


def _coerce(value: object) -> ProviderId:
    if isinstance(value, ProviderId):
        return value
    if isinstance(value, str):
        return ProviderId.parse(value)
    if isinstance(value, tuple) and len(value) == 3:
        return ProviderId(*value)
    raise InvalidProviderIdError(
        f"Cannot use {type(value).__name__} {value!r} as a provider ID.",
        hint="Pass a ProviderId, the text provider/namespace/external_id, or a "
        "(provider, namespace, external_id) tuple.",
    )


def _external_id(value: object, column: str) -> str | None:
    if value is None:
        return None
    if type(value).__name__ in {"NAType", "NaTType"}:
        return None
    if isinstance(value, float):
        if math.isnan(value):
            return None
        raise InvalidProviderIdError(
            f"Column {column!r} holds floating-point numbers such as {value!r}.",
            hint="Convert the column to strings (or to pandas 'Int64') first, so that "
            "IDs such as 568177 do not become '568177.0'.",
        )
    if isinstance(value, bool):
        raise InvalidArgumentError(f"Column {column!r} holds booleans.")
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, bytes):
        return value.decode().strip() or None
    raise InvalidArgumentError(
        f"Column {column!r} holds {type(value).__name__} values.",
        hint="The ID column must hold strings or integers.",
    )


def _frame_kind(frame: object) -> str:
    module = type(frame).__module__.split(".")[0]
    name = type(frame).__name__
    if module == "pandas" and name == "DataFrame":
        return "pandas"
    if module == "polars" and name == "DataFrame":
        return "polars"
    if module == "pyarrow" and name == "Table":
        return "arrow"
    raise InvalidArgumentError(
        f"resolve_frame does not accept {type(frame).__name__}.",
        hint="Pass a pandas DataFrame, a polars DataFrame or a pyarrow Table.",
    )


def _frame_columns(frame: Any, kind: str) -> list[str]:
    if kind == "arrow":
        return [str(c) for c in frame.column_names]
    return [str(c) for c in frame.columns]


def _frame_values(frame: Any, kind: str, column: str) -> Sequence[object]:
    if kind == "pandas":
        return list(frame[column].tolist())
    if kind == "polars":
        return list(frame[column].to_list())
    return list(frame[column].to_pylist())


def _frame_with(
    frame: FrameT, kind: str, columns: dict[str, list[str | None]]
) -> FrameT:
    target: Any = frame
    if kind == "pandas":
        return target.assign(**columns)  # type: ignore[no-any-return]
    if kind == "polars":
        import polars as pl

        return target.with_columns(  # type: ignore[no-any-return]
            [
                pl.Series(name, values, dtype=pl.String)
                for name, values in columns.items()
            ]
        )
    import pyarrow as pa

    for name, values in columns.items():
        target = target.append_column(name, pa.array(values, type=pa.string()))
    return target  # type: ignore[no-any-return]
