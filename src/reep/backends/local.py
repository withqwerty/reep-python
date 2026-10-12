"""The local backend: lookups against a DuckDB file.

The backend reads either the SDK's own catalogue or the DuckDB file that ships with
each release. Both use the same table layout. The backend opens the file read-only,
so several processes can read it at the same time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from reep import _store
from reep.errors import CatalogMissingError, UnsupportedReleaseError
from reep.models import (
    ENTITY_TYPE_BY_LETTER,
    WITHDRAWN_REASONS,
    Bridge,
    Entity,
    EntityMatch,
    IdState,
    IdStatus,
    ProviderId,
    ProviderNamespace,
    ReleaseInfo,
    Resolution,
    ResolutionStatus,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import duckdb

__all__ = ["LocalBackend"]

logger = logging.getLogger(__name__)

_MAX_REDIRECT_HOPS = 8

_BRIDGES_FOR_TRIPLES = """
WITH q AS (
    SELECT unnest($idx) AS idx, unnest($provider) AS provider,
           unnest($namespace) AS namespace, unnest($external_id) AS external_id
)
SELECT q.idx, b.reep_id, nullif(b.rung, ''), nullif(b.upstream_status, '')
FROM q
JOIN bridges b
  ON b.provider = q.provider AND b.namespace = q.namespace
 AND b.external_id = q.external_id
ORDER BY q.idx, b.reep_id
"""

_STATES = """
WITH q AS (SELECT DISTINCT unnest($ids) AS reep_id)
SELECT q.reep_id, e.entity_type, e.status, nullif(e.label, ''),
       nullif(r.to_id, ''), r.reason
FROM q
LEFT JOIN entities e ON e.reep_id = q.reep_id
LEFT JOIN redirects r ON r.from_id = q.reep_id
"""

_IDS_FOR_PAIR = """
WITH q AS (SELECT DISTINCT unnest($ids) AS external_id)
SELECT q.external_id, b.reep_id
FROM q
JOIN bridges b
  ON b.provider = $provider AND b.namespace = $namespace
 AND b.external_id = q.external_id
"""


class LocalBackend:
    """Answer lookups from a local DuckDB file.

    Args:
        path: The DuckDB file: the SDK catalogue or a shipped release file.

    Raises:
        CatalogMissingError: If the file does not exist.
        UnsupportedReleaseError: If the file lacks the bridge tables.
    """

    name = "local"

    def __init__(self, path: Path) -> None:
        """Open the file read-only."""
        if not path.is_file():
            raise CatalogMissingError(
                f"No Reep catalogue at {path}.",
                hint="Run 'reep catalog sync' to download one, or set "
                "REEP_DUCKDB_PATH to a downloaded reep-register-v1.duckdb file.",
            )
        self.path = path
        self._con = _store.connect(path, read_only=True)
        self._tables = _store.table_names(self._con)
        missing = [t for t in _store.SLIM_TABLES if t not in self._tables]
        if missing:
            self._con.close()
            raise UnsupportedReleaseError(
                f"{path} is not a Reep store: it lacks {', '.join(missing)}.",
                hint="Point REEP_DUCKDB_PATH at a reep-register-v1.duckdb file.",
            )
        self._meta = _store.read_metadata(self._con)
        self._providers: list[ProviderNamespace] | None = None

    def close(self) -> None:
        """Close the DuckDB connection."""
        self._con.close()

    @property
    def tables(self) -> frozenset[str]:
        """Return the tables in the store."""
        return frozenset(self._tables)

    def release_info(self) -> ReleaseInfo:
        """Return the release that the store holds."""
        meta = self._meta
        shipped = _store.META_SOURCE not in meta
        return ReleaseInfo(
            stamp=meta.get(_store.META_STAMP),
            schema_version=meta.get(_store.META_SCHEMA),
            backend=self.name,
            source="shipped_duckdb" if shipped else meta[_store.META_SOURCE],
            profile=None if shipped else meta.get(_store.META_PROFILE),
            generated_at=meta.get(_store.META_GENERATED_AT),
            path=self.path,
            tables=tuple(sorted(self._tables)),
            bridge_attributes_stamp=(
                meta.get(_store.META_STAMP)
                if shipped
                else meta.get(_store.META_BRIDGE_ATTRS)
            ),
            built_at=meta.get(_store.META_BUILT_AT),
            extra={
                k: v
                for k, v in meta.items()
                if k
                not in {
                    _store.META_STAMP,
                    _store.META_SCHEMA,
                    _store.META_GENERATED_AT,
                    _store.META_BUILT_AT,
                    _store.META_SOURCE,
                    _store.META_PROFILE,
                    _store.META_BRIDGE_ATTRS,
                }
            },
        )

    @property
    def stamp(self) -> str | None:
        """Return the release stamp of the store."""
        return self._meta.get(_store.META_STAMP)

    def providers(self) -> list[ProviderNamespace]:
        """Return every ``(provider, namespace)`` pair with its bridge count."""
        if self._providers is None:
            rows = self._query(
                "SELECT provider, namespace, count(*) AS n, "
                "list(DISTINCT substr(reep_id, 2, 1)) AS letters "
                "FROM bridges GROUP BY provider, namespace "
                "ORDER BY provider, namespace"
            )
            self._providers = [
                ProviderNamespace(
                    provider=str(provider),
                    namespace=str(namespace),
                    bridge_count=int(count),
                    entity_types=tuple(
                        sorted(
                            ENTITY_TYPE_BY_LETTER[letter]
                            for letter in letters
                            if letter in ENTITY_TYPE_BY_LETTER
                        )
                    ),
                )
                for provider, namespace, count, letters in rows
            ]
        return list(self._providers)

    def resolve_many(self, pids: Sequence[ProviderId]) -> list[Resolution]:
        """Resolve provider IDs, in the order given.

        Args:
            pids: The provider IDs.

        Returns:
            One resolution per provider ID.
        """
        if not pids:
            return []
        rows = self._query(
            _BRIDGES_FOR_TRIPLES,
            {
                "idx": list(range(len(pids))),
                "provider": [p.provider for p in pids],
                "namespace": [p.namespace for p in pids],
                "external_id": [p.external_id for p in pids],
            },
        )
        matches: dict[int, list[tuple[str, str | None, str | None]]] = {}
        for idx, reep_id, rung, upstream in rows:
            matches.setdefault(int(idx), []).append((str(reep_id), rung, upstream))
        states = self._states(
            {rid for found in matches.values() for rid, _, _ in found}
        )
        stamp = self.stamp
        return [
            _resolution(pid, matches.get(i, []), states, stamp)
            for i, pid in enumerate(pids)
        ]

    def resolve_ids(
        self, provider: str, namespace: str, external_ids: Sequence[str]
    ) -> list[tuple[str | None, str]]:
        """Resolve many IDs of one ``(provider, namespace)`` pair.

        Args:
            provider: The provider slug.
            namespace: The namespace.
            external_ids: The provider's IDs.

        Returns:
            One ``(reep_id, status)`` pair per input ID, in the order given.
        """
        if not external_ids:
            return []
        rows = self._query(
            _IDS_FOR_PAIR,
            {"ids": list(external_ids), "provider": provider, "namespace": namespace},
        )
        by_id: dict[str, set[str]] = {}
        for external_id, reep_id in rows:
            by_id.setdefault(str(external_id), set()).add(str(reep_id))
        states = self._states({rid for rids in by_id.values() for rid in rids})
        out: list[tuple[str | None, str]] = []
        for external_id in external_ids:
            rids = by_id.get(external_id)
            if not rids:
                out.append((None, ResolutionStatus.NOT_FOUND.value))
                continue
            current = {_current_or_none(states[rid]) for rid in rids}
            if len(rids) > 1 and len(current) > 1:
                out.append((None, ResolutionStatus.AMBIGUOUS.value))
                continue
            state = states[next(iter(rids))]
            if state.status is IdStatus.WITHDRAWN:
                out.append((None, ResolutionStatus.WITHDRAWN.value))
            else:
                out.append((state.current_id, ResolutionStatus.FOUND.value))
        return out

    def current_many(self, reep_ids: Sequence[str]) -> list[IdState]:
        """Return the state of stored Reep IDs, in the order given.

        Args:
            reep_ids: Valid Reep IDs.

        Returns:
            One state per ID.
        """
        states = self._states(set(reep_ids))
        return [states[rid] for rid in reep_ids]

    def entity(self, reep_id: str, *, follow_redirects: bool = True) -> Entity | None:
        """Return an entity with its bridges and aliases.

        Args:
            reep_id: A valid Reep ID.
            follow_redirects: Return the survivor when the ID merged into another.

        Returns:
            The entity, or ``None`` if the store does not have it.
        """
        state = self._states({reep_id})[reep_id]
        target = reep_id
        if (
            follow_redirects
            and state.status is IdStatus.REDIRECTED
            and state.current_id
        ):
            target = state.current_id
        rows = self._query(
            "SELECT reep_id, entity_type, status, nullif(label, ''), "
            "nullif(gender, ''), nullif(country, ''), "
            "nullif(corroboration_grade, ''), nullif(corroboration_count, '') "
            "FROM entities WHERE reep_id = $id",
            {"id": target},
        )
        if not rows:
            return None
        rid, etype, status, label, gender, country, grade, count = rows[0]
        bridges = tuple(
            Bridge(
                provider_id=ProviderId(str(p), str(n), str(x)),
                reep_id=target,
                rung=rung,
                upstream_status=upstream,
            )
            for p, n, x, rung, upstream in self._query(
                "SELECT provider, namespace, external_id, nullif(rung, ''), "
                "nullif(upstream_status, '') FROM bridges WHERE reep_id = $id "
                "ORDER BY provider, namespace, external_id",
                {"id": target},
            )
        )
        aliases: tuple[str, ...] = ()
        if "aliases" in self._tables:
            aliases = tuple(
                str(row[0])
                for row in self._query(
                    "SELECT alias FROM aliases WHERE reep_id = $id "
                    "GROUP BY alias ORDER BY min(TRY_CAST(rank AS INTEGER)) "
                    "NULLS LAST, alias",
                    {"id": target},
                )
            )
        return Entity(
            reep_id=str(rid),
            entity_type=str(etype),
            status=str(status),
            label=label,
            gender=gender,
            country=country,
            corroboration_grade=grade,
            corroboration_count=int(count) if count is not None else None,
            bridges=bridges,
            aliases=aliases,
            redirected_from=reep_id if target != reep_id else None,
            release_stamp=self.stamp,
        )

    def search(
        self,
        query: str,
        *,
        entity_type: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[EntityMatch]:
        """Search entities by label and alias.

        Every word of the query must occur in the entity's label or one of its
        names. Accents and case are ignored.

        Args:
            query: The name to search for.
            entity_type: Return only this entity type.
            limit: The maximum number of results.
            offset: The number of results to skip.

        Returns:
            The matches, best first.
        """
        normalised = self._normalise(query)
        tokens = normalised.split()
        if not tokens:
            return []
        params: dict[str, Any] = {"q": normalised, "limit": limit, "offset": offset}
        conditions = []
        for i, token in enumerate(tokens):
            params[f"t{i}"] = token
            conditions.append(f"contains(strip_accents(lower(s.search_text)), $t{i})")
        if entity_type:
            params["etype"] = entity_type
            conditions.append("s.entity_type = $etype")
        # The conditions are constant SQL with bound parameters.
        sql = (
            "WITH m AS (SELECT s.reep_id, s.entity_type, s.label, s.bridge_count, "
            "strip_accents(lower(coalesce(s.label, ''))) AS nl "
            f"FROM entity_search s WHERE {' AND '.join(conditions)}) "
            "SELECT m.reep_id, m.entity_type, nullif(m.label, ''), e.status, "
            "m.bridge_count, nullif(e.gender, ''), nullif(e.country, ''), "
            "CASE WHEN m.nl = $q THEN 0 "
            "WHEN starts_with(m.nl, $q) THEN 1 WHEN contains(m.nl, $q) THEN 2 "
            "ELSE 3 END AS rank "
            "FROM m LEFT JOIN entities e USING (reep_id) "
            "ORDER BY rank, coalesce(e.status, '') <> 'active', "
            "m.bridge_count DESC, m.label, m.reep_id "
            "LIMIT $limit OFFSET $offset"
        )
        return [
            EntityMatch(
                reep_id=str(rid),
                entity_type=str(etype),
                label=label,
                status=status,
                bridge_count=int(bridges),
                rank=int(rank),
                gender=gender,
                country=country,
            )
            for rid, etype, label, status, bridges, gender, country, rank in (
                self._query(sql, params)
            )
        ]

    def _normalise(self, text: str) -> str:
        row = self._query(
            "SELECT trim(regexp_replace(strip_accents(lower($t)), '\\s+', ' ', 'g'))",
            {"t": text},
        )
        return str(row[0][0]) if row and row[0][0] is not None else ""

    def _states(self, reep_ids: set[str]) -> dict[str, IdState]:
        """Return the state of each ID, following merge redirects to the survivor."""
        raw = self._raw_states(reep_ids)
        stamp = self.stamp
        out: dict[str, IdState] = {}
        for rid in reep_ids:
            out[rid] = self._final_state(rid, raw, stamp)
        return out

    def _raw_states(
        self, reep_ids: set[str]
    ) -> dict[str, tuple[str | None, str | None, str | None, str | None, str | None]]:
        raw: dict[
            str, tuple[str | None, str | None, str | None, str | None, str | None]
        ] = {}
        pending = set(reep_ids)
        for _ in range(_MAX_REDIRECT_HOPS + 1):
            if not pending:
                break
            rows = self._query(_STATES, {"ids": sorted(pending)})
            pending = set()
            for rid, etype, status, label, to_id, reason in rows:
                raw[str(rid)] = (etype, status, label, to_id, reason)
                if reason == "merged" and to_id and to_id not in raw:
                    pending.add(str(to_id))
        return raw

    def _final_state(
        self,
        rid: str,
        raw: dict[
            str, tuple[str | None, str | None, str | None, str | None, str | None]
        ],
        stamp: str | None,
    ) -> IdState:
        etype_from_id = ENTITY_TYPE_BY_LETTER.get(rid[1])
        etype, status, label, to_id, reason = raw.get(
            rid, (None, None, None, None, None)
        )
        if reason == "merged" and to_id:
            seen = {rid}
            current = to_id
            for _ in range(_MAX_REDIRECT_HOPS):
                c_type, c_status, c_label, c_to, c_reason = raw.get(
                    current, (None, None, None, None, None)
                )
                if c_reason == "merged" and c_to and c_to not in seen:
                    seen.add(current)
                    current = c_to
                    continue
                if c_reason in WITHDRAWN_REASONS or c_status is None:
                    return IdState(
                        reep_id=rid,
                        status=IdStatus.WITHDRAWN,
                        reason=c_reason or "target_not_published",
                        entity_type=etype_from_id,
                        label=label,
                        release_stamp=stamp,
                    )
                return IdState(
                    reep_id=rid,
                    status=IdStatus.REDIRECTED,
                    current_id=current,
                    current_status=c_status,
                    reason="merged",
                    entity_type=c_type or etype_from_id,
                    label=c_label,
                    release_stamp=stamp,
                )
            logger.warning("Redirect chain from %s does not end", rid)
            return IdState(
                reep_id=rid,
                status=IdStatus.WITHDRAWN,
                reason="redirect_loop",
                entity_type=etype_from_id,
                release_stamp=stamp,
            )
        if reason in WITHDRAWN_REASONS:
            return IdState(
                reep_id=rid,
                status=IdStatus.WITHDRAWN,
                reason=reason,
                entity_type=etype or etype_from_id,
                label=label,
                release_stamp=stamp,
            )
        if status is None:
            return IdState(
                reep_id=rid,
                status=IdStatus.UNKNOWN,
                entity_type=etype_from_id,
                release_stamp=stamp,
            )
        if status == "redirected":
            # A redirected entity without a redirect row: the release names no
            # survivor.
            return IdState(
                reep_id=rid,
                status=IdStatus.WITHDRAWN,
                reason=reason,
                entity_type=etype or etype_from_id,
                label=label,
                release_stamp=stamp,
            )
        return IdState(
            reep_id=rid,
            status=IdStatus.RETIRED if status == "retired" else IdStatus.ACTIVE,
            current_id=rid,
            current_status=status,
            entity_type=etype or etype_from_id,
            label=label,
            release_stamp=stamp,
        )

    def _query(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[tuple[Any, ...]]:
        cursor: duckdb.DuckDBPyConnection = self._con.cursor()
        try:
            return cursor.execute(sql, params or {}).fetchall()
        finally:
            cursor.close()


def _current_or_none(state: IdState) -> str | None:
    return state.current_id


def _resolution(
    pid: ProviderId,
    found: list[tuple[str, str | None, str | None]],
    states: dict[str, IdState],
    stamp: str | None,
) -> Resolution:
    if not found:
        return Resolution(
            query=pid, status=ResolutionStatus.NOT_FOUND, release_stamp=stamp
        )
    if len({rid for rid, _, _ in found}) > 1:
        candidates = tuple(
            sorted({states[rid].current_id or rid for rid, _, _ in found})
        )
        if len(candidates) > 1:
            return Resolution(
                query=pid,
                status=ResolutionStatus.AMBIGUOUS,
                candidates=candidates,
                release_stamp=stamp,
            )
    rid, rung, upstream = found[0]
    bridge = Bridge(provider_id=pid, reep_id=rid, rung=rung, upstream_status=upstream)
    state = states[rid]
    if state.status is IdStatus.WITHDRAWN:
        return Resolution(
            query=pid,
            status=ResolutionStatus.WITHDRAWN,
            bridge=bridge,
            withdrawn_reason=state.reason,
            entity_type=state.entity_type,
            release_stamp=stamp,
        )
    if state.status is IdStatus.REDIRECTED:
        return Resolution(
            query=pid,
            status=ResolutionStatus.FOUND,
            reep_id=state.current_id,
            entity_type=state.entity_type,
            label=state.label,
            entity_status=state.current_status,
            bridge=bridge,
            redirected_from=rid,
            release_stamp=stamp,
        )
    return Resolution(
        query=pid,
        status=ResolutionStatus.FOUND,
        reep_id=rid,
        entity_type=state.entity_type,
        label=state.label,
        entity_status=state.current_status,
        bridge=bridge,
        release_stamp=stamp,
    )
