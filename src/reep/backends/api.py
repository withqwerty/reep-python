"""The API backend: lookups through the hosted Reep API (``/api/v1``).

The API needs a key. Every lookup costs one credit per item; ``/meta``,
``/providers``, ``/key`` and ``/changes`` cost nothing.

Differences from the local backend:

* bridges carry no ``rung`` or ``upstream_status``;
* a withdrawn ID that the release removed (``de_corroborated``, ``withheld``) reads
  as ``UNKNOWN``, and withdrawn states carry no reason;
* search returns at most 20 results and no entity status;
* ``providers`` reports no namespaces.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from reep._version import USER_AGENT
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
    ResolutionStatus,
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
        self.release_stamp: str | None = None
        self.key_expires_at: str | None = None
        self.key_days_remaining: int | None = None

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
        """Return the providers with their bridge counts; namespaces are ``None``."""
        body = self._get_json("/providers")
        out: list[ProviderNamespace] = []
        for item in body.get("providers") or []:
            if not isinstance(item, dict) or item.get("role") == "overlay_only":
                continue
            out.append(
                ProviderNamespace(
                    provider=str(item.get("name")),
                    namespace=None,
                    bridge_count=int(item.get("bridge_count") or 0),
                    entity_types=tuple(sorted(item.get("entity_types") or ())),
                )
            )
        return sorted(out, key=lambda p: p.provider)

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
                if status in (200, 409):
                    found.append(_bridge_candidates(body))
                else:
                    raise _error_from(status, body)
        survivors = {
            str(env["canonical_reep_id"])
            for candidates in found
            if len(candidates) == 1
            for env in candidates
            if env.get("lifecycle_state") == "redirected"
            and env.get("canonical_reep_id")
        }
        survivor_states = (
            dict(
                zip(
                    sorted(survivors), self.current_many(sorted(survivors)), strict=True
                )
            )
            if survivors
            else {}
        )
        return [
            self._resolution(pid, candidates, survivor_states)
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
        out: list[IdState] = []
        for start in range(0, len(reep_ids), MAX_ENTITY_BATCH):
            chunk = reep_ids[start : start + MAX_ENTITY_BATCH]
            items = self._batch([{"type": "entity", "id": rid} for rid in chunk])
            for rid, (status, body) in zip(chunk, items, strict=True):
                out.append(self._state(rid, status, body))
        return out

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
        redirected_to = body.get("redirected_to")
        if redirected_to and not follow_redirects:
            return Entity(
                reep_id=reep_id,
                entity_type=ENTITY_TYPE_BY_LETTER[reep_id[1]],
                status="redirected",
                release_stamp=self.release_stamp,
            )
        target = str(body.get("reep_id"))
        bridges = sorted(
            (
                Bridge(
                    provider_id=ProviderId(
                        str(b.get("provider")),
                        str(b.get("namespace") or ""),
                        str(b.get("external_id")),
                    ),
                    reep_id=target,
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
        return Entity(
            reep_id=target,
            entity_type=str(body.get("entity_type")),
            status=str(body.get("lifecycle_state")),
            label=body.get("name_en") or None,
            country=country if isinstance(country, str) and country else None,
            bridges=tuple(bridges),
            aliases=_alias_names(body.get("aliases")),
            redirected_from=reep_id if redirected_to else None,
            release_stamp=self.release_stamp,
        )

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
                status=None,
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
        survivor_states: dict[str, IdState],
    ) -> Resolution:
        stamp = self.release_stamp
        if not candidates:
            return Resolution(
                query=pid, status=ResolutionStatus.NOT_FOUND, release_stamp=stamp
            )
        ids = {str(env.get("reep_id")) for env in candidates}
        if len(ids) > 1:
            current = sorted(
                {
                    str(env.get("canonical_reep_id") or env.get("reep_id"))
                    for env in candidates
                }
            )
            if len(current) > 1:
                return Resolution(
                    query=pid,
                    status=ResolutionStatus.AMBIGUOUS,
                    candidates=tuple(current),
                    release_stamp=stamp,
                )
        env = candidates[0]
        rid = str(env.get("reep_id"))
        bridge = Bridge(provider_id=pid, reep_id=rid)
        lifecycle = env.get("lifecycle_state")
        if lifecycle == "redirected":
            survivor = env.get("canonical_reep_id")
            state = survivor_states.get(str(survivor)) if survivor else None
            if survivor and survivor != rid and state is not None and state.current_id:
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
                status=ResolutionStatus.WITHDRAWN,
                bridge=bridge,
                entity_type=env.get("entity_type"),
                release_stamp=stamp,
            )
        return Resolution(
            query=pid,
            status=ResolutionStatus.FOUND,
            reep_id=rid,
            entity_type=env.get("entity_type"),
            label=env.get("name_en") or None,
            entity_status=str(lifecycle) if lifecycle else None,
            bridge=bridge,
            release_stamp=stamp,
        )

    def _state(self, rid: str, status: int, body: dict[str, Any]) -> IdState:
        stamp = self.release_stamp
        etype = ENTITY_TYPE_BY_LETTER.get(rid[1])
        if status == 404:
            return IdState(
                reep_id=rid,
                status=IdStatus.UNKNOWN,
                entity_type=etype,
                release_stamp=stamp,
            )
        if status != 200:
            raise _error_from(status, body)
        lifecycle = body.get("lifecycle_state")
        label = body.get("name_en") or None
        redirected_to = body.get("redirected_to")
        if redirected_to:
            return IdState(
                reep_id=rid,
                status=IdStatus.REDIRECTED,
                current_id=str(redirected_to),
                current_status=str(lifecycle) if lifecycle else None,
                reason="merged",
                entity_type=body.get("entity_type") or etype,
                label=label,
                release_stamp=stamp,
            )
        if lifecycle == "redirected":
            return IdState(
                reep_id=rid,
                status=IdStatus.WITHDRAWN,
                entity_type=body.get("entity_type") or etype,
                label=label,
                release_stamp=stamp,
            )
        return IdState(
            reep_id=rid,
            status=IdStatus.RETIRED if lifecycle == "retired" else IdStatus.ACTIVE,
            current_id=rid,
            current_status=str(lifecycle) if lifecycle else None,
            entity_type=body.get("entity_type") or etype,
            label=label,
            release_stamp=stamp,
        )

    # -- HTTP ----------------------------------------------------------------

    def _batch(
        self, requests: list[dict[str, str]]
    ) -> list[tuple[int, dict[str, Any]]]:
        response = self._request("POST", "/batch", json_body={"requests": requests})
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
            payload = item.get("body") if isinstance(item, dict) else None
            out.append((status, payload if isinstance(payload, dict) else {}))
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
        stamp = response.headers.get("X-Reep-Release-Stamp")
        if stamp:
            self.release_stamp = stamp
        expires = response.headers.get("X-Reep-Key-Expires")
        if expires:
            self.key_expires_at = expires
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
