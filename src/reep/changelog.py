"""Read release changelogs and replay them onto a local catalogue.

Each release ships ``changelog.json.gz``: the typed difference from the release
before it (``prior.stamp``). The SDK replays the bridge events to update its
catalogue without a full download. Version 1 of the format has these event types:

``entity_added``, ``entity_removed``, ``entity_status_changed``,
``entity_type_changed``, ``label_updated``, ``redirect_changed``,
``provider_updated``, ``score_updated`` and ``fixture_updated``.

The changelog does not carry every column: bridge ``rung`` and ``upstream_status``,
the entity columns other than type, status and label, and the redirect rows without
a target (withdrawn IDs). The catalogue sync therefore downloads the ``entities``
and ``redirects`` tables again and replays only the bridges.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from reep.errors import ReplayError

if TYPE_CHECKING:
    from pathlib import Path

    import duckdb

__all__ = [
    "CHANGELOG_VERSION",
    "EVENT_TYPES",
    "Changelog",
    "apply_bridge_changes",
    "apply_staged_bridge_changes",
    "replay_redirects",
    "stage_bridge_changes",
]

#: The changelog format version that this version of reep reads.
CHANGELOG_VERSION = 1

#: The event types of changelog version 1.
EVENT_TYPES = frozenset(
    {
        "entity_added",
        "entity_removed",
        "entity_status_changed",
        "entity_type_changed",
        "label_updated",
        "redirect_changed",
        "provider_updated",
        "score_updated",
        "fixture_updated",
    }
)

_BRIDGE_FIELDS = ("provider", "namespace", "external_id")
_REDIRECT_EVENTS = frozenset(
    {"entity_added", "entity_status_changed", "redirect_changed"}
)

#: A bridge row: ``(provider, namespace, external_id, reep_id)``.
BridgeRow = tuple[str, str, str, str]


@dataclass(frozen=True, slots=True)
class Changelog:
    """The parts of one changelog that the replay uses.

    Attributes:
        stamp: The release that the changelog leads to.
        prior_stamp: The release that the changelog starts from, or ``None`` when
            the release has no prior (the first release, or a pruned history).
        counts: The number of events of each type.
        bridge_adds: Bridge rows that the release adds.
        bridge_removes: Bridge rows that the release removes.
        redirect_events: The events that change the merge redirects, in order.
    """

    stamp: str
    prior_stamp: str | None
    counts: dict[str, int]
    bridge_adds: list[BridgeRow] = field(default_factory=list[BridgeRow])
    bridge_removes: list[BridgeRow] = field(default_factory=list[BridgeRow])
    redirect_events: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    @classmethod
    def read(cls, path: Path) -> Changelog:
        """Read and check a ``changelog.json.gz`` file.

        Args:
            path: The file.

        Returns:
            The changelog.

        Raises:
            ReplayError: If the file is not a valid version 1 changelog.
        """
        try:
            with gzip.open(path, "rb") as fh:
                doc = json.loads(fh.read())
        except (OSError, ValueError) as exc:
            raise ReplayError(f"{path.name} cannot be read: {exc}") from exc
        return cls.from_json(doc)

    @classmethod
    def from_json(cls, doc: object) -> Changelog:
        """Check a parsed changelog document.

        Args:
            doc: The parsed JSON document.

        Returns:
            The changelog.

        Raises:
            ReplayError: If the document is not a valid version 1 changelog.
        """
        if not isinstance(doc, dict):
            raise ReplayError("The changelog is not a JSON object.")
        version = doc.get("changelog_version")
        if version != CHANGELOG_VERSION:
            raise ReplayError(
                f"The changelog has version {version!r}; this version of reep reads "
                f"version {CHANGELOG_VERSION}.",
                hint="Upgrade the reep package to replay newer changelogs.",
            )
        new = doc.get("new")
        stamp = new.get("stamp") if isinstance(new, dict) else None
        if not isinstance(stamp, str):
            raise ReplayError("The changelog has no new.stamp.")
        prior = doc.get("prior")
        if prior is None:
            return cls(stamp=stamp, prior_stamp=None, counts={})
        prior_stamp = prior.get("stamp") if isinstance(prior, dict) else None
        if not isinstance(prior_stamp, str):
            raise ReplayError("The changelog has no prior.stamp.")
        events = doc.get("events")
        if not isinstance(events, list):
            raise ReplayError("The changelog has no events list.")
        counts: dict[str, int] = {}
        adds: list[BridgeRow] = []
        removes: list[BridgeRow] = []
        redirect_events: list[dict[str, str]] = []
        seen_bridge_events: set[str] = set()
        for event in events:
            event_type, reep_id = _event_head(event)
            counts[event_type] = counts.get(event_type, 0) + 1
            if event_type == "provider_updated":
                if reep_id in seen_bridge_events:
                    raise ReplayError(
                        f"The changelog has two provider_updated events for {reep_id}."
                    )
                seen_bridge_events.add(reep_id)
                adds.extend(_bridge_rows(event, "added", reep_id))
                removes.extend(_bridge_rows(event, "removed", reep_id))
            elif event_type in _REDIRECT_EVENTS:
                redirect_events.append(_redirect_event(event))
        declared = doc.get("counts")
        if isinstance(declared, dict) and declared != counts:
            raise ReplayError("The changelog's counts do not match its events.")
        return cls(
            stamp=stamp,
            prior_stamp=prior_stamp,
            counts=counts,
            bridge_adds=adds,
            bridge_removes=removes,
            redirect_events=redirect_events,
        )


def _event_head(event: object) -> tuple[str, str]:
    if not isinstance(event, dict):
        raise ReplayError("A changelog event is not an object.")
    event_type = event.get("type")
    reep_id = event.get("reep_id")
    if event_type not in EVENT_TYPES:
        raise ReplayError(
            f"The changelog has an unknown event type {event_type!r}.",
            hint="Upgrade the reep package to replay newer changelogs.",
        )
    if not isinstance(reep_id, str):
        raise ReplayError(f"A {event_type} event has no reep_id.")
    return str(event_type), reep_id


def _bridge_rows(event: dict[str, Any], key: str, reep_id: str) -> list[BridgeRow]:
    rows = event.get(key)
    if not isinstance(rows, list):
        raise ReplayError(f"A provider_updated event has no {key} list.")
    out: list[BridgeRow] = []
    for row in rows:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(name), str) for name in _BRIDGE_FIELDS
        ):
            raise ReplayError(f"A provider_updated {key} entry is not valid.")
        out.append((row["provider"], row["namespace"], row["external_id"], reep_id))
    return out


def _redirect_event(event: dict[str, Any]) -> dict[str, str]:
    kept = {
        key: event[key]
        for key in ("type", "reep_id", "from", "to", "redirect_to")
        if key in event
    }
    if event["type"] == "redirect_changed" and not (
        isinstance(kept.get("from"), str) and isinstance(kept.get("to"), str)
    ):
        raise ReplayError("A redirect_changed event lacks from or to.")
    return {k: str(v) for k, v in kept.items()}


def replay_redirects(
    prior: dict[str, str], events: list[dict[str, str]]
) -> dict[str, str]:
    """Replay redirect events onto the merge redirects of the prior release.

    The rules match the release pipeline:

    * ``entity_added`` or ``entity_status_changed`` with ``redirect_to`` adds or
      moves a merge redirect;
    * ``entity_status_changed`` from ``redirected`` without ``redirect_to`` removes
      the redirect (the ID is active again);
    * ``redirect_changed`` sets the target, and an empty ``to`` removes the row.

    Args:
        prior: ``{from_id: to_id}`` for the prior release's ``merged`` rows.
        events: The changelog's redirect events, in order.

    Returns:
        ``{from_id: to_id}`` for the new release's ``merged`` rows.
    """
    redirects = dict(prior)
    for event in events:
        event_type, reep_id = event["type"], event["reep_id"]
        if event_type in ("entity_added", "entity_status_changed"):
            if "redirect_to" in event:
                redirects[reep_id] = event["redirect_to"]
            elif (
                event_type == "entity_status_changed"
                and event.get("from") == "redirected"
            ):
                redirects.pop(reep_id, None)
        elif event_type == "redirect_changed":
            if event["to"]:
                redirects[reep_id] = event["to"]
            else:
                redirects.pop(reep_id, None)
    return redirects


def stage_bridge_changes(
    con: duckdb.DuckDBPyConnection, changelog: Changelog, name: str
) -> None:
    """Write a changelog's bridge removals and additions to two tables.

    The tables are ``<name>_removes`` and ``<name>_adds``. Staging lets the replay
    parse one changelog at a time while it walks the chain backwards, then apply
    the changes in release order.

    Args:
        con: An open writable connection.
        changelog: The changelog.
        name: The prefix of the two tables.
    """
    for suffix, rows in (
        ("removes", changelog.bridge_removes),
        ("adds", changelog.bridge_adds),
    ):
        table = f"{name}_{suffix}"
        con.execute(
            f"CREATE OR REPLACE TABLE {table} (provider VARCHAR, namespace VARCHAR, "
            "external_id VARCHAR, reep_id VARCHAR)"
        )
        if rows:
            columns = list(zip(*rows, strict=True))
            con.execute(
                f"INSERT INTO {table} SELECT DISTINCT unnest($p), unnest($n), "
                "unnest($x), unnest($r)",
                {"p": columns[0], "n": columns[1], "x": columns[2], "r": columns[3]},
            )


def apply_staged_bridge_changes(
    con: duckdb.DuckDBPyConnection, table: str, name: str
) -> None:
    """Apply staged bridge changes to a table, then drop the staging tables.

    Bridges are a set of ``(provider, namespace, external_id, reep_id)`` rows. A
    removal deletes the matching row. An addition inserts a row with an empty
    ``rung`` and ``upstream_status``, because the changelog does not carry them.

    Args:
        con: An open writable connection.
        table: The bridge table to change.
        name: The prefix that :func:`stage_bridge_changes` used.
    """
    con.execute(
        f"DELETE FROM {table} AS b USING {name}_removes AS d "
        "WHERE b.provider = d.provider AND b.namespace = d.namespace "
        "AND b.external_id = d.external_id AND b.reep_id = d.reep_id"
    )
    con.execute(
        f"INSERT INTO {table} (provider, namespace, external_id, reep_id, rung, "
        "upstream_status) SELECT a.provider, a.namespace, a.external_id, "
        f"a.reep_id, NULL, NULL FROM {name}_adds AS a WHERE NOT EXISTS ("
        f"SELECT 1 FROM {table} AS b WHERE b.provider = a.provider "
        "AND b.namespace = a.namespace AND b.external_id = a.external_id "
        "AND b.reep_id = a.reep_id)"
    )
    con.execute(f"DROP TABLE {name}_removes")
    con.execute(f"DROP TABLE {name}_adds")


def apply_bridge_changes(
    con: duckdb.DuckDBPyConnection, table: str, changelog: Changelog
) -> None:
    """Apply a changelog's bridge removals and additions to a table.

    Args:
        con: An open writable connection.
        table: The bridge table to change.
        changelog: The changelog.
    """
    stage_bridge_changes(con, changelog, "reep_stage")
    apply_staged_bridge_changes(con, table, "reep_stage")
