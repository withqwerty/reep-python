"""A fake Reep API that serves the fixture release in the site's response shapes.

The shapes follow ``reep-site/src/lib/next-api.ts`` and ``public/openapi.yaml``:
``/meta``, ``/providers``, ``/key``, ``/resolve/{provider}/{external_id}``,
``/batch``, ``/entities/{reep_id}``, ``/search`` and ``/changes/releases``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import httpx

from tests.support.fixture_release import STAMP_1, ReleaseState, state_1

VALID_KEY = "test-key-0000000000"
EXPIRED_KEY = "expired-key-000000"
BASE = "https://api.test/api/v1"
MAX_BATCH = 100
MAX_BATCH_RESOLVE = 20
_REEP_ID = re.compile(r"^r[cglmopst][0-9a-f]{14}$")

# One Wikidata overlay hit that the SDK must not treat as a bridge.
OVERLAY_TRIPLE = ("fbref", "person", "overlay1")


def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.split())


@dataclass
class FakeApi:
    """The fake API's state and request log."""

    state: ReleaseState = field(default_factory=state_1)
    stamp: str = STAMP_1
    days_remaining: int = 30
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_statuses: list[int] = field(default_factory=list)
    batch_sizes: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.redirects = {
            r["from_id"]: r["to_id"] for r in self.state.redirects if r["to_id"]
        }
        self.entities: dict[str, dict[str, Any]] = {}
        for e in self.state.entities:
            row: dict[str, Any] = {
                "reep_id": e["reep_id"],
                "entity_type": e["entity_type"],
                "name_en": e["label"],
                "lifecycle_state": e["status"],
                "canonical_reep_id": (
                    self.redirects.get(e["reep_id"])
                    if e["status"] == "redirected"
                    else e["reep_id"]
                ),
            }
            if e["country"]:
                row["country"] = e["country"]
            self.entities[e["reep_id"]] = row
        self.bridges: dict[str, list[dict[str, str]]] = {}
        self.index: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for b in self.state.bridges:
            etype = self.entities[b["reep_id"]]["entity_type"]
            self.bridges.setdefault(b["reep_id"], []).append(
                {
                    "provider": b["provider"],
                    "namespace": b["namespace"],
                    "external_id": b["external_id"],
                    "entity_type": etype,
                    "role": "canonical_bridge",
                }
            )
            key = (b["provider"], b["namespace"], b["external_id"])
            self.index.setdefault(key, []).append(
                {
                    "reep_id": b["reep_id"],
                    "entity_type": etype,
                    "namespace": b["namespace"],
                }
            )
        self.index[OVERLAY_TRIPLE] = [
            {
                "reep_id": "rp00000000000001",
                "entity_type": "player",
                "namespace": "person",
                "source": "overlay",
                "confidence": 0.8,
            }
        ]
        self.aliases: dict[str, list[dict[str, Any]]] = {}
        for a in self.state.aliases:
            self.aliases.setdefault(a["reep_id"], []).append(
                {
                    "name": a["alias"],
                    "language": a["language"] or None,
                    "type": a["kind"] or None,
                    "rank": int(a["rank"]) if a["rank"] else None,
                }
            )

    # -- transport -----------------------------------------------------------

    def client(self) -> httpx.Client:
        """Return an ``httpx.Client`` that sends requests to this fake."""
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        self.calls.append((request.method, path))
        if self.fail_statuses:
            status = self.fail_statuses.pop(0)
            headers = {"Retry-After": "1"} if status == 429 else {}
            return httpx.Response(status, json={"error": "fault"}, headers=headers)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return self._json({"error": "unauthorized", "message": "Pass a key."}, 401)
        key = auth.removeprefix("Bearer ")
        if key == EXPIRED_KEY:
            return self._json({"error": "key_expired", "detail": "expired"}, 401)
        if key != VALID_KEY:
            return self._json(
                {"error": "unauthorized", "detail": "Invalid or revoked API key."}, 401
            )
        response = self._route(request, path)
        response.headers["X-Reep-Release-Stamp"] = self.stamp
        response.headers["X-Reep-Key-Expires"] = "2026-11-12T00:00:00.000Z"
        response.headers["X-Reep-Key-Days-Remaining"] = str(self.days_remaining)
        return response

    def _json(self, body: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=body)

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        params = request.url.params
        parts = [p for p in path.split("/") if p]
        if request.method == "POST" and parts == ["batch"]:
            return self._batch(json.loads(request.content))
        if parts == ["meta"]:
            return self._json(
                {
                    "api_version": "1.0.0",
                    "namespace": "v1",
                    "release": {
                        "stamp": self.stamp,
                        "generated_at": "2026-01-01T00:00:00+00:00",
                        "schema_version": "bridge-register-v1",
                    },
                }
            )
        if parts == ["providers"]:
            return self._json(self._providers())
        if parts == ["key"]:
            return self._json(
                {
                    "key": {
                        "plan": "evaluation",
                        "expires_at": "2026-11-12T00:00:00.000Z",
                        "days_remaining": self.days_remaining,
                        "expiring_soon": self.days_remaining <= 7,
                        "credits_remaining": 9990,
                    },
                    "extend": {"how": "email", "contact": "getintouch@withqwerty.com"},
                }
            )
        if parts[:1] == ["resolve"] and len(parts) == 3:
            status, body = self._resolve(
                parts[1], parts[2], params.get("namespace", "")
            )
            return self._json(body, status)
        if parts[:1] == ["entities"] and len(parts) == 2:
            status, body = self._entity(parts[1], params.get("include", ""))
            return self._json(body, status)
        if parts == ["search"]:
            return self._search(params)
        if parts == ["changes", "releases"]:
            etag = 'W/"releases-1"'
            if request.headers.get("If-None-Match") == etag:
                return httpx.Response(304, headers={"ETag": etag})
            return httpx.Response(
                200,
                json={"latest": self.stamp, "releases": [{"stamp": self.stamp}]},
                headers={"ETag": etag},
            )
        if parts == ["changes"]:
            return self._json({"events": [], "count": 0, "next_cursor": None})
        return self._json({"error": "not_found"}, 404)

    # -- handlers (ported from next-api.ts) ----------------------------------

    def _providers(self) -> dict[str, Any]:
        rows: dict[str, dict[str, Any]] = {}
        for (provider, _ns, _x), candidates in self.index.items():
            if candidates and candidates[0].get("source") == "overlay":
                continue
            row = rows.setdefault(
                provider, {"name": provider, "bridge_count": 0, "entity_types": set()}
            )
            row["bridge_count"] += len(candidates)
            row["entity_types"].update(c["entity_type"] for c in candidates)
        providers = [
            {
                **r,
                "entity_types": sorted(r["entity_types"]),
                "overlay_count": 0,
                "role": "canonical_bridge",
            }
            for r in rows.values()
        ]
        providers.append(
            {
                "name": "wikidata",
                "bridge_count": 0,
                "overlay_count": 5,
                "entity_types": ["player"],
                "role": "overlay_only",
            }
        )
        return {"providers": providers, "count": len(providers)}

    def _envelope(self, reep_id: str, include: set[str]) -> dict[str, Any] | None:
        row = self.entities.get(reep_id)
        if row is None:
            return None
        payload = dict(row)
        if "bridges" in include:
            payload["bridges"] = self.bridges.get(reep_id, [])
        if "aliases" in include:
            aliases = self.aliases.get(reep_id, [])
            payload["aliases"] = aliases
            payload["names"] = [a["name"] for a in aliases]
        return payload

    def _resolve(
        self, provider: str, external_id: str, namespace: str
    ) -> tuple[int, dict[str, Any]]:
        if provider not in {k[0] for k in self.index}:
            return 400, {
                "error": "unknown_provider",
                "provider": provider,
                "providers": sorted({k[0] for k in self.index}),
            }
        candidates = self.index.get((provider, namespace, external_id), [])
        unique: dict[str, dict[str, Any]] = {}
        for c in sorted(candidates, key=lambda c: 1 if c.get("source") else 0):
            unique.setdefault(c["reep_id"], c)
        if not unique:
            return 200, {"results": [], "count": 0}
        hydrated = []
        for c in unique.values():
            env = self._envelope(c["reep_id"], set())
            if env is None:
                continue
            via: dict[str, Any] = {
                "provider": provider,
                "namespace": c.get("namespace", namespace),
                "external_id": external_id,
                "source": c.get("source", "bridge"),
            }
            if "confidence" in c:
                via["confidence"] = c["confidence"]
            hydrated.append({**env, "resolved_via": via})
        if len(unique) > 1:
            return 409, {
                "error": "ambiguous_provider_id",
                "results": hydrated,
                "count": len(unique),
            }
        return 200, {"results": hydrated, "count": len(hydrated)}

    def _entity(self, reep_id: str, include_raw: str) -> tuple[int, dict[str, Any]]:
        if not _REEP_ID.match(reep_id):
            return 400, {"error": "invalid_reep_next_id", "id": reep_id}
        include = {i for i in include_raw.split(",") if i}
        target = self.redirects.get(reep_id)
        payload = self._envelope(target or reep_id, include)
        if payload is None:
            return 404, {"error": "not_found", "id": reep_id}
        if target:
            payload["requested_id"] = reep_id
            payload["redirected_to"] = target
        return 200, payload

    def _batch(self, body: dict[str, Any]) -> httpx.Response:
        requests = body.get("requests")
        if not isinstance(requests, list) or not requests:
            return self._json({"error": "requests_required"}, 400)
        self.batch_sizes.append(len(requests))
        if len(requests) > MAX_BATCH:
            return self._json({"error": "batch_too_large", "limit": MAX_BATCH}, 400)
        resolves = sum(1 for r in requests if r.get("type") == "resolve")
        if resolves > MAX_BATCH_RESOLVE:
            return self._json({"error": "resolve_batch_too_large"}, 400)
        results = []
        for item in requests:
            if item.get("type") == "resolve":
                status, out = self._resolve(
                    item["provider"], item["external_id"], item.get("namespace", "")
                )
            elif item.get("type") == "entity":
                status, out = self._entity(item["id"], "")
            else:
                status, out = 400, {"error": "unknown_batch_item_type"}
            results.append({"status": status, "body": out})
        return self._json({"results": results, "count": len(results)})

    def _search(self, params: httpx.QueryParams) -> httpx.Response:
        query = _norm(params.get("q", ""))
        tokens = query.split()
        if len(query) < 3 or not tokens:
            return self._json(
                {
                    "error": "invalid_search_query",
                    "message": "q must contain at least 3 normalised characters.",
                },
                400,
            )
        if len(tokens) > 5:
            return self._json({"error": "search_query_too_long"}, 400)
        etype = params.get("type")
        limit = min(int(params.get("limit", "10")), 20)
        rows = []
        for rid, row in self.entities.items():
            names = [row["name_en"], *[a["name"] for a in self.aliases.get(rid, [])]]
            text = _norm(" ".join(names))
            if etype and row["entity_type"] != etype:
                continue
            if not all(t in text for t in tokens):
                continue
            label = _norm(row["name_en"])
            relevance = (
                0
                if label == query
                else 1
                if label.startswith(query)
                else 2
                if query in label
                else 3
            )
            rows.append(
                {
                    "reep_id": rid,
                    "entity_type": row["entity_type"],
                    "label": row["name_en"],
                    "bridge_count": len(self.bridges.get(rid, [])),
                    "relevance": relevance,
                }
            )
        rows.sort(key=lambda r: (r["relevance"], -r["bridge_count"], r["label"]))
        rows = rows[:limit]
        return self._json(
            {
                "query": params.get("q"),
                "results": rows,
                "count": len(rows),
                "limit": limit,
                "bounded": True,
            }
        )
