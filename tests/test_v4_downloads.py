"""Versioned downloads: real exporter bytes, synthetic selector, no live services."""

from __future__ import annotations

import csv
import gzip
import io
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from reep import Register, _v4
from reep.backends.local import LocalBackend
from reep.catalog import import_release, state, sync
from reep.errors import ChecksumMismatchError, ReplayError, UnsupportedReleaseError
from reep.models import IdStatus, entity_type_of, validate_reep_id
from tests.conftest import make_settings
from tests.support.v4_release import STAMP, V4Bucket, encode, sha

TEAM = "rt8df038ffeaed40"
MATCH = "rm8a047e9193a247"
VENUE = "rv7bffdbdab61249"
OLD = "rt00000000000000"


@pytest.fixture
def v4(tmp_path: Path) -> V4Bucket:
    return V4Bucket(tmp_path / "release")


def rows(bucket: V4Bucket, table: str) -> tuple[list[str], list[dict[str, str]]]:
    with gzip.open(bucket.root / f"{table}.csv.gz", "rt", newline="") as f:
        reader = csv.DictReader(f)
        result = list(reader)
        return list(reader.fieldnames or []), result


def write_rows(
    bucket: V4Bucket, table: str, header: list[str], values: list[dict[str, Any]]
) -> None:
    # Explicit controlled CSV mutation for adapter/failure tests, not qualified data.
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    writer.writerows(values)
    (bucket.root / f"{table}.csv.gz").write_bytes(
        gzip.compress(out.getvalue().encode(), mtime=0)
    )
    manifest = json.loads((bucket.root / "manifest.json").read_text())
    manifest[_v4.COUNT_KEYS[table]] = len(values)
    (bucket.root / "manifest.json").write_bytes(encode(manifest))
    bucket.rebuild()


@pytest.mark.parametrize("profile", ["slim", "full"])
def test_real_exporter_snapshot_preserves_unreported_lifecycle(
    v4: V4Bucket, tmp_path: Path, profile: str
) -> None:
    settings = make_settings(tmp_path / "cache")
    with v4.client() as client:
        manifest = client.manifest()
        assert manifest.schema_version == _v4.SCHEMA
        entry = manifest.table_file("entities")
        assert entry is not None
        assert entry.rows == 8
        result = sync(profile, client=client, settings=settings)
        assert result.method == "snapshot"
        assert sync(profile, client=client, settings=settings).method == "up_to_date"
        with pytest.raises(ReplayError, match="unavailable"):
            sync(method="replay", client=client, settings=settings)
    with Register.local(settings=settings) as reg:
        resolved = reg.resolve("opta:team:TH")
        assert resolved.found
        assert resolved.reep_id == TEAM
        assert resolved.entity_status is None
        assert resolved.bridge is not None
        assert resolved.bridge.rung is None
        assert resolved.bridge.upstream_status is None
        current = reg.current(TEAM)
        assert current.status is IdStatus.PUBLISHED
        assert current.current_status is None
        assert current.current_id == TEAM
        entity = reg.entity(TEAM)
        assert entity is not None
        assert entity.label == "Home United"
        assert entity.status is None
        assert reg.entity(VENUE).entity_type == "venue"
        assert reg.search("Home United")[0].status is None
        assert reg.release.stamp == STAMP
        assert reg.release.extra["sdk_manifest_sha256"] == manifest.identity_sha256
    assert "pointer.json" not in v4.calls
    assert all("changelog" not in call for call in v4.calls)


def test_local_import_hash_binding_and_exact_triples(
    v4: V4Bucket, tmp_path: Path
) -> None:
    header, values = rows(v4, "bridges")
    for row in values:
        if (
            row["provider"] == "opta"
            and row["namespace"] == "team"
            and row["external_id"] == "TH"
        ):
            row["external_id"] = "0042"
    write_rows(v4, "bridges", header, values)
    settings = make_settings(tmp_path / "cache")
    import_release(v4.root, profile="full", settings=settings)
    with Register.local(settings=settings) as reg:
        assert reg.resolve("opta:team:0042").reep_id == TEAM
        assert not reg.resolve("opta:team:42").found
        assert not reg.resolve("opta:player:0042").found
    assert v4.calls == []
    (v4.root / "entities.csv.gz").write_bytes(b"tampered")
    before = sha(settings.catalog_path.read_bytes())
    with pytest.raises(ChecksumMismatchError):
        import_release(v4.root, settings=settings)
    assert sha(settings.catalog_path.read_bytes()) == before


def test_shipped_database_is_read_only_and_unknown_stamp(v4: V4Bucket) -> None:
    path = v4.root / "reep-register.duckdb"
    before = sha(path.read_bytes())
    with Register.local(path) as reg:
        assert reg.resolve("opta:team:TH").reep_id == TEAM
        assert reg.current(TEAM).status is IdStatus.PUBLISHED
        assert reg.release.stamp is None
        assert reg.release.extra["publication_identity"] == "unverified_local_file"
        assert reg.entity(TEAM).status is None
        assert reg.search("Home")[0].label == "Home United"
    assert sha(path.read_bytes()) == before
    assert not path.with_name(path.name + ".wal").exists()


@pytest.mark.parametrize("scores", [("0", "0"), ("", "")])
def test_match_zero_and_unknown_are_distinct(
    v4: V4Bucket, tmp_path: Path, scores: tuple[str, str]
) -> None:
    header, values = rows(v4, "matches")
    values[0]["home_score"], values[0]["away_score"] = scores
    write_rows(v4, "matches", header, values)
    settings = make_settings(tmp_path / "cache")
    import_release(v4.root, profile="full", settings=settings)
    backend = LocalBackend(settings.catalog_path)
    try:
        result = backend.matches(team_id=TEAM)
        assert len(result) == 1
        assert result[0]["reep_id"] == MATCH
        assert (
            result[0]["home_score"]
            == result[0]["away_score"]
            == (0 if scores[0] else None)
        )
        assert result[0]["status"] is None
        assert result[0]["score_basis"] is None
        assert backend.matches(date_from="2026-09-11") == []
        assert backend.matches(season_id="rs00000000000000") == []
        filters_list: list[dict[str, Any]] = [
            {"status": "active"},
            {"competition_id": "rl00000000000000"},
        ]
        for filters in filters_list:
            with pytest.raises(UnsupportedReleaseError, match="unavailable"):
                backend.matches(**filters)
    finally:
        backend.close()


def test_v4_redirect_is_not_renamed_merged_and_aliases_survive(
    v4: V4Bucket, tmp_path: Path
) -> None:
    header, values = rows(v4, "redirects")
    values.append(dict.fromkeys(header, ""))
    values[-1].update(from_id=OLD, to_id=TEAM, reason="reviewed_duplicate")
    write_rows(v4, "redirects", header, values)
    header, values = rows(v4, "provider_aliases")
    values.append(
        {
            "reep_id": TEAM,
            "alias": "Explicit alternate",
            "alias_kind": "source",
            "provider": "",
        }
    )
    write_rows(v4, "provider_aliases", header, values)
    settings = make_settings(tmp_path / "cache")
    import_release(v4.root, profile="slim", with_aliases=True, settings=settings)
    with Register.local(settings=settings) as reg:
        current = reg.current(OLD)
        assert current.status is IdStatus.REDIRECTED
        assert current.current_id == TEAM
        assert current.current_status is None
        assert current.reason == "reviewed_duplicate"
        entity = reg.entity(OLD)
        assert entity is not None
        assert entity.reep_id == TEAM
        assert entity.status is None
        assert entity.aliases == ("Explicit alternate",)
        assert reg.search("alternate")[0].reep_id == TEAM
        assert (
            reg.translate("opta:team:TH", "transfermarkt")
            .targets[0]
            .provider_id.external_id
            == "TH"
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "channel",
        "schema",
        "hash",
        "bytes",
        "candidate",
        "policy",
        "stamp",
        "path",
        "source",
    ],
)
def test_selector_descriptor_mismatches_never_fall_back(
    v4: V4Bucket, mutation: str
) -> None:
    p = v4.selected
    if mutation == "channel":
        p["channel"] = "api"
    elif mutation == "schema":
        p["schema"] = "unknown"
    elif mutation == "hash":
        p["release"]["sha256"] = "0" * 64
    elif mutation == "bytes":
        p["release"]["bytes"] += 1
    elif mutation in {"candidate", "policy"}:
        p[mutation + "_sha256"] = "0" * 64
    elif mutation == "stamp":
        p["stamp"] = "2026-10-24.1"
    elif mutation == "path":
        p["release"]["key"] = "../release.json"
    else:
        v4.descriptor["source_manifest_sha256"] = "0" * 64
        v4.publish_descriptor()
        p = v4.selected
    v4.objects["releases/latest.json"] = encode(p)
    with (
        v4.client() as client,
        pytest.raises((UnsupportedReleaseError, ChecksumMismatchError)),
    ):
        client.manifest()
    assert "pointer.json" not in v4.calls


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "key",
        "sha",
        "bytes",
        "bool",
        "foreign_stamp",
        "source_generation",
        "extra",
    ],
)
def test_bad_descriptor_is_explicit(v4: V4Bucket, mutation: str) -> None:
    d = v4.descriptor
    if mutation == "missing":
        del d["files"]["entities.csv.gz"]
    elif mutation == "key":
        d["files"]["entities.csv.gz"]["key"] = (
            "releases/2026-10-23.1/objects/../entities.csv.gz"
        )
    elif mutation == "sha":
        d["files"]["entities.csv.gz"]["sha256"] = "bad"
    elif mutation == "bytes":
        d["files"]["entities.csv.gz"]["bytes"] = 0
    elif mutation == "bool":
        d["files"]["entities.csv.gz"]["bytes"] = True
    elif mutation == "foreign_stamp":
        d["files"]["entities.csv.gz"]["key"] = (
            "releases/2026-10-22.1/objects/x/entities.csv.gz"
        )
    elif mutation == "source_generation":
        d["source"]["generation_id"] = "../unknown"
    else:
        d["unrecognised"] = "field"
    v4.publish_descriptor()
    with v4.client() as client, pytest.raises(UnsupportedReleaseError):
        client.manifest()


def test_duplicate_json_and_metadata_bounds(v4: V4Bucket) -> None:
    raw = v4.objects["releases/latest.json"]
    v4.objects["releases/latest.json"] = raw[:-1] + b',"channel":"downloads"}'
    with (
        v4.client() as client,
        pytest.raises(UnsupportedReleaseError, match="unambiguous"),
    ):
        client.manifest()
    v4.objects["releases/latest.json"] = b" " * 65_537
    with v4.client() as client, pytest.raises(UnsupportedReleaseError, match="bound"):
        client.latest()


@pytest.mark.parametrize("mutation", ["count", "checksum", "tag", "boolean_count"])
def test_source_manifest_binding(v4: V4Bucket, mutation: str) -> None:
    manifest = json.loads((v4.root / "manifest.json").read_text())
    if mutation == "count":
        manifest["entity_count"] = -1
    elif mutation == "checksum":
        manifest["checksums_sha256"]["entities.parquet"] = "0" * 64
    elif mutation == "tag":
        manifest["release_tag"] = "2026-10-23.2"
    else:
        manifest["entity_count"] = True
    raw = encode(manifest)
    ref = v4.descriptor["files"]["manifest.json"]
    ref.update(sha256=sha(raw), bytes=len(raw))
    v4.descriptor["source_manifest_sha256"] = sha(raw)
    v4.objects[ref["key"]] = raw
    v4.publish_descriptor()
    with v4.client() as client, pytest.raises(UnsupportedReleaseError):
        client.manifest()


@pytest.mark.parametrize(
    "mutation", ["rows", "columns", "duplicate", "dangling", "cycle"]
)
def test_bad_table_never_replaces_catalogue(
    v4: V4Bucket, tmp_path: Path, mutation: str
) -> None:
    settings = make_settings(tmp_path / "cache")
    with v4.client() as client:
        sync(client=client, settings=settings)
    before = sha(settings.catalog_path.read_bytes())
    if mutation in {"dangling", "cycle"}:
        header, values = rows(v4, "redirects")
        values.append(
            {
                "from_id": OLD,
                "to_id": OLD if mutation == "cycle" else "rt11111111111111",
                "reason": "correction",
                "created_at": "",
            }
        )
        write_rows(v4, "redirects", header, values)
    else:
        header, values = rows(v4, "entities")
        if mutation == "rows":
            values.pop()
        elif mutation == "columns":
            header.remove("canonical_name")
            for row in values:
                del row["canonical_name"]
        else:
            values.append(values[0])
        write_rows(v4, "entities", header, values)
    v4.rebuild("2026-10-23.2")
    with v4.client() as client, pytest.raises(UnsupportedReleaseError):
        sync(client=client, settings=settings)
    assert sha(settings.catalog_path.read_bytes()) == before


def test_same_stamp_changed_bytes_refused_and_numeric_revision_snapshots(
    v4: V4Bucket, tmp_path: Path
) -> None:
    settings = make_settings(tmp_path / "cache")
    with v4.client() as client:
        sync(client=client, settings=settings)
    v4.descriptor["policy_sha256"] = "d" * 64
    v4.publish_descriptor()
    with (
        v4.client() as client,
        pytest.raises(UnsupportedReleaseError, match="same stamp"),
    ):
        sync(client=client, settings=settings)
    v4.rebuild("2026-10-23.10")
    with v4.client() as client:
        result = sync(client=client, settings=settings)
    assert result.method == "snapshot"
    assert "unavailable" in result.notes[0]
    current = state(settings)
    assert current is not None
    assert current.stamp == "2026-10-23.10"


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-02-30.1",
        "2026-10-23.0",
        "2026-10-23.01",
        "2026-10-23.999999999999999999999",
    ],
)
def test_invalid_v4_stamp(v4: V4Bucket, stamp: str) -> None:
    v4.selected["stamp"] = stamp
    v4.objects["releases/latest.json"] = encode(v4.selected)
    with v4.client() as client, pytest.raises(UnsupportedReleaseError):
        client.latest()


@pytest.mark.parametrize(
    ("identifier", "kind"),
    [(VENUE, "venue"), ("r_" + "0" * 16, "stage"), ("m_" + "0" * 16, "match")],
)
def test_declared_id_families(identifier: str, kind: str) -> None:
    assert validate_reep_id(identifier) == identifier
    assert entity_type_of(identifier) == kind


def test_shipped_unknown_layout_refused(v4: V4Bucket) -> None:
    path = v4.root / "reep-register.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("ALTER TABLE entities ADD COLUMN status VARCHAR")
    with pytest.raises(UnsupportedReleaseError, match="layout"):
        LocalBackend(path)


def test_published_state_database_sync_preserves_unknown_facts(
    v4: V4Bucket, tmp_path: Path
) -> None:
    import sqlalchemy as sa

    from reep import dbsync

    settings = make_settings(tmp_path / "cache")
    import_release(v4.root, settings=settings)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'consumer.db'}")
    config = dbsync.parse_config(
        {
            "version": 1,
            "tables": {
                "teams": {
                    "table": "teams",
                    "entity_type": "team",
                    "primary_key": "id",
                    "reep_id": "reep_id",
                    "status": "lookup_status",
                    "provider_ids": {"opta/team": "opta_id"},
                }
            },
        }
    )
    try:
        with engine.begin() as con:
            con.execute(
                sa.text(
                    "CREATE TABLE teams(id INTEGER PRIMARY KEY,opta_id TEXT,"
                    "reep_id TEXT,lookup_status TEXT,entity_lifecycle TEXT)"
                )
            )
            con.execute(
                sa.text(
                    "INSERT INTO teams VALUES(1,'TH',NULL,NULL,NULL),"
                    "(2,'TH',:rid,NULL,NULL)"
                ),
                {"rid": TEAM},
            )
        with Register.local(settings=settings) as reg:
            report = dbsync.sync(config, reg, apply=True, engine=engine)
            assert report.targets[0].outcomes["new"] == 1
            assert report.targets[0].outcomes["unchanged"] == 1
            assert reg.current(TEAM).current_status is None
        with engine.connect() as con:
            assert con.execute(
                sa.text(
                    "SELECT reep_id,lookup_status,entity_lifecycle "
                    "FROM teams ORDER BY id"
                )
            ).fetchall() == [(TEAM, "resolved", None), (TEAM, "resolved", None)]
    finally:
        engine.dispose()


def test_actual_site_download_contract_parser(v4: V4Bucket) -> None:
    import os
    import subprocess

    site = os.environ.get("REEP_SITE_ROOT")
    node = os.environ.get("REEP_TEST_NODE")
    if not site or not node:
        pytest.skip("set REEP_SITE_ROOT/REEP_TEST_NODE for actual site parser")
    run = subprocess.run(  # noqa: S603
        [
            node,
            "--import",
            str(Path(site) / "node_modules/tsx/dist/loader.mjs"),
            str(Path(__file__).parent / "support/downloads_site.mts"),
        ],
        input=json.dumps({"selection": v4.selected, "descriptor": v4.descriptor}),
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
        env={"PATH": "/usr/bin:/bin", "REEP_SITE_ROOT": site},
    )
    result = json.loads(run.stdout)
    assert result["channel"] == "downloads"
    assert set(result["files"]) == _v4.FILES
    with v4.client() as client:
        assert client.manifest().stamp == result["stamp"]


def test_cli_entity_schema_allows_truthful_null(v4: V4Bucket, tmp_path: Path) -> None:
    import jsonschema
    from click.testing import CliRunner

    from reep.cli.app import cli
    from reep.cli.schemas import ENTITY, ID_STATE

    settings = make_settings(tmp_path / "cache")
    import_release(v4.root, settings=settings)
    runner = CliRunner()
    shown = runner.invoke(
        cli, ["show", TEAM, "--db", str(settings.catalog_path), "--json"]
    )
    assert shown.exit_code == 0, shown.output
    entity = json.loads(shown.output)["data"]
    jsonschema.validate(entity, ENTITY)
    assert entity["status"] is None
    current = runner.invoke(
        cli, ["current", TEAM, "--db", str(settings.catalog_path), "--json"]
    )
    assert current.exit_code == 0, current.output
    result = json.loads(current.output)["data"]
    jsonschema.validate(result, ID_STATE)
    assert result["status"] == "published"
    assert result["current_status"] is None


def test_declared_download_size_is_a_write_bound(v4: V4Bucket, tmp_path: Path) -> None:
    with v4.client() as client:
        manifest = client.manifest()
        entry = manifest.table_file("entities")
        assert entry is not None
        v4.objects[entry.object_key] += b"extra bytes"
        dest = tmp_path / "entities.csv.gz"
        with pytest.raises(ChecksumMismatchError, match="exceeds"):
            client.download(entry, dest)
        assert not dest.exists()
        assert dest.with_name(dest.name + ".part").stat().st_size <= entry.size


def test_legacy_v4_transitions_snapshot_without_mixed_ordering(
    v4: V4Bucket, settings: Any, client: Any
) -> None:
    from tests.support.fixture_release import STAMP_2

    legacy = sync(client=client, settings=settings)
    assert legacy.stamp_after == STAMP_2
    with v4.client() as current:
        newer = sync(client=current, settings=settings)
        assert newer.method == "snapshot"
        assert newer.stamp_before == STAMP_2
        assert newer.stamp_after == STAMP
    returned = sync(client=client, settings=settings)
    assert returned.method == "snapshot"
    assert returned.stamp_after == STAMP_2
    assert "cross release schemas" in returned.notes[0]


def test_import_cannot_replace_a_bound_descriptor_at_the_same_stamp(
    v4: V4Bucket, tmp_path: Path
) -> None:
    settings = make_settings(tmp_path / "cache")
    import_release(v4.root, settings=settings)
    before = sha(settings.catalog_path.read_bytes())
    v4.descriptor["candidate_sha256"] = "d" * 64
    v4.publish_descriptor()
    with pytest.raises(UnsupportedReleaseError, match="same stamp"):
        import_release(v4.root, settings=settings)
    assert sha(settings.catalog_path.read_bytes()) == before


def test_unknown_version_cannot_fall_back_through_a_legacy_marker(
    v4: V4Bucket, client: Any
) -> None:
    from reep.release import ReleaseManifest

    legacy = client.manifest()
    contradictory = dict(legacy.raw)
    contradictory["schema"] = "reep-public-release/v999"
    with pytest.raises(UnsupportedReleaseError, match="versioned"):
        ReleaseManifest.from_json(contradictory)
    v4.objects["releases/latest.json"] = encode(
        {
            "schema": "reep-public-selection/v999",
            "schema_version": "reep-public-release-latest-v1",
            "stamp": "20261005T180536Z",
            "manifest_sha256": "a" * 64,
        }
    )
    with (
        v4.client() as current,
        pytest.raises(UnsupportedReleaseError, match="versioned"),
    ):
        current.latest()
