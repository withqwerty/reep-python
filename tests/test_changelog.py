from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from reep.changelog import Changelog, apply_bridge_changes, replay_redirects
from reep.errors import ReplayError
from tests.support.fixture_release import (
    MERGED_TO_RETIRED,
    NAMESAKE,
    OZIL,
    PALMER,
    STAMP_1,
    STAMP_2,
    UNPUBLISHED,
    FixtureBucket,
)


def _doc(bucket: FixtureBucket, stamp: str = STAMP_2) -> dict[str, Any]:
    path = bucket.release_dir(stamp) / "changelog.json.gz"
    data: dict[str, Any] = json.loads(gzip.decompress(path.read_bytes()))
    return data


def test_read_the_fixture_changelog(bucket: FixtureBucket) -> None:
    log = Changelog.read(bucket.release_dir(STAMP_2) / "changelog.json.gz")
    assert log.stamp == STAMP_2
    assert log.prior_stamp == STAMP_1
    assert log.counts["provider_updated"] >= 1
    assert ("understat", "player", "u1", PALMER) in log.bridge_adds
    assert ("sofascore", "player", "111", PALMER) in log.bridge_removes
    assert ("whoscored", "player", "333", PALMER) in log.bridge_adds
    assert any(r[3] == NAMESAKE and r[0] == "transfermarkt" for r in log.bridge_removes)
    types = {e["type"] for e in log.redirect_events}
    assert {"entity_status_changed", "redirect_changed"} <= types


def test_redirect_replay_matches_the_new_release(bucket: FixtureBucket) -> None:
    log = Changelog.read(bucket.release_dir(STAMP_2) / "changelog.json.gz")
    prior = {"rp00000000000003": PALMER, MERGED_TO_RETIRED: "rp00000000000008"}
    replayed = replay_redirects(prior, log.redirect_events)
    assert replayed == {
        "rp00000000000003": PALMER,
        UNPUBLISHED: PALMER,
        OZIL: PALMER,
    }


def test_replay_redirects_rules() -> None:
    events = [
        {"type": "entity_added", "reep_id": "a", "redirect_to": "x"},
        {
            "type": "entity_status_changed",
            "reep_id": "b",
            "from": "redirected",
            "to": "active",
        },
        {
            "type": "entity_status_changed",
            "reep_id": "c",
            "from": "active",
            "to": "retired",
        },
        {"type": "redirect_changed", "reep_id": "d", "from": "y", "to": "z"},
        {"type": "redirect_changed", "reep_id": "e", "from": "y", "to": ""},
        {"type": "redirect_changed", "reep_id": "f", "from": "", "to": "w"},
    ]
    prior = {"b": "q", "c": "q", "d": "y", "e": "y"}
    assert replay_redirects(prior, events) == {
        "a": "x",
        "c": "q",
        "d": "z",
        "f": "w",
    }
    assert prior == {"b": "q", "c": "q", "d": "y", "e": "y"}


def test_no_prior_means_no_events(bucket: FixtureBucket) -> None:
    doc = _doc(bucket)
    doc["prior"] = None
    del doc["events"]
    log = Changelog.from_json(doc)
    assert log.prior_stamp is None
    assert log.bridge_adds == []


def _mutate(doc: dict[str, Any], change: str) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    events: list[Any] = out["events"]
    provider_event = next(e for e in events if e["type"] == "provider_updated")
    if change == "version":
        out["changelog_version"] = 2
    elif change == "no_new_stamp":
        out["new"] = {}
    elif change == "no_prior_stamp":
        out["prior"] = {"dir_name": "x"}
    elif change == "no_events":
        del out["events"]
    elif change == "unknown_type":
        events.append({"type": "entity_teleported", "reep_id": PALMER})
    elif change == "no_reep_id":
        events.append({"type": "label_updated", "from": "a", "to": "b"})
    elif change == "not_an_object":
        events.append("event")
    elif change == "counts":
        out["counts"]["provider_updated"] += 1
    elif change == "duplicate":
        events.append(copy.deepcopy(provider_event))
        out["counts"]["provider_updated"] += 1
    elif change == "bad_added":
        provider_event["added"] = [{"provider": "x"}]
    elif change == "added_not_list":
        provider_event["added"] = "x"
    elif change == "redirect_without_to":
        events.append({"type": "redirect_changed", "reep_id": PALMER, "from": "a"})
        out["counts"]["redirect_changed"] = out["counts"].get("redirect_changed", 0) + 1
    return out


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("version", "version"),
        ("no_new_stamp", "new.stamp"),
        ("no_prior_stamp", "prior.stamp"),
        ("no_events", "events"),
        ("unknown_type", "entity_teleported"),
        ("no_reep_id", "reep_id"),
        ("not_an_object", "not an object"),
        ("counts", "counts"),
        ("duplicate", "two provider_updated"),
        ("bad_added", "not valid"),
        ("added_not_list", "added list"),
        ("redirect_without_to", "from or to"),
    ],
)
def test_invalid_changelogs_are_refused(
    bucket: FixtureBucket, change: str, message: str
) -> None:
    with pytest.raises(ReplayError, match=message):
        Changelog.from_json(_mutate(_doc(bucket), change))


def test_unreadable_changelog(tmp_path: Path) -> None:
    path = tmp_path / "changelog.json.gz"
    path.write_bytes(b"not gzip")
    with pytest.raises(ReplayError, match="cannot be read"):
        Changelog.read(path)
    with pytest.raises(ReplayError, match="not a JSON object"):
        Changelog.from_json([])


def test_apply_bridge_changes(tmp_path: Path) -> None:
    con = duckdb.connect(str(tmp_path / "b.duckdb"))
    con.execute(
        "CREATE TABLE b (provider VARCHAR, namespace VARCHAR, external_id VARCHAR, "
        "reep_id VARCHAR, rung VARCHAR, upstream_status VARCHAR)"
    )
    con.execute(
        "INSERT INTO b VALUES ('p', 'n', '1', 'r1', 'name-dob', NULL), "
        "('p', 'n', '2', 'r1', 'footprint', 'retired')"
    )
    log = Changelog(
        stamp=STAMP_2,
        prior_stamp=STAMP_1,
        counts={},
        bridge_adds=[("p", "n", "3", "r2"), ("p", "n", "1", "r1")],
        bridge_removes=[("p", "n", "2", "r1"), ("p", "n", "9", "r9")],
    )
    apply_bridge_changes(con, "b", log)
    rows = con.execute("SELECT * FROM b ORDER BY external_id").fetchall()
    assert rows == [
        ("p", "n", "1", "r1", "name-dob", None),
        ("p", "n", "3", "r2", None, None),
    ]
    empty = Changelog(stamp=STAMP_2, prior_stamp=STAMP_1, counts={})
    apply_bridge_changes(con, "b", empty)
    assert len(con.execute("SELECT * FROM b").fetchall()) == 2
    con.close()
