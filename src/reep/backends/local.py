"""The local backend: lookups against a DuckDB file.

The backend reads either the SDK's own catalogue or the DuckDB file that ships with
each release. Both use the same table layout. The backend opens the file read-only,
so several processes can read it at the same time.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import TYPE_CHECKING, Any

from reep import _store, _v4, _v4_store
from reep.backends.base import resolution_from_states
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
    entity_type_of,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import duckdb

__all__ = ["LocalBackend"]

logger = logging.getLogger(__name__)

_MAX_REDIRECT_HOPS = 8
# Entity statuses that end a redirect chain; any other status has no current ID.
_LIVE = frozenset({"active", "retired"})
_STRIP_ACCENTS_TABLE = str.maketrans(
    {
        "ß": "ss",
        "æ": "ae",
        "œ": "oe",
        "ø": "o",
        "ł": "l",
        "đ": "d",
        "ð": "d",
        "þ": "th",
        "\u0131": "i",
    }
)

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

_BRIDGES_FOR_IDS = """
WITH q AS (SELECT DISTINCT unnest($ids) AS reep_id)
SELECT b.reep_id, b.provider, b.namespace, b.external_id, nullif(b.rung, ''),
       nullif(b.upstream_status, '')
FROM q
JOIN bridges b ON b.reep_id = q.reep_id
WHERE b.provider = $provider AND ($namespace IS NULL OR b.namespace = $namespace)
ORDER BY b.reep_id, b.namespace, b.external_id
"""

_MERGED_INTO = """
WITH q AS (SELECT DISTINCT unnest($ids) AS reep_id)
SELECT r.from_id, r.to_id
FROM q
JOIN redirects r ON r.to_id = q.reep_id
WHERE r.reason = 'merged'
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
        self._v4 = self._meta.get(_store.META_SCHEMA) == _v4.SCHEMA
        if not self._v4 and "canonical_name" in _store.table_columns(
            self._con, "entities"
        ):
            try:
                self._bind_shipped_v4()
            except Exception:
                self._con.close()
                raise
        self._providers: list[ProviderNamespace] | None = None

    def _bind_shipped_v4(self) -> None:
        """Adapt an exact shipped layout in memory; never write to the release."""
        if self._tables != set(_v4.TABLES):
            raise UnsupportedReleaseError(
                "Shipped v4 table inventory differs from its contract."
            )
        for table in sorted(self._tables):
            _v4_store.validate_columns(table, _store.table_columns(self._con, table))
        self._con.close()
        import duckdb

        self._con = duckdb.connect(":memory:")
        path = str(self.path.resolve()).replace("'", "''")
        self._con.execute(f"ATTACH '{path}' AS downloaded (READ_ONLY)")
        for table in sorted(self._tables):
            self._con.execute(
                f"CREATE VIEW {table} AS "
                + _v4_store.projection(table, f"downloaded.main.{table}")
            )
        _v4_store.check_content(self._con)
        self._con.execute(
            "CREATE VIEW aliases AS " + _v4_store.aliases_query(self._tables)
        )
        self._tables.add("aliases")
        self._meta = {
            _store.META_SCHEMA: _v4.SCHEMA,
            "publication_identity": "unverified_local_file",
            "lifecycle": "not_published",
        }
        self._v4 = True

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
        if self._providers is None and self._v4:
            self._providers = [
                ProviderNamespace(str(p), str(n), int(count), tuple(types))
                for p, n, count, types in self._query(
                    "SELECT b.provider,b.namespace,count(*),"
                    "list_sort(list(DISTINCT e.entity_type)) "
                    "FROM bridges b JOIN entities e USING(reep_id) "
                    "GROUP BY b.provider,b.namespace ORDER BY b.provider,b.namespace"
                )
            ]
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
            resolution_from_states(pid, matches.get(i, []), states, stamp)
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
        stamp = self.stamp
        out: list[tuple[str | None, str]] = []
        for external_id in external_ids:
            found = [(rid, None, None) for rid in sorted(by_id.get(external_id, ()))]
            result = resolution_from_states(
                ProviderId(provider, namespace, external_id), found, states, stamp
            )
            out.append((result.reep_id, result.status.value))
        return out

    def bridges_many(
        self, reep_ids: Sequence[str], provider: str, namespace: str | None = None
    ) -> dict[str, list[Bridge]]:
        """Return the bridges of many entities at one provider.

        The bridges of an entity include the bridges that still name an ID merged
        into it; such a bridge's ``reep_id`` is the merged ID.

        Args:
            reep_ids: Valid Reep IDs of active or retired entities.
            provider: The provider slug.
            namespace: Return only this namespace; ``None`` for all.

        Returns:
            The bridges of each ID that has any, sorted by namespace and ID.
        """
        out: dict[str, list[Bridge]] = {}
        if not reep_ids:
            return out
        owner = {rid: rid for rid in reep_ids}
        owner.update(self._merged_into(reep_ids))
        rows = self._query(
            _BRIDGES_FOR_IDS,
            {"ids": sorted(owner), "provider": provider, "namespace": namespace},
        )
        for rid, prov, ns, external_id, rung, upstream in rows:
            out.setdefault(owner[str(rid)], []).append(
                Bridge(
                    provider_id=ProviderId(str(prov), str(ns), str(external_id)),
                    reep_id=str(rid),
                    rung=rung,
                    upstream_status=upstream,
                )
            )
        return {owner: _unique_bridges(found, owner) for owner, found in out.items()}

    def _merged_into(self, survivors: Sequence[str]) -> dict[str, str]:
        """Return each ID whose merge chain ends at one of ``survivors``.

        Args:
            survivors: Reep IDs of active or retired entities.

        Returns:
            ``{merged ID: survivor}``.
        """
        owner: dict[str, str] = {}
        done = set(survivors)
        frontier = {rid: rid for rid in survivors}
        for _ in range(_MAX_REDIRECT_HOPS):
            if not frontier:
                break
            following: dict[str, str] = {}
            query = (
                _MERGED_INTO.replace("r.reason = 'merged'", "r.to_id IS NOT NULL")
                if self._v4
                else _MERGED_INTO
            )
            for src, dst in self._query(query, {"ids": sorted(frontier)}):
                src = str(src)
                if src in done:
                    continue
                done.add(src)
                owner[src] = following[src] = frontier[str(dst)]
            frontier = following
        return owner

    def current_many(self, reep_ids: Sequence[str]) -> list[IdState]:
        """Return the state of stored Reep IDs, in the order given.

        Args:
            reep_ids: Valid Reep IDs.

        Returns:
            One state per ID.
        """
        states = self._states(set(reep_ids))
        return [states[rid] for rid in reep_ids]

    def entities_many(
        self, reep_ids: Sequence[str], *, follow_redirects: bool = True
    ) -> dict[str, Entity]:
        """Return many entities with their bridges and aliases in bulk.

        Args:
            reep_ids: Valid Reep IDs.
            follow_redirects: Return the survivor when an ID merged into another.

        Returns:
            ``{requested_reep_id: Entity}`` for each ID found in the store.
        """
        if not reep_ids:
            return {}
        unique_ids = list(dict.fromkeys(reep_ids))
        states = self._states(set(unique_ids))
        target_by_req: dict[str, str] = {}
        live_targets: set[str] = set()
        for rid in unique_ids:
            state = states[rid]
            target = rid
            if (
                follow_redirects
                and state.status is IdStatus.REDIRECTED
                and state.current_id
            ):
                target = state.current_id
            target_by_req[rid] = target
            if target_state(state, target) in _LIVE or (
                self._v4 and state.current_id == target
            ):
                live_targets.add(target)

        targets = sorted(set(target_by_req.values()))
        merged_owner = self._merged_into(sorted(live_targets)) if live_targets else {}
        bridge_sources = sorted(set(targets) | set(merged_owner))

        entity_rows = {
            str(row[0]): row
            for row in self._query(
                "SELECT reep_id, entity_type, status, nullif(label, ''), "
                "nullif(gender, ''), nullif(country, ''), "
                "nullif(corroboration_grade, ''), nullif(corroboration_count, '') "
                "FROM entities WHERE reep_id IN (SELECT unnest($ids))",
                {"ids": targets},
            )
        }
        if not entity_rows:
            return {}

        bridges_by_target: dict[str, list[Bridge]] = {t: [] for t in entity_rows}
        if bridge_sources:
            for owner_id, p, n, x, rung, upstream in self._query(
                "SELECT reep_id, provider, namespace, external_id, nullif(rung, ''), "
                "nullif(upstream_status, '') FROM bridges "
                "WHERE reep_id IN (SELECT unnest($ids)) "
                "ORDER BY provider, namespace, external_id, reep_id",
                {"ids": bridge_sources},
            ):
                owner_str = str(owner_id)
                bridge_obj = Bridge(
                    provider_id=ProviderId(str(p), str(n), str(x)),
                    reep_id=owner_str,
                    rung=rung,
                    upstream_status=upstream,
                )
                if owner_str in bridges_by_target:
                    bridges_by_target[owner_str].append(bridge_obj)
                survivor_str = merged_owner.get(owner_str)
                if survivor_str and survivor_str in bridges_by_target:
                    bridges_by_target[survivor_str].append(bridge_obj)

        aliases_by_target: dict[str, list[str]] = {t: [] for t in entity_rows}
        if "aliases" in self._tables:
            for tid, alias in self._query(
                "SELECT reep_id, alias FROM aliases "
                "WHERE reep_id IN (SELECT unnest($ids)) "
                "GROUP BY reep_id, alias "
                "ORDER BY reep_id, min(TRY_CAST(rank AS INTEGER)) NULLS LAST, alias",
                {"ids": sorted(entity_rows)},
            ):
                aliases_by_target[str(tid)].append(str(alias))

        stamp = self.stamp
        out: dict[str, Entity] = {}
        for req_id, target in target_by_req.items():
            row = entity_rows.get(target)
            if row is None:
                continue
            rid, etype, status, label, gender, country, grade, count = row
            deduped_bridges = tuple(
                _unique_bridges(bridges_by_target.get(target, []), target)
            )
            out[req_id] = Entity(
                reep_id=str(rid),
                entity_type=str(etype),
                status=status,
                label=label,
                gender=gender,
                country=country,
                corroboration_grade=grade,
                corroboration_count=int(count) if count is not None else None,
                bridges=deduped_bridges,
                aliases=tuple(aliases_by_target.get(target, ())),
                redirected_from=req_id if target != req_id else None,
                release_stamp=stamp,
            )
        return out

    def entity(self, reep_id: str, *, follow_redirects: bool = True) -> Entity | None:
        """Return an entity with its bridges and aliases.

        Args:
            reep_id: A valid Reep ID.
            follow_redirects: Return the survivor when the ID merged into another.

        Returns:
            The entity, or ``None`` if the store does not have it.
        """
        return self.entities_many([reep_id], follow_redirects=follow_redirects).get(
            reep_id
        )

    def matches(
        self,
        *,
        competition_id: str | None = None,
        season_id: str | None = None,
        team_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        status: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query matches from the local store (requires ``matches`` table).

        Args:
            competition_id: Filter by competition Reep ID.
            season_id: Filter by season Reep ID.
            team_id: Filter by home or away team Reep ID.
            date_from: Inclusive start date (``YYYY-MM-DD``).
            date_to: Inclusive end date (``YYYY-MM-DD``).
            status: Filter by lifecycle status (default ``"active"``).
            limit: Maximum number of matches to return.
            offset: Number of matches to skip.

        Returns:
            A list of match dictionaries.
        """
        if "matches" not in self._tables:
            raise UnsupportedReleaseError(
                "The local catalogue does not include the 'matches' table.",
                hint="Run 'reep catalog sync --profile full' or use a shipped "
                "reep-register-v1.duckdb file to query matches locally.",
            )
        if self._v4 and (status is not None or competition_id is not None):
            raise UnsupportedReleaseError(
                "v4 downloads do not publish match lifecycle or competition IDs; "
                "this filter is unavailable."
            )
        conditions = ["1=1"]
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status is not None:
            conditions.append("m.status = $status")
            params["status"] = status
        if competition_id is not None:
            conditions.append("m.competition_id = $comp")
            params["comp"] = competition_id
        if season_id is not None:
            conditions.append("m.season_id = $season")
            params["season"] = season_id
        if team_id is not None:
            conditions.append("(m.home_team_id = $team OR m.away_team_id = $team)")
            params["team"] = team_id
        if date_from is not None:
            conditions.append("m.kickoff_date >= $date_from")
            params["date_from"] = date_from
        if date_to is not None:
            conditions.append("m.kickoff_date <= $date_to")
            params["date_to"] = date_to
        sql = (
            "SELECT m.reep_id, m.status, nullif(m.kickoff_date, ''), "
            "nullif(m.competition_id, ''), nullif(m.season_id, ''), "
            "nullif(m.home_team_id, ''), nullif(m.away_team_id, ''), "
            "nullif(e.label, '') "
            f"FROM matches m LEFT JOIN entities e USING (reep_id) "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY m.kickoff_date, m.reep_id "
            "LIMIT $limit OFFSET $offset"
        )
        rows = self._query(sql, params)
        if self._v4:
            # Additional v4 facts retain integer zero versus missing values.
            fact_rows = self._query(
                "SELECT reep_id,home_score,away_score,"
                "match_state,result_form,score_basis "
                "FROM matches WHERE reep_id IN (SELECT unnest($ids))",
                {"ids": [r[0] for r in rows]},
            )
            facts = {
                str(r[0]): {
                    "home_score": int(r[1]) if r[1] is not None else None,
                    "away_score": int(r[2]) if r[2] is not None else None,
                    "match_state": r[3],
                    "result_form": r[4],
                    "score_basis": r[5],
                }
                for r in fact_rows
            }
        else:
            facts = {}
        return [
            {
                "reep_id": str(rid),
                "status": st,
                "kickoff_date": kdate,
                "competition_id": comp_id,
                "season_id": seas_id,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "label": label,
                **facts.get(str(rid), {}),
            }
            for rid, st, kdate, comp_id, seas_id, home_id, away_id, label in rows
        ]

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

    @staticmethod
    def _normalise(text: str) -> str:
        folded = text.lower().translate(_STRIP_ACCENTS_TABLE)
        decomposed = unicodedata.normalize("NFD", folded)
        stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
        return " ".join(stripped.split())

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
                if (self._v4 or reason == "merged") and to_id and to_id not in raw:
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
        try:
            etype_from_id = entity_type_of(rid)
        except ValueError:
            etype_from_id = None
        etype, status, label, to_id, reason = raw.get(
            rid, (None, None, None, None, None)
        )
        if (self._v4 or reason == "merged") and to_id:
            seen = {rid}
            current = to_id
            for _ in range(_MAX_REDIRECT_HOPS):
                c_type, c_status, c_label, c_to, c_reason = raw.get(
                    current, (None, None, None, None, None)
                )
                if (self._v4 or c_reason == "merged") and c_to:
                    if current in seen or c_to in seen:
                        break  # a loop: no ID in it is current
                    seen.add(current)
                    current = c_to
                    continue
                if (
                    not self._v4
                    and (c_reason in WITHDRAWN_REASONS or c_status not in _LIVE)
                ) or (self._v4 and c_type is None):
                    # Withdrawn, unpublished, or redirected with no survivor.
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
                    reason=reason if self._v4 else "merged",
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
        if self._v4 and etype is not None:
            return IdState(
                reep_id=rid,
                status=IdStatus.PUBLISHED,
                current_id=rid,
                current_status=None,
                entity_type=etype,
                label=label,
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


def _unique_bridges(found: list[Bridge], owner: str) -> list[Bridge]:
    """Return one bridge per provider ID, the entity's own bridge first, sorted.

    The same provider ID can be on an entity and on an ID merged into it.
    """
    chosen: dict[ProviderId, Bridge] = {}
    for b in sorted(found, key=lambda b: b.reep_id != owner):
        chosen.setdefault(b.provider_id, b)
    return sorted(
        chosen.values(),
        key=lambda b: (
            b.provider_id.provider,
            b.provider_id.namespace,
            b.provider_id.external_id,
        ),
    )


def target_state(state: IdState, target: str) -> str | None:
    """Return the entity status of ``target``, the ID that ``entity`` reads."""
    if state.current_id == target:
        return state.current_status
    return None
