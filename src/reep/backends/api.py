"""The API backend: lookups through the hosted Reep API (``/api/v1``).

The API needs a key. Every lookup costs one credit per item; ``/meta``,
``/providers``, ``/key`` and ``/changes`` cost nothing.

Differences from the local backend:

* bridges carry no ``rung`` or ``upstream_status``;
* an entity lists only the bridges that name its own ID, not the bridges that still
  name an ID merged into it;
* a withdrawn ID that the release removed (``de_corroborated``, ``withheld``) reads
  as ``UNKNOWN``, and a withdrawn state carries a reason only for a redirect chain
  without a current ID (``redirect_loop``, ``target_not_published``);
* search returns at most 20 results and no entity status;
* ``providers`` reports no namespaces.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from reep._version import USER_AGENT
from reep.backends import _matches
from reep.backends.base import resolution_from_states
from reep.config import DEFAULT_API_URL
from reep.errors import (
    ApiError,
    AuthError,
    InvalidArgumentError,
    InvalidReepIdError,
    NetworkError,
    NotFoundError,
    QuotaExceededError,
    RateLimitError,
    UnknownProviderError,
)
from reep.models import (
    ENTITY_TYPE_BY_LETTER,
    Bridge,
    Entity,
    EntityMatch,
    IdState,
    IdStatus,
    ProviderId,
    ProviderNamespace,
    ReleaseInfo,
    Resolution,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = ["MAX_ENTITY_BATCH", "MAX_RESOLVE_BATCH", "MAX_SEARCH_RESULTS", "ApiBackend"]

logger = logging.getLogger(__name__)

#: The API's limit of resolve items per ``/batch`` call.
MAX_RESOLVE_BATCH = 20
#: The API's limit of entity items per ``/batch`` call.
MAX_ENTITY_BATCH = 100
#: The most results that ``/search`` returns.
MAX_SEARCH_RESULTS = 20

_RETRY_STATUSES = frozenset({429, 502, 503, 504})
# The API follows one redirect per request; the backend follows the rest.
_MAX_REDIRECT_HOPS = 8
# Entity states that end a redirect chain; any other state has no current ID.
_LIVE = frozenset({"active", "retired"})
_ARGUMENT_ERRORS = frozenset(
    {
        "invalid_search_query",
        "search_query_too_long",
        "unknown_entity_type",
        "invalid_sort",
        "unknown_include",
        "invalid_include",
    }
)
_KEY_HINT = (
    "Set REEP_API_KEY, or run 'reep auth login'. Request a key at "
    "https://reep.football/api."
)


class ApiBackend:
    """Answer lookups through the hosted API.

    Args:
        key: The API key.
        base_url: The API base URL.
        http: An ``httpx.Client`` to use instead of a new one.
        timeout: The request timeout in seconds.
        retries: How many times to retry a rate-limited or failed request.
        backoff: The first retry delay in seconds; each retry doubles it.
        sleep: The function that waits between retries.
        expiry_warning_days: Log a warning when the key expires within this many
            days.
    """

    name = "api"

    def __init__(
        self,
        key: str,
        *,
        base_url: str = DEFAULT_API_URL,
        http: httpx.Client | None = None,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        expiry_warning_days: int = 7,
    ) -> None:
        """Create the backend."""
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {key}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        self._owns_http = http is None
        self._http = http or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=15.0), follow_redirects=True
        )
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep
        self._expiry_warning_days = expiry_warning_days
        self._warned = False
        self._etags: dict[str, tuple[str, Any]] = {}
        self._api_metadata: dict[str, str | None] = {}
        self.release_stamp: str | None = None
        self.key_expires_at: str | None = None
        self.key_days_remaining: int | None = None
        self.credits_remaining: int | None = None
        self.ratelimit_remaining: int | None = None
        self.ratelimit_reset: int | None = None
        self.plan: str | None = None

    @property
    def last_call_meta(self) -> dict[str, Any]:
        """Return the telemetry headers seen on the latest HTTP response."""
        return {
            "release_stamp": self.release_stamp,
            "key_expires_at": self.key_expires_at,
            "key_days_remaining": self.key_days_remaining,
            "credits_remaining": self.credits_remaining,
            "ratelimit_remaining": self.ratelimit_remaining,
            "ratelimit_reset": self.ratelimit_reset,
            "plan": self.plan,
            "api_metadata": dict(self._api_metadata),
        }

    def close(self) -> None:
        """Close the HTTP client if this object created it."""
        if self._owns_http:
            self._http.close()

    # -- metadata ------------------------------------------------------------

    def release_info(self) -> ReleaseInfo:
        """Return the release that the API serves."""
        body = self._get_json("/meta")
        raw = body.get("release")
        release: dict[str, Any] = raw if isinstance(raw, dict) else {}
        stamp = release.get("stamp") or self.release_stamp
        schema = release.get("schema_version") or body.get("api_schema")
        generated = release.get("generated_at")
        return ReleaseInfo(
            stamp=stamp if isinstance(stamp, str) else None,
            schema_version=schema if isinstance(schema, str) else None,
            backend=self.name,
            source="api",
            generated_at=generated if isinstance(generated, str) else None,
            extra={"api_url": self.base_url},
        )

    def providers(self) -> list[ProviderNamespace]:
        """Return the providers with their bridge counts and namespaces."""
        body = self._get_json("/providers")
        out: list[ProviderNamespace] = []
        for item in body.get("providers") or []:
            if not isinstance(item, dict) or item.get("role") == "overlay_only":
                continue
            prov = str(item.get("name"))
            etypes = tuple(sorted(item.get("entity_types") or ()))
            namespaces = item.get("namespaces")
            if isinstance(namespaces, dict) and namespaces:
                for ns, ns_info in namespaces.items():
                    if not isinstance(ns, str) or not ns:
                        continue
                    ns_count = (
                        int(ns_info.get("bridge_count") or 0)
                        if isinstance(ns_info, dict)
                        else int(ns_info or 0)
                    )
                    ns_etypes = (
                        tuple(sorted(ns_info.get("entity_types") or ()))
                        if isinstance(ns_info, dict) and ns_info.get("entity_types")
                        else etypes
                    )
                    out.append(
                        ProviderNamespace(
                            provider=prov,
                            namespace=ns,
                            bridge_count=ns_count,
                            entity_types=ns_etypes,
                        )
                    )
            else:
                out.append(
                    ProviderNamespace(
                        provider=prov,
                        namespace=None,
                        bridge_count=int(item.get("bridge_count") or 0),
                        entity_types=etypes,
                    )
                )
        return sorted(out, key=lambda p: (p.provider, p.namespace or ""))

    def key_status(self) -> dict[str, Any]:
        """Return the key's plan, expiry and remaining quota (``GET /key``)."""
        return self._get_json("/key")

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
        """Return one page of changelog events (``GET /changes``).

        Args:
            since: Return changes after this release stamp.
            to: Return changes up to this release stamp.
            event_type: Return only this event type.
            provider: Return only events that touch this provider.
            limit: The page size.
            cursor: The cursor from the previous page.

        Returns:
            The response body.
        """
        params = {
            "since": since,
            "to": to,
            "type": event_type,
            "provider": provider,
            "limit": str(limit) if limit is not None else None,
            "cursor": cursor,
        }
        return self._get_json("/changes", {k: v for k, v in params.items() if v})

    def releases(self) -> dict[str, Any]:
        """Return the release chain (``GET /changes/releases``), cached by ETag."""
        return self._get_json("/changes/releases", etag=True)

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
        """Return up to ``limit`` fixtures, skipping ``offset`` on one API generation.

        Offset traversal can make several requests. If the API advances while
        paging, raise ``ApiError`` rather than combine generations or restart.
        Use :meth:`matches_page` to retain unresolved anchors and page metadata.
        """
        _matches.positive_int(limit, "limit")
        _matches.positive_int(offset, "offset", zero=True)
        params = _matches.parameters(
            season_id=season_id,
            competition_id=competition_id,
            team_id=team_id,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            venue_id=venue_id,
            date=date,
            date_from=date_from,
            date_to=date_to,
            limit=min(100, limit + offset),
            cursor=None,
        )
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        identity: tuple[object, ...] | None = None
        skipped = 0
        while True:
            page = self._matches_page(params)
            meta = page["api_metadata"]
            # These are stable release/source fields from this response,
            # not mutable credit/rate-limit telemetry.
            current = (page["generation"], tuple(sorted(meta.items())))
            if identity is not None and identity != current:
                raise _matches.contract_error("API identity changed while paging")
            identity = current
            for row in page["matches"]:
                if row["reep_id"] in seen_ids:
                    raise _matches.contract_error("duplicate match across pages")
                seen_ids.add(row["reep_id"])
                if skipped < offset:
                    skipped += 1
                elif len(rows) < limit:
                    rows.append(row)
            cursor = page["next_cursor"]
            if cursor is not None:
                if cursor in seen_cursors:
                    raise _matches.contract_error("repeated cursor")
                seen_cursors.add(cursor)
            if len(rows) == limit or cursor is None:
                return rows
            params["cursor"] = cursor

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
        """Return one validated API page with its cursor and generation.

        The result retains ``matches``, ``count``, ``generation``, ``unresolved``
        and ``next_cursor``. ``api_metadata`` is a snapshot of this response's
        release, selection and source headers, with absent values set to None.
        These describe the API channel, independently of downloadable snapshots.
        Repeat exactly the same filters and limit when continuing a cursor.
        A stale cursor raises ``ApiError`` with ``api_code='stale_cursor'``.

        Existing SDK names are translated: ``date`` is an exact day,
        ``date_from``/``date_to`` are inclusive, and home/away IDs select sides.
        ``venue_id`` is unsupported by the remote API and raises explicitly.
        """
        params = _matches.parameters(
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
        return self._matches_page(params)

    def _matches_page(self, params: dict[str, str]) -> dict[str, Any]:
        response = self._request("GET", "/matches", params=params)
        page = _matches.validate_page(self._json(response), int(params["limit"]))
        if params.get("cursor") is not None and page["unresolved"]:
            raise _matches.contract_error(
                "resolved anchors became unresolved on continuation"
            )
        metadata = _match_metadata(response)
        if metadata["channel"] not in (None, "api"):
            raise _matches.contract_error("response is not from the API channel")
        return {**page, "api_metadata": metadata}

    def notices(
        self,
        *,
        provider: str,
        status: str = "open",
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return partner bridge-defect notices (``GET /notices``)."""
        params = {
            "provider": provider,
            "status": status,
            "limit": str(limit) if limit is not None else None,
            "cursor": cursor,
        }
        return self._get_json("/notices", {k: v for k, v in params.items() if v})

    def acknowledge_notices(
        self,
        *,
        provider: str,
        ids: Sequence[str],
        disposition: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Acknowledge partner bridge-defect notices (``POST /notices/acknowledge``)."""
        payload: dict[str, Any] = {
            "provider": provider,
            "ids": list(ids),
            "disposition": disposition,
        }
        if note is not None:
            payload["note"] = note
        response = self._request("POST", "/notices/acknowledge", json_body=payload)
        return self._json(response)

    # -- lookups -------------------------------------------------------------

    def resolve_many(self, pids: Sequence[ProviderId]) -> list[Resolution]:
        """Resolve provider IDs through ``POST /batch``, 20 at a time.

        Args:
            pids: The provider IDs.

        Returns:
            One resolution per provider ID, in the order given.
        """
        found: list[list[dict[str, Any]]] = []
        for start in range(0, len(pids), MAX_RESOLVE_BATCH):
            chunk = pids[start : start + MAX_RESOLVE_BATCH]
            items = self._batch(
                [
                    {
                        "type": "resolve",
                        "provider": p.provider,
                        "namespace": p.namespace,
                        "external_id": p.external_id,
                    }
                    for p in chunk
                ]
            )
            for status, body in items:
                if status == 200 or (
                    status == 409 and body.get("error") == "ambiguous_provider_id"
                ):
                    found.append(_bridge_candidates(body))
                else:
                    raise _error_from(status, body)
        # Follow each merged candidate from its own ID, so the chain has the same
        # length limit as in the local backend.
        merged = sorted(
            {
                str(env.get("reep_id"))
                for candidates in found
                for env in candidates
                if env.get("lifecycle_state") == "redirected"
            }
        )
        merged_states = (
            dict(zip(merged, self.current_many(merged), strict=True)) if merged else {}
        )
        return [
            self._resolution(pid, candidates, merged_states)
            for pid, candidates in zip(pids, found, strict=True)
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
        results = self.resolve_many(
            [ProviderId(provider, namespace, x) for x in external_ids]
        )
        return [(r.reep_id, r.status.value) for r in results]

    def current_many(self, reep_ids: Sequence[str]) -> list[IdState]:
        """Return the state of stored Reep IDs through ``POST /batch``.

        Args:
            reep_ids: Valid Reep IDs.

        Returns:
            One state per ID, in the order given.
        """
        responses = self._entity_responses(reep_ids)
        return [self._state(rid, responses) for rid in reep_ids]

    def _entity_responses(
        self, reep_ids: Sequence[str]
    ) -> dict[str, tuple[int, dict[str, Any]]]:
        """Fetch entity items, and the items of each redirect target that merged.

        The API answers a merged ID with its target's entity, one step only. When
        that target merged too, the backend asks for it as well, up to
        ``_MAX_REDIRECT_HOPS`` steps.
        """
        responses: dict[str, tuple[int, dict[str, Any]]] = {}
        pending = list(dict.fromkeys(reep_ids))
        for _ in range(_MAX_REDIRECT_HOPS + 1):
            if not pending:
                break
            for start in range(0, len(pending), MAX_ENTITY_BATCH):
                chunk = pending[start : start + MAX_ENTITY_BATCH]
                items = self._batch([{"type": "entity", "id": rid} for rid in chunk])
                responses.update(zip(chunk, items, strict=True))
            following: list[str] = []
            for rid in pending:
                status, body = responses[rid]
                target = body.get("redirected_to") if status == 200 else None
                if target and body.get("lifecycle_state") == "redirected":
                    nxt = str(target)
                    if nxt not in responses and nxt not in following:
                        following.append(nxt)
            pending = following
        return responses

    def entity(self, reep_id: str, *, follow_redirects: bool = True) -> Entity | None:
        """Return an entity with its bridges and aliases (``GET /entities/{id}``).

        Args:
            reep_id: A valid Reep ID.
            follow_redirects: Return the survivor when the ID merged into another.

        Returns:
            The entity, or ``None`` if the API does not have it.
        """
        response = self._request(
            "GET",
            f"/entities/{reep_id}",
            params={"include": "aliases,bridges"},
        )
        if response.status_code == 404:
            return None
        body = self._json(response)
        redirected = bool(body.get("redirected_to") or body.get("redirected_from"))
        if redirected and not follow_redirects:
            return Entity(
                reep_id=reep_id,
                entity_type=ENTITY_TYPE_BY_LETTER[reep_id[1]],
                status="redirected",
                release_stamp=self.release_stamp,
            )
        redirected_from: str | None = reep_id if redirected else None
        if redirected and body.get("lifecycle_state") not in _LIVE:
            # Multi-hop redirect chain or broken target; walk remaining hops.
            state = self.current_many([reep_id])[0]
            if state.status is IdStatus.UNKNOWN:
                return None
            if state.status is not IdStatus.REDIRECTED or not state.current_id:
                return Entity(
                    reep_id=reep_id,
                    entity_type=ENTITY_TYPE_BY_LETTER[reep_id[1]],
                    status="redirected",
                    release_stamp=self.release_stamp,
                )
            response = self._request(
                "GET",
                f"/entities/{state.current_id}",
                params={"include": "aliases,bridges"},
            )
            if response.status_code == 404:
                return None
            body = self._json(response)
        return self._entity_from_body(body, redirected_from=redirected_from)

    def _entity_from_body(
        self, body: dict[str, Any], *, redirected_from: str | None = None
    ) -> Entity:
        target = str(body.get("reep_id"))
        bridges = sorted(
            (
                Bridge(
                    provider_id=ProviderId(
                        str(b.get("provider")),
                        str(b.get("namespace") or ""),
                        str(b.get("external_id")),
                    ),
                    reep_id=str(b.get("reep_id") or target),
                    rung=str(b.get("rung")) if b.get("rung") else None,
                    upstream_status=(
                        str(b.get("upstream_status"))
                        if b.get("upstream_status")
                        else None
                    ),
                )
                for b in body.get("bridges") or []
                if isinstance(b, dict)
                and b.get("namespace")
                and b.get("role") != "overlay_only"
            ),
            key=lambda b: (
                b.provider_id.provider,
                b.provider_id.namespace,
                b.provider_id.external_id,
            ),
        )
        country = body.get("country")
        gender = body.get("gender")
        grade = body.get("corroboration_grade")
        count = body.get("corroboration_count")
        return Entity(
            reep_id=target,
            entity_type=str(body.get("entity_type")),
            status=str(body.get("lifecycle_state")),
            label=body.get("name_en") or None,
            gender=gender if isinstance(gender, str) and gender else None,
            country=country if isinstance(country, str) and country else None,
            corroboration_grade=grade if isinstance(grade, str) and grade else None,
            corroboration_count=int(count) if isinstance(count, int) else None,
            bridges=tuple(bridges),
            aliases=_alias_names(body.get("aliases")),
            redirected_from=redirected_from,
            release_stamp=self.release_stamp,
        )

    def entities_many(
        self, reep_ids: Sequence[str], *, follow_redirects: bool = True
    ) -> dict[str, Entity]:
        """Return many entities with bridges and aliases via ``POST /batch``."""
        if not reep_ids:
            return {}
        distinct = list(dict.fromkeys(reep_ids))
        raw_responses: dict[str, tuple[int, dict[str, Any]]] = {}
        for start in range(0, len(distinct), MAX_ENTITY_BATCH):
            chunk = distinct[start : start + MAX_ENTITY_BATCH]
            items = self._batch(
                [{"type": "entity", "id": rid} for rid in chunk],
                include=("aliases", "bridges"),
            )
            raw_responses.update(zip(chunk, items, strict=True))
        multi_hop = [
            rid
            for rid, (status, body) in raw_responses.items()
            if status == 200
            and follow_redirects
            and (body.get("redirected_to") or body.get("redirected_from"))
            and body.get("lifecycle_state") not in _LIVE
        ]
        resolved_states = (
            dict(zip(multi_hop, self.current_many(multi_hop), strict=True))
            if multi_hop
            else {}
        )
        survivors_to_fetch = sorted(
            {
                st.current_id
                for st in resolved_states.values()
                if st.status is IdStatus.REDIRECTED
                and st.current_id
                and st.current_id not in raw_responses
            }
        )
        for start in range(0, len(survivors_to_fetch), MAX_ENTITY_BATCH):
            chunk = survivors_to_fetch[start : start + MAX_ENTITY_BATCH]
            items = self._batch(
                [{"type": "entity", "id": rid} for rid in chunk],
                include=("aliases", "bridges"),
            )
            raw_responses.update(zip(chunk, items, strict=True))
        entities_by_id: dict[str, Entity] = {}
        for rid in distinct:
            status, body = raw_responses[rid]
            if status == 404:
                continue
            if status != 200:
                raise _error_from(status, body)
            redirected = bool(body.get("redirected_to") or body.get("redirected_from"))
            if redirected and not follow_redirects:
                entities_by_id[rid] = Entity(
                    reep_id=rid,
                    entity_type=ENTITY_TYPE_BY_LETTER[rid[1]],
                    status="redirected",
                    release_stamp=self.release_stamp,
                )
                continue
            if redirected and body.get("lifecycle_state") not in _LIVE:
                st = resolved_states[rid]
                if st.status is IdStatus.UNKNOWN:
                    continue
                if st.status is not IdStatus.REDIRECTED or not st.current_id:
                    entities_by_id[rid] = Entity(
                        reep_id=rid,
                        entity_type=ENTITY_TYPE_BY_LETTER[rid[1]],
                        status="redirected",
                        release_stamp=self.release_stamp,
                    )
                    continue
                s_status, s_body = raw_responses.get(st.current_id, (404, {}))
                if s_status == 404:
                    continue
                if s_status != 200:
                    raise _error_from(s_status, s_body)
                entities_by_id[rid] = self._entity_from_body(
                    s_body, redirected_from=rid
                )
                continue
            entities_by_id[rid] = self._entity_from_body(
                body, redirected_from=rid if redirected else None
            )
        return entities_by_id

    def bridges_many(
        self,
        reep_ids: Sequence[str],
        provider: str,
        namespace: str | None = None,
    ) -> dict[str, list[Bridge]]:
        """Return bridges for many entities in batched calls."""
        if not reep_ids:
            return {}
        distinct = list(dict.fromkeys(reep_ids))
        entities = self.entities_many(distinct, follow_redirects=False)
        out: dict[str, list[Bridge]] = {}
        for rid in distinct:
            ent = entities.get(rid)
            if ent is None:
                out[rid] = []
                continue
            out[rid] = [
                b
                for b in ent.bridges
                if b.provider_id.provider == provider
                and (namespace is None or b.provider_id.namespace == namespace)
            ]
        return out

    def search(
        self,
        query: str,
        *,
        entity_type: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[EntityMatch]:
        """Search entities by name (``GET /search``; at most 20 results).

        Args:
            query: The name to search for (at least 3 characters for the API).
            entity_type: Return only this entity type.
            limit: The maximum number of results.
            offset: The number of results to skip.

        Returns:
            The matches, best first.
        """
        wanted = min(limit + offset, MAX_SEARCH_RESULTS)
        params = {"q": query, "limit": str(wanted)}
        if entity_type:
            params["type"] = entity_type
        body = self._get_json("/search", params)
        matches = [
            EntityMatch(
                reep_id=str(item.get("reep_id")),
                entity_type=str(item.get("entity_type")),
                label=item.get("label") or None,
                status=(
                    str(item.get("status"))
                    if isinstance(item.get("status"), str) and item.get("status")
                    else None
                ),
                gender=(
                    str(item.get("gender"))
                    if isinstance(item.get("gender"), str) and item.get("gender")
                    else None
                ),
                country=(
                    str(item.get("country"))
                    if isinstance(item.get("country"), str) and item.get("country")
                    else None
                ),
                bridge_count=int(item.get("bridge_count") or 0),
                rank=min(int(item.get("relevance") or 0), 3),
            )
            for item in body.get("results") or []
            if isinstance(item, dict)
        ]
        return matches[offset : offset + limit]

    # -- mapping -------------------------------------------------------------

    def _resolution(
        self,
        pid: ProviderId,
        candidates: list[dict[str, Any]],
        merged_states: dict[str, IdState],
    ) -> Resolution:
        found: list[tuple[str, str | None, str | None]] = []
        states: dict[str, IdState] = {}
        for env in candidates:
            origin = str(env.get("redirected_from") or env.get("reep_id"))
            if origin in states:
                continue
            states[origin] = self._candidate_state(origin, env, merged_states)
            found.append((origin, None, None))
        return resolution_from_states(pid, found, states, self.release_stamp)

    def _candidate_state(
        self, rid: str, env: dict[str, Any], merged_states: dict[str, IdState]
    ) -> IdState:
        """Return the state of a resolve candidate, after its redirects."""
        stamp = self.release_stamp
        lifecycle = env.get("lifecycle_state")
        target = str(env.get("reep_id"))
        etype = env.get("entity_type") or ENTITY_TYPE_BY_LETTER.get(rid[1])
        if (
            env.get("redirected_from")
            and isinstance(lifecycle, str)
            and lifecycle in _LIVE
        ):
            return IdState(
                reep_id=rid,
                status=IdStatus.REDIRECTED,
                current_id=target,
                current_status=str(lifecycle),
                reason="merged",
                entity_type=etype,
                label=env.get("name_en") or None,
                release_stamp=stamp,
            )
        if lifecycle != "redirected":
            if not isinstance(lifecycle, str) or lifecycle not in _LIVE:
                return IdState(
                    reep_id=rid,
                    status=IdStatus.WITHDRAWN,
                    reason=env.get("reason") or "target_not_published",
                    entity_type=etype,
                    release_stamp=stamp,
                )
            return IdState(
                reep_id=rid,
                status=IdStatus.RETIRED if lifecycle == "retired" else IdStatus.ACTIVE,
                current_id=rid,
                current_status=str(lifecycle) if lifecycle else None,
                entity_type=etype,
                label=env.get("name_en") or None,
                release_stamp=stamp,
            )
        state = merged_states.get(rid)
        if state is not None and state.status is IdStatus.REDIRECTED:
            return state
        reason = state.reason if state is not None else None
        if state is None or state.status is IdStatus.UNKNOWN:
            # The resolve index names the ID, but the API has no entity for its
            # target: the release does not publish it.
            reason = "target_not_published"
        return IdState(
            reep_id=rid,
            status=IdStatus.WITHDRAWN,
            reason=reason,
            entity_type=etype,
            release_stamp=stamp,
        )

    def _state(
        self, rid: str, responses: dict[str, tuple[int, dict[str, Any]]]
    ) -> IdState:
        stamp = self.release_stamp
        etype = ENTITY_TYPE_BY_LETTER.get(rid[1])
        status, body = responses[rid]
        if status == 404:
            return IdState(
                reep_id=rid,
                status=IdStatus.UNKNOWN,
                entity_type=etype,
                release_stamp=stamp,
            )
        if status != 200:
            raise _error_from(status, body)
        redirected_to = body.get("redirected_to")
        if not redirected_to:
            lifecycle = body.get("lifecycle_state")
            if not isinstance(lifecycle, str) or lifecycle not in _LIVE:
                # Only active or retired entities can end a resolution.
                return IdState(
                    reep_id=rid,
                    status=IdStatus.WITHDRAWN,
                    reason=body.get("reason") or "target_not_published",
                    entity_type=body.get("entity_type") or etype,
                    label=body.get("name_en") or None,
                    release_stamp=stamp,
                )
            return IdState(
                reep_id=rid,
                status=IdStatus.RETIRED if lifecycle == "retired" else IdStatus.ACTIVE,
                current_id=rid,
                current_status=str(lifecycle) if lifecycle else None,
                entity_type=body.get("entity_type") or etype,
                label=body.get("name_en") or None,
                release_stamp=stamp,
            )
        # ``body`` is the entity of ``current``; walk on while it merged too.
        seen = {rid}
        current = str(redirected_to)
        reason = "redirect_loop"
        for _ in range(_MAX_REDIRECT_HOPS):
            lifecycle = body.get("lifecycle_state")
            if isinstance(lifecycle, str) and lifecycle in _LIVE:
                return IdState(
                    reep_id=rid,
                    status=IdStatus.REDIRECTED,
                    current_id=current,
                    current_status=str(lifecycle),
                    reason="merged",
                    entity_type=body.get("entity_type") or etype,
                    label=body.get("name_en") or None,
                    release_stamp=stamp,
                )
            if lifecycle != "redirected" or current in seen:
                reason = "redirect_loop" if current in seen else "target_not_published"
                break
            seen.add(current)
            step = responses.get(current)
            if step is None:
                break  # longer than _MAX_REDIRECT_HOPS
            step_status, step_body = step
            if step_status not in (200, 404):
                raise _error_from(step_status, step_body)
            nxt = step_body.get("redirected_to") if step_status == 200 else None
            if not nxt:
                reason = "target_not_published"
                break
            current, body = str(nxt), step_body
        return IdState(
            reep_id=rid,
            status=IdStatus.WITHDRAWN,
            reason=reason,
            entity_type=etype,
            release_stamp=stamp,
        )

    # -- HTTP ----------------------------------------------------------------

    def _batch(
        self,
        requests: list[dict[str, Any]],
        *,
        include: Sequence[str] | None = None,
    ) -> list[tuple[int, dict[str, Any]]]:
        payload: dict[str, Any] = {"requests": requests}
        if include:
            payload["include"] = list(include)
        response = self._request("POST", "/batch", json_body=payload)
        body = self._json(response)
        results = body.get("results")
        if not isinstance(results, list) or len(results) != len(requests):
            raise ApiError(
                "The API returned an unexpected batch response.",
                status=response.status_code,
            )
        out: list[tuple[int, dict[str, Any]]] = []
        for item in results:
            status = int(item.get("status", 0)) if isinstance(item, dict) else 0
            payload_body = item.get("body") if isinstance(item, dict) else None
            out.append((status, payload_body if isinstance(payload_body, dict) else {}))
        return out

    def _get_json(
        self, path: str, params: dict[str, str] | None = None, *, etag: bool = False
    ) -> dict[str, Any]:
        response = self._request("GET", path, params=params, etag=etag)
        if response.status_code == 404:
            raise NotFoundError(f"The API has no {path}.")
        return self._json(response)

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise _error_from(response.status_code, _body(response))
        return _body(response)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        etag: bool = False,
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        headers = dict(self._headers)
        cached = self._etags.get(url) if etag else None
        if cached is not None:
            headers["If-None-Match"] = cached[0]
        attempt = 0
        while True:
            try:
                response = self._http.request(
                    method, url, params=params, json=json_body, headers=headers
                )
            except httpx.TransportError as exc:
                if attempt >= self._retries:
                    raise NetworkError(
                        f"{method} {path} failed: {exc}",
                        hint="Check the network connection and try again.",
                    ) from exc
                self._sleep(self._backoff * (2**attempt))
                attempt += 1
                continue
            self._note_headers(response)
            if response.status_code in _RETRY_STATUSES and attempt < self._retries:
                self._sleep(self._delay(response, attempt))
                attempt += 1
                continue
            if etag:
                return self._with_etag(url, response, cached)
            return response

    def _with_etag(
        self, url: str, response: httpx.Response, cached: tuple[str, Any] | None
    ) -> httpx.Response:
        if response.status_code == 304 and cached is not None:
            return httpx.Response(200, json=cached[1], request=response.request)
        tag = response.headers.get("ETag")
        if response.status_code == 200 and tag:
            self._etags[url] = (tag, _body(response))
        return response

    def _delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return min(float(retry_after), 60.0)
        reset = response.headers.get("X-Reep-Ratelimit-Reset", "")
        if reset.isdigit():
            wait = int(reset) / 1000.0 - time.time()
            if 0 < wait <= 60:
                return wait
        return float(self._backoff * (2**attempt))

    def _note_headers(self, response: httpx.Response) -> None:
        # Each page carries only its own headers, never stale values from a
        # previous generation or another channel.
        self._api_metadata = _match_metadata(response)
        stamp = response.headers.get("X-Reep-Release-Stamp")
        if stamp:
            self.release_stamp = stamp
        expires = response.headers.get("X-Reep-Key-Expires")
        if expires:
            self.key_expires_at = expires
        plan = response.headers.get("X-Reep-Plan")
        if plan:
            self.plan = plan
        credits = response.headers.get("X-Reep-Credits-Remaining", "")
        if credits.lstrip("-").isdigit():
            self.credits_remaining = int(credits)
        rl_rem = response.headers.get("X-Reep-Ratelimit-Remaining", "")
        if rl_rem.lstrip("-").isdigit():
            self.ratelimit_remaining = int(rl_rem)
        rl_reset = response.headers.get("X-Reep-Ratelimit-Reset", "")
        if rl_reset.isdigit():
            self.ratelimit_reset = int(rl_reset)
        days = response.headers.get("X-Reep-Key-Days-Remaining", "")
        if days.lstrip("-").isdigit():
            self.key_days_remaining = int(days)
            if (
                self.key_days_remaining <= self._expiry_warning_days
                and not self._warned
            ):
                self._warned = True
                logger.warning(
                    "The Reep API key expires in %s day(s) (%s). Email "
                    "getintouch@withqwerty.com to extend it.",
                    self.key_days_remaining,
                    expires or "unknown date",
                )


def _match_metadata(response: httpx.Response) -> dict[str, str | None]:
    """Snapshot only this response; backend telemetry cannot bind a page."""
    return {
        key: response.headers.get(header)
        for key, header in {
            "release_stamp": "X-Reep-Release-Stamp",
            "release_generated_at": "X-Reep-Release-Generated-At",
            "selection": "X-Reep-Selection",
            "channel": "X-Reep-Channel",
            "source_generation": "X-Reep-Source-Generation",
            "source_as_of": "X-Reep-Source-As-Of",
        }.items()
    }


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _bridge_candidates(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the bridge hits of a resolve body; Wikidata overlay hits are skipped."""
    out: list[dict[str, Any]] = []
    for env in body.get("results") or []:
        if not isinstance(env, dict):
            continue
        via = env.get("resolved_via")
        source = via.get("source", "bridge") if isinstance(via, dict) else "bridge"
        if source == "bridge":
            out.append(env)
    return out


def _alias_names(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    best: dict[str, float] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        rank = item.get("rank")
        value = float(rank) if isinstance(rank, (int, float)) else float("inf")
        name = item["name"]
        best[name] = min(best.get(name, float("inf")), value)
    return tuple(sorted(best, key=lambda n: (best[n], n)))


def _error_from(status: int, body: dict[str, Any]) -> Exception:
    code = body.get("error") if isinstance(body.get("error"), str) else None
    detail = body.get("message") or body.get("detail")
    message = str(detail) if detail else f"The API returned HTTP {status}."
    if status == 401:
        if code == "key_expired":
            return AuthError(
                "The Reep API key has expired.",
                hint="Email getintouch@withqwerty.com to extend it. The full register "
                "stays free to download: run 'reep catalog sync'.",
            )
        return AuthError("The Reep API key is missing or not valid.", hint=_KEY_HINT)
    if status == 402:
        return QuotaExceededError(
            "The Reep API key has used its quota.",
            hint="Use the local catalogue ('reep catalog sync') for bulk lookups.",
        )
    if status == 403:
        return AuthError(message, hint="This key cannot use this endpoint.")
    if status == 429:
        return RateLimitError(
            "The API rate limit stopped the request.", status=status, api_code=code
        )
    if status == 404:
        return NotFoundError(message)
    if status == 400 and code == "unknown_provider":
        providers = body.get("providers")
        hint = (
            f"Known providers: {', '.join(providers)}."
            if isinstance(providers, list)
            else "Run 'reep providers' to list the providers."
        )
        return UnknownProviderError(
            f"The API has no provider {body.get('provider')!r}.", hint=hint
        )
    if status == 400 and code == "invalid_reep_next_id":
        return InvalidReepIdError(f"{body.get('id')!r} is not a Reep ID.")
    if status == 400 and code in _ARGUMENT_ERRORS:
        return InvalidArgumentError(message)
    return ApiError(message, status=status, api_code=code)
