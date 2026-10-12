"""Generate a small Reep release bucket for tests.

The bucket holds two releases in the shape of the public release contract:

* ``STAMP_1``: the base release.
* ``STAMP_2``: the next release, with a ``changelog.json.gz`` against ``STAMP_1``.

The changelog is derived from the two states with the same rules as the release
pipeline, so a replay of it onto ``STAMP_1`` must give ``STAMP_2``.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

STAMP_0 = "20251225T000000Z"
STAMP_1 = "20260101T000000Z"
STAMP_2 = "20260108T000000Z"

SCHEMA_VERSION = "bridge-register-v1"

ENTITY_COLUMNS = [
    "reep_id",
    "entity_type",
    "status",
    "label",
    "gender",
    "country",
    "corroboration_grade",
    "corroboration_count",
    "source_tier_band",
]
BRIDGE_COLUMNS = [
    "provider",
    "namespace",
    "external_id",
    "reep_id",
    "rung",
    "upstream_status",
]
REDIRECT_COLUMNS = ["from_id", "to_id", "reason"]
ALIAS_COLUMNS = ["reep_id", "alias", "kind", "rank", "language"]
PLAYER_COLUMNS = ["reep_id", "status", "label", "gender", "country"]

# Reep IDs used by the tests.
PALMER = "rp00000000000001"
NAMESAKE = "rp00000000000002"
MERGED = "rp00000000000003"
UNPUBLISHED = "rp00000000000004"
DECORROBORATED = "rp00000000000005"
WITHHELD = "rp00000000000006"
MERGED_TO_RETIRED = "rp00000000000007"
RETIRED = "rp00000000000008"
OZIL = "rp00000000000009"
NEW_PLAYER = "rp0000000000000a"
MARESCA = "rc00000000000001"
CHELSEA = "rt00000000000001"
CHELSEA_WOMEN = "rt00000000000002"
INTER = "rt00000000000003"
PREMIER_LEAGUE = "rl00000000000001"
SEASON = "rs00000000000001"
OLD_SEASON = "rs00000000000002"
STAGE = "rg00000000000001"
MATCH = "rm00000000000001"
REFEREE = "ro00000000000001"
UNKNOWN = "rp00000000000fff"


def _entity(
    rid: str,
    etype: str,
    status: str,
    label: str,
    gender: str = "",
    country: str = "",
) -> dict[str, str]:
    return {
        "reep_id": rid,
        "entity_type": etype,
        "status": status,
        "label": label,
        "gender": gender,
        "country": country,
        "corroboration_grade": "B",
        "corroboration_count": "2",
        "source_tier_band": "multi-bucket",
    }


def _bridge(
    provider: str,
    namespace: str,
    external_id: str,
    rid: str,
    rung: str = "",
    upstream: str = "",
) -> dict[str, str]:
    return {
        "provider": provider,
        "namespace": namespace,
        "external_id": external_id,
        "reep_id": rid,
        "rung": rung,
        "upstream_status": upstream,
    }


@dataclass
class ReleaseState:
    """The tables of one release."""

    entities: list[dict[str, str]]
    bridges: list[dict[str, str]]
    redirects: list[dict[str, str]]
    aliases: list[dict[str, str]] = field(default_factory=list)


def state_1() -> ReleaseState:
    """Return the tables of ``STAMP_1``."""
    entities = [
        _entity(PALMER, "player", "active", "Cole Palmer", "men", "England"),
        _entity(NAMESAKE, "player", "active", "Cole Palmer", "men", "Wales"),
        _entity(MERGED, "player", "redirected", "C. Palmer", "men", "England"),
        _entity(UNPUBLISHED, "player", "redirected", "Hidden Target", "men"),
        _entity(MERGED_TO_RETIRED, "player", "redirected", "Old Twin", "men"),
        _entity(RETIRED, "player", "retired", "Retired Player", "men"),
        _entity(OZIL, "player", "active", "Mesut Özil", "men", "Germany"),
        _entity(MARESCA, "coach", "active", "Enzo Maresca", "men", "Italy"),
        _entity(CHELSEA, "team", "active", "Chelsea", "men", "England"),
        _entity(CHELSEA_WOMEN, "team", "active", "Chelsea", "women", "England"),
        _entity(INTER, "team", "active", 'Inter, "Nerazzurri"', "men", "Italy"),
        _entity(PREMIER_LEAGUE, "competition", "active", "Premier League", "men"),
        _entity(SEASON, "season", "active", "Premier League 2025/26"),
        _entity(OLD_SEASON, "season", "retired", "Premier League 1891/92"),
        _entity(STAGE, "stage", "active", "Premier League 2025/26 Regular Season"),
        _entity(MATCH, "match", "active", "Chelsea v Arsenal"),
        _entity(REFEREE, "referee", "active", "Michael Oliver", "men", "England"),
    ]
    bridges = [
        _bridge("transfermarkt", "spieler", "568177", PALMER, "corroborated-mint"),
        _bridge("fbref", "person", "dc7f8a28", PALMER, "name-dob"),
        _bridge("opta", "person", "p1", PALMER, "first-party"),
        _bridge("sofascore", "player", "111", PALMER, "footprint"),
        _bridge("fotmob", "person", "1001", PALMER, "footprint", "retired"),
        _bridge("fotmob", "person", "1002", PALMER, "footprint"),
        _bridge("transfermarkt", "spieler", "900001", NAMESAKE, ""),
        _bridge("skillcorner", "player", "777", NAMESAKE, "name-dob", "removed"),
        _bridge("whoscored", "player", "333", MERGED, "crosswalk"),
        _bridge("wyscout", "player", "444", UNPUBLISHED, "name-nationality"),
        _bridge("wyscout", "player", "777", MERGED_TO_RETIRED, ""),
        _bridge("transfermarkt", "spieler", "35664", OZIL, "corroborated-mint"),
        _bridge("transfermarkt", "trainer", "568177", MARESCA, "operator"),
        _bridge("opta", "person", "c1", MARESCA, "first-party"),
        _bridge("transfermarkt", "verein", "631", CHELSEA, "corroborated-mint"),
        _bridge("opta", "team", "t1", CHELSEA, "first-party"),
        _bridge("soccerdonna", "verein", "2002", CHELSEA_WOMEN, "corroborated-mint"),
        _bridge("transfermarkt", "verein", "46", INTER, "corroborated-mint"),
        _bridge("opta", "competition", "8", PREMIER_LEAGUE, "first-party"),
        _bridge("opta", "season", "2025", SEASON, "first-party"),
        _bridge("opta", "stage", "s1", STAGE, "first-party"),
        _bridge("opta", "match", "m1", MATCH, "footprint"),
        _bridge("transfermarkt", "schiedsrichter", "4", REFEREE, "name-dob"),
    ]
    redirects = [
        {"from_id": MERGED, "to_id": PALMER, "reason": "merged"},
        {"from_id": UNPUBLISHED, "to_id": "", "reason": "target_not_published"},
        {"from_id": DECORROBORATED, "to_id": "", "reason": "de_corroborated"},
        {"from_id": WITHHELD, "to_id": "", "reason": "withheld"},
        {"from_id": MERGED_TO_RETIRED, "to_id": RETIRED, "reason": "merged"},
    ]
    aliases = [
        {
            "reep_id": PALMER,
            "alias": "Cole Jermaine Palmer",
            "kind": "official_name",
            "rank": "1",
            "language": "",
        },
        {
            "reep_id": PALMER,
            "alias": "Палмер",
            "kind": "provider-name",
            "rank": "2",
            "language": "ru",
        },
        {
            "reep_id": CHELSEA,
            "alias": "Chelsea FC",
            "kind": "short",
            "rank": "1",
            "language": "",
        },
        {
            "reep_id": CHELSEA,
            "alias": "The Blues",
            "kind": "known_name",
            "rank": "",
            "language": "en",
        },
    ]
    return ReleaseState(entities, bridges, redirects, aliases)


def state_2() -> ReleaseState:
    """Return the tables of ``STAMP_2``: ``STAMP_1`` after a week of changes."""
    s = state_1()
    entities = {e["reep_id"]: dict(e) for e in s.entities}
    # entity_added
    entities[NEW_PLAYER] = _entity(NEW_PLAYER, "player", "active", "New Player", "men")
    # entity_removed (de-corroborated; the release adds a tombstone)
    del entities[NAMESAKE]
    # entity_status_changed with a new redirect
    entities[OZIL]["status"] = "redirected"
    # label_updated
    entities[CHELSEA]["label"] = "Chelsea FC"
    # The retired survivor leaves the cut: its merge row loses its target.
    del entities[RETIRED]
    bridges = [
        dict(b)
        for b in s.bridges
        if not (b["provider"] == "sofascore" and b["external_id"] == "111")
        and b["reep_id"] != NAMESAKE
    ]
    bridges.append(_bridge("understat", "player", "u1", PALMER, "name-dob"))
    bridges.append(_bridge("transfermarkt", "spieler", "1000001", NEW_PLAYER, ""))
    # A triple moves from one entity to another: the merged ID's bridge moves to
    # the survivor.
    for b in bridges:
        if b["provider"] == "whoscored" and b["external_id"] == "333":
            b["reep_id"] = PALMER
    bridges.append(_bridge("opta", "person", "p9", NEW_PLAYER, "first-party"))
    redirects = [
        {"from_id": MERGED, "to_id": PALMER, "reason": "merged"},
        # The target of UNPUBLISHED is now published: the row gains a target.
        {"from_id": UNPUBLISHED, "to_id": PALMER, "reason": "merged"},
        {"from_id": DECORROBORATED, "to_id": "", "reason": "de_corroborated"},
        {"from_id": WITHHELD, "to_id": "", "reason": "withheld"},
        {"from_id": NAMESAKE, "to_id": "", "reason": "de_corroborated"},
        {"from_id": OZIL, "to_id": PALMER, "reason": "merged"},
        {"from_id": MERGED_TO_RETIRED, "to_id": "", "reason": "target_not_published"},
        {"from_id": RETIRED, "to_id": "", "reason": "withheld"},
    ]
    return ReleaseState(list(entities.values()), bridges, redirects, list(s.aliases))


# -- changelog, ported from the release pipeline's rules ----------------------

_EVENT_ORDER = (
    "entity_added",
    "entity_removed",
    "entity_status_changed",
    "entity_type_changed",
    "label_updated",
    "redirect_changed",
    "provider_updated",
    "score_updated",
    "fixture_updated",
)


def _apply_redirect(redirects: dict[str, str], event: dict[str, Any]) -> None:
    etype, rid = event["type"], event["reep_id"]
    if etype in ("entity_added", "entity_status_changed"):
        if "redirect_to" in event:
            redirects[rid] = event["redirect_to"]
        elif etype == "entity_status_changed" and event.get("from") == "redirected":
            redirects.pop(rid, None)
    elif etype == "redirect_changed":
        if event["to"]:
            redirects[rid] = event["to"]
        else:
            redirects.pop(rid, None)


def changelog_events(prior: ReleaseState, new: ReleaseState) -> list[dict[str, Any]]:
    """Return the changelog events from ``prior`` to ``new``."""
    events: list[dict[str, Any]] = []
    p_ent = {
        e["reep_id"]: {k: e[k] for k in ("entity_type", "status", "label")}
        for e in prior.entities
    }
    n_ent = {
        e["reep_id"]: {k: e[k] for k in ("entity_type", "status", "label")}
        for e in new.entities
    }
    p_red = {r["from_id"]: r["to_id"] for r in prior.redirects if r["to_id"]}
    n_red = {r["from_id"]: r["to_id"] for r in new.redirects if r["to_id"]}
    for rid in n_ent.keys() - p_ent.keys():
        e: dict[str, Any] = {"type": "entity_added", "reep_id": rid, **n_ent[rid]}
        if e["status"] == "redirected" and rid in n_red:
            e["redirect_to"] = n_red[rid]
        events.append(e)
    for rid in p_ent.keys() - n_ent.keys():
        events.append({"type": "entity_removed", "reep_id": rid})
    for rid in p_ent.keys() & n_ent.keys():
        old, cur = p_ent[rid], n_ent[rid]
        if old["entity_type"] != cur["entity_type"]:
            events.append(
                {
                    "type": "entity_type_changed",
                    "reep_id": rid,
                    "from": old["entity_type"],
                    "to": cur["entity_type"],
                }
            )
        if old["status"] != cur["status"]:
            ev: dict[str, Any] = {
                "type": "entity_status_changed",
                "reep_id": rid,
                "from": old["status"],
                "to": cur["status"],
            }
            if cur["status"] == "redirected" and rid in n_red:
                ev["redirect_to"] = n_red[rid]
            events.append(ev)
        if old["label"] != cur["label"]:
            events.append(
                {
                    "type": "label_updated",
                    "reep_id": rid,
                    "from": old["label"],
                    "to": cur["label"],
                }
            )
    p_br = {
        (b["provider"], b["namespace"], b["external_id"], b["reep_id"])
        for b in prior.bridges
    }
    n_br = {
        (b["provider"], b["namespace"], b["external_id"], b["reep_id"])
        for b in new.bridges
    }
    changed: dict[str, dict[str, list[dict[str, str]]]] = {}
    for prov, ns, ext, rid in sorted(n_br - p_br):
        changed.setdefault(rid, {"added": [], "removed": []})["added"].append(
            {"provider": prov, "namespace": ns, "external_id": ext}
        )
    for prov, ns, ext, rid in sorted(p_br - n_br):
        changed.setdefault(rid, {"added": [], "removed": []})["removed"].append(
            {"provider": prov, "namespace": ns, "external_id": ext}
        )
    for rid, delta in changed.items():
        events.append({"type": "provider_updated", "reep_id": rid, **delta})
    for rid in p_red.keys() & n_red.keys():
        if p_red[rid] != n_red[rid]:
            events.append(
                {
                    "type": "redirect_changed",
                    "reep_id": rid,
                    "from": p_red[rid],
                    "to": n_red[rid],
                }
            )
    replayed = dict(p_red)
    for event in events:
        _apply_redirect(replayed, event)
    for rid in sorted(n_red.keys() - replayed.keys()):
        events.append(
            {"type": "redirect_changed", "reep_id": rid, "from": "", "to": n_red[rid]}
        )
    for rid in sorted(replayed.keys() - n_red.keys()):
        events.append(
            {
                "type": "redirect_changed",
                "reep_id": rid,
                "from": replayed[rid],
                "to": "",
            }
        )
    # A score event for a table the SDK does not replay in the slim profile.
    events.append(
        {
            "type": "score_updated",
            "reep_id": MATCH,
            "home_score": "2",
            "away_score": "1",
            "result_form": "regular",
            "score_basis": "corroborated",
            "shootout_home": "",
            "shootout_away": "",
        }
    )
    order = {t: i for i, t in enumerate(_EVENT_ORDER)}
    events.sort(key=lambda e: (order[e["type"]], e["reep_id"]))
    return events


# -- writing a bucket ----------------------------------------------------------


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_release(
    root: Path,
    stamp: str,
    state: ReleaseState,
    *,
    prior: tuple[str, ReleaseState] | None = None,
    bridge_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Write one release under ``root/releases/<stamp>/`` and return its manifest."""
    out = root / "releases" / stamp
    files: dict[str, dict[str, Any]] = {}
    rows: dict[str, int] = {}
    players = [
        {k: e[k] for k in PLAYER_COLUMNS}
        for e in state.entities
        if e["entity_type"] == "player"
    ]
    columns = bridge_columns or BRIDGE_COLUMNS
    bridges = [{k: b[k] for k in columns} for b in state.bridges]
    tables = {
        "entities": (ENTITY_COLUMNS, state.entities, "canonical_csv"),
        "bridges": (columns, bridges, "canonical_csv"),
        "redirects": (REDIRECT_COLUMNS, state.redirects, "canonical_csv"),
        "aliases": (ALIAS_COLUMNS, state.aliases, "canonical_csv"),
        "players": (PLAYER_COLUMNS, players, "canonical_csv"),
    }
    for table, (cols, data, role) in tables.items():
        name = f"csv/{table}.csv.gz"
        count = _write_csv(out / name, cols, data)
        rows[name] = count
        files[name] = {"rows": count, "role": role}
    changelog: dict[str, Any] = {
        "changelog_version": 1,
        "mode": "public_bridge_v1",
        "new": {"stamp": stamp, "generated_at": _generated_at(stamp)},
    }
    if prior is None:
        changelog["prior"] = None
    else:
        prior_stamp, prior_state = prior
        events = changelog_events(prior_state, state)
        counts: dict[str, int] = {}
        for event in events:
            counts[event["type"]] = counts.get(event["type"], 0) + 1
        changelog["prior"] = {
            "stamp": prior_stamp,
            "dir_name": prior_stamp,
            "generated_at": _generated_at(prior_stamp),
        }
        changelog["counts"] = counts
        changelog["events"] = events
    with gzip.open(out / "changelog.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(changelog, fh)
    files["changelog.json.gz"] = {"role": "metadata"}
    duck = out / "duckdb" / "reep-register-v1.duckdb"
    write_shipped_duckdb(duck, out, list(tables), stamp)
    files["duckdb/reep-register-v1.duckdb"] = {"role": "convenience"}
    for name, entry in files.items():
        path = out / name
        entry["bytes"] = path.stat().st_size
        entry["sha256"] = _sha256(path)
        entry["object_key"] = f"releases/{stamp}/{name}"
        entry["url"] = f"https://example.invalid/releases/{stamp}/{name}"
    manifest = {
        "files": files,
        "generated_at": _generated_at(stamp),
        "manifest_version": 1,
        "projection_mode": "public_bridge_v1",
        "row_counts": rows,
        "schema_version": SCHEMA_VERSION,
        "stamp": stamp,
        "tier": "public_bridge_v1",
    }
    (out / "release.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def write_shipped_duckdb(
    path: Path, release_dir: Path, tables: list[str], stamp: str
) -> None:
    """Write a DuckDB file the way the release pipeline does, without the FTS index."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    try:
        for table in tables:
            csv_path = release_dir / "csv" / f"{table}.csv.gz"
            con.execute(
                f"CREATE TABLE {table} AS SELECT * FROM read_csv_auto("
                "?, header=true, all_varchar=true, quote='\"', escape='\"')",
                [str(csv_path)],
            )
        con.execute(
            "CREATE TABLE entity_search AS "
            "WITH ca AS (SELECT reep_id, string_agg(DISTINCT alias, ' ') AS names, "
            "count(DISTINCT alias) AS alias_count FROM aliases GROUP BY reep_id), "
            "bc AS (SELECT reep_id, count(*) AS bridge_count FROM bridges "
            "GROUP BY reep_id) "
            "SELECT e.reep_id, e.entity_type, e.label, "
            "trim(concat_ws(' ', e.label, ca.names)) AS search_text, "
            "coalesce(bc.bridge_count, 0)::BIGINT AS bridge_count, "
            "coalesce(ca.alias_count, 0)::BIGINT AS alias_count "
            "FROM entities e LEFT JOIN ca USING (reep_id) LEFT JOIN bc USING (reep_id)"
        )
        con.execute("CREATE TABLE release_metadata (key VARCHAR, value VARCHAR)")
        for key, value in (
            ("source_stamp", stamp),
            ("schema_version", SCHEMA_VERSION),
            ("projection_mode", "public_bridge_v1"),
            ("search_index", "entity_search_fts_v1"),
        ):
            con.execute("INSERT INTO release_metadata VALUES (?, ?)", [key, value])
    finally:
        con.close()


def write_latest(root: Path, stamp: str) -> None:
    """Point ``releases/latest.json`` at a release."""
    manifest = root / "releases" / stamp / "release.json"
    pointer = {
        "schema_version": "reep-public-release-latest-v1",
        "stamp": stamp,
        "generated_at": _generated_at(stamp),
        "release_schema_version": SCHEMA_VERSION,
        "manifest_object_key": f"releases/{stamp}/release.json",
        "manifest_sha256": _sha256(manifest),
    }
    (root / "releases" / "latest.json").write_text(
        json.dumps(pointer), encoding="utf-8"
    )


def _generated_at(stamp: str) -> str:
    return (
        f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:00+00:00"
    )


@dataclass(frozen=True)
class FixtureBucket:
    """A generated bucket on disk."""

    root: Path

    def release_dir(self, stamp: str) -> Path:
        """Return the directory of a release."""
        return self.root / "releases" / stamp

    def point_latest(self, stamp: str) -> None:
        """Point ``latest.json`` at a release."""
        write_latest(self.root, stamp)


def build_bucket(root: Path) -> FixtureBucket:
    """Write the fixture bucket under ``root``, with ``latest.json`` at ``STAMP_2``."""
    s0 = state_1()
    # STAMP_0 is an older release cut before the bridge metadata columns existed.
    write_release(
        root,
        STAMP_0,
        s0,
        bridge_columns=["provider", "namespace", "external_id", "reep_id"],
    )
    s1 = state_1()
    write_release(root, STAMP_1, s1, prior=(STAMP_0, s0))
    write_release(root, STAMP_2, state_2(), prior=(STAMP_1, s1))
    write_latest(root, STAMP_2)
    return FixtureBucket(root)
