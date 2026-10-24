"""Actual v4 exporter bytes with explicitly synthetic site publication references."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

import duckdb
import httpx

from reep import _historical_v4, _v4
from reep.release import ReleaseClient

STAMP = "2026-10-23.1"


def unpack(root: Path) -> None:
    archive = Path(__file__).parents[1] / "fixtures/v4-download.tar.gz"
    receipt = json.loads(archive.with_name("v4-download-provenance.json").read_text())
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == receipt["archive_sha256"]
    root.mkdir()
    with tarfile.open(archive) as handle:
        handle.extractall(root, filter="data")
    for name, expected in receipt["files"].items():
        raw = (root / name).read_bytes()
        assert len(raw) == expected["bytes"]
        assert hashlib.sha256(raw).hexdigest() == expected["sha256"]


def encode(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class V4Bucket:
    descriptor: dict[str, Any]
    selected: dict[str, Any]

    def __init__(self, root: Path) -> None:
        unpack(root)
        self.root = root
        self.calls: list[str] = []
        self.objects: dict[str, bytes] = {}
        self.rebuild()

    def rebuild(self, stamp: str = STAMP) -> None:
        # The controlled selector is synthetic contract input; no qualification
        # flag is changed and the real publisher would still refuse this fixture.
        self.stamp = stamp
        source = json.loads((self.root / "manifest.json").read_text())
        source["release_tag"] = stamp
        source["release_date"] = stamp.split(".")[0]
        for name in _v4.FILES - {
            "manifest.json",
            "latest.json",
            "upkeep_ledger.json",
            "site-summary.json",
        }:
            source["checksums_sha256"][name] = sha((self.root / name).read_bytes())
        (self.root / "manifest.json").write_bytes(encode(source))
        files = {}
        for name in sorted(_v4.FILES):
            raw = (self.root / name).read_bytes()
            key = f"releases/{stamp}/objects/synthetic-contract/{name}"
            self.objects[key] = raw
            files[name] = {"key": key, "sha256": sha(raw), "bytes": len(raw)}
        self.descriptor = {
            "schema": _v4.SCHEMA,
            "channel": "downloads",
            "stamp": stamp,
            "candidate_sha256": "a" * 64,
            "policy_sha256": "b" * 64,
            "source": {
                "generation_id": "synthetic-sdk-fixture",
                "manifest_sha256": "c" * 64,
                "as_of": None,
            },
            "generated_at": "2026-10-23T12:00:00Z",
            "source_manifest_sha256": files["manifest.json"]["sha256"],
            "files": files,
        }
        self.publish_descriptor()

    def publish_descriptor(self) -> None:
        raw = encode(self.descriptor)
        key = f"releases/{self.stamp}/release.json"
        self.objects[key] = raw
        (self.root / "release.json").write_bytes(raw)
        self.selected = {
            "schema": _v4.SELECTOR,
            "channel": "downloads",
            "stamp": self.stamp,
            "candidate_sha256": self.descriptor["candidate_sha256"],
            "policy_sha256": self.descriptor["policy_sha256"],
            "generated_at": self.descriptor["generated_at"],
            "activation_requested_at": "2026-10-23T12:01:00Z",
            "predecessor_sha256": None,
            "release": {"key": key, "sha256": sha(raw), "bytes": len(raw)},
        }
        self.objects["releases/latest.json"] = encode(self.selected)
        self.objects["pointer.json"] = encode(
            {"channel": "api", "stamp": "2026-10-23.99"}
        )

    def transport(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip("/")
        self.calls.append(key)
        assert key != "pointer.json", "Download client read API selector"
        raw = self.objects.get(key)
        return httpx.Response(
            404 if raw is None else 200, content=raw or b"", request=request
        )

    def client(self) -> ReleaseClient:
        return ReleaseClient(
            "https://fixture",
            http=httpx.Client(transport=httpx.MockTransport(self.transport)),
            retries=0,
        )


class HistoricalV4Bucket:
    """Self-contained fixture for the retained v4_physical_views layout."""

    descriptor: dict[str, Any]
    selected: dict[str, Any]

    def __init__(self, root: Path) -> None:
        unpack(root)
        self.root = root
        self.calls: list[str] = []
        self.objects: dict[str, bytes] = {}
        self._convert_layout()
        self.rebuild()

    def _convert_layout(self) -> None:
        csv_dir = self.root / "csv"
        csv_dir.mkdir(exist_ok=True)
        duck_dir = self.root / "duckdb"
        duck_dir.mkdir(exist_ok=True)
        for unused in ("entity_search.csv.gz", "entity_search.parquet", "latest.json"):
            (self.root / unused).unlink(missing_ok=True)
        for table in _historical_v4.TABLES:
            src = self.root / f"{table}.csv.gz"
            dest = csv_dir / f"{table}.csv.gz"
            if src.exists():
                src.replace(dest)
        self._trim_matches_columns()
        self._sync_aliases_and_duckdb()

    def _trim_matches_columns(self) -> None:
        path = self.root / "csv/matches.csv.gz"
        with gzip.open(path, "rt", newline="") as f:
            reader = csv.DictReader(f)
            rows = [
                {col: row.get(col, "") for col in _historical_v4.MATCH_COLUMNS}
                for row in reader
            ]
        out = io.StringIO(newline="")
        writer = csv.DictWriter(
            out, fieldnames=_historical_v4.MATCH_COLUMNS, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
        path.write_bytes(gzip.compress(out.getvalue().encode(), mtime=0))
        parquet_path = self.root / "matches.parquet"
        with duckdb.connect(":memory:") as con:
            cols = ", ".join(_historical_v4.MATCH_COLUMNS)
            con.execute(
                f"COPY (SELECT {cols} FROM read_parquet(?)) TO ? (FORMAT PARQUET)",
                [str(parquet_path), str(parquet_path)],
            )

    def _sync_aliases_and_duckdb(self) -> None:
        csv_dir = self.root / "csv"
        for alias, canonical in _historical_v4.ALIASES.items():
            shutil.copyfile(
                csv_dir / f"{canonical}.csv.gz", csv_dir / f"{alias}.csv.gz"
            )
        db_path = self.root / "reep-register.duckdb"
        db_path.unlink(missing_ok=True)
        with duckdb.connect(str(db_path)) as con:
            for table in (
                "bridges",
                "edges",
                "entities",
                "overlay_links",
                "provider_aliases",
                "redirects",
            ):
                con.execute(
                    f"CREATE TABLE {table} AS SELECT * FROM read_csv(?, "
                    "header = true, all_varchar = true)",
                    [str(csv_dir / f"{table}.csv.gz")],
                )
            con.execute(
                "CREATE TABLE _entity_search_idx AS "
                "SELECT reep_id, canonical_name AS alias_blob, "
                "1::BIGINT AS bridge_count, 0::BIGINT AS alias_count FROM entities"
            )
            con.execute(
                "CREATE TABLE _match_dates AS "
                "SELECT reep_id, match_date_iso FROM read_csv(?, "
                "header = true, all_varchar = true)",
                [str(csv_dir / "matches.csv.gz")],
            )
            con.execute(
                "CREATE TABLE _overlay_aliases_raw AS "
                "SELECT reep_id, source, qid, name, language, name_source, "
                "TRY_CAST(confidence AS FLOAT) AS confidence, "
                "TRY_CAST(link_confidence AS FLOAT) AS link_confidence, dob_check "
                "FROM read_csv(?, header = true, all_varchar = true)",
                [str(csv_dir / "overlay_aliases.csv.gz")],
            )
            con.execute(
                "CREATE VIEW entity_search AS "
                "SELECT e.reep_id, e.entity_type, e.canonical_name AS label, "
                "e.canonical_name, e.canonical_name AS search_text, "
                "1::BIGINT AS bridge_count, 0::BIGINT AS alias_count FROM entities e"
            )
            cols = ", ".join(_historical_v4.MATCH_COLUMNS)
            con.execute(
                f"CREATE VIEW matches AS SELECT {cols} FROM ("
                "SELECT e.reep_id, e.canonical_name AS fixture_label, e.gender, "
                "e.country, m.match_date_iso, e.grade, e.bucket_count, "
                "e.corroboration_buckets, e.metadata_json "
                "FROM entities e JOIN _match_dates m USING (reep_id))"
            )
            con.execute(
                "CREATE VIEW overlay_aliases AS "
                "SELECT reep_id, source, qid, name, name AS alias, language, "
                "language AS lang, name_source, confidence, link_confidence, "
                "dob_check, NULL::VARCHAR AS evidence, NULL::VARCHAR AS created_at "
                "FROM _overlay_aliases_raw"
            )
        shutil.copyfile(db_path, self.root / "duckdb/reep-register-v1.duckdb")

    def rebuild(self, stamp: str = STAMP) -> None:
        self._sync_aliases_and_duckdb()
        self.stamp = stamp
        source = json.loads((self.root / "manifest.json").read_text())
        source["release_tag"] = stamp
        source["release_date"] = stamp.split(".")[0]
        counts: dict[str, int] = {}
        for table in _historical_v4.TABLES:
            with gzip.open(self.root / f"csv/{table}.csv.gz", "rt", newline="") as f:
                counts[table] = sum(1 for _ in csv.DictReader(f))
            source[_v4.COUNT_KEYS[table]] = counts[table]
            source["checksums_sha256"][f"{table}.parquet"] = sha(
                (self.root / f"{table}.parquet").read_bytes()
            )
            # Non-deterministic gzip streams differ between manifests.
            source["checksums_sha256"][f"{table}.csv.gz"] = "f" * 64
        for name in (
            "reep-register.duckdb",
            "upkeep_ledger.json",
            "site-summary.json",
        ):
            source["checksums_sha256"][name] = sha((self.root / name).read_bytes())
        (self.root / "manifest.json").write_bytes(encode(source))
        (self.root / "checksums.txt").write_text("historical checksums\n")
        files: dict[str, dict[str, Any]] = {}
        row_counts: dict[str, int] = {}
        for name in sorted(_historical_v4.FILES):
            raw = (self.root / name).read_bytes()
            key = f"releases/{stamp}/{name}"
            self.objects[key] = raw
            entry: dict[str, Any] = {
                "object_key": key,
                "sha256": sha(raw),
                "bytes": len(raw),
            }
            table = (
                name.removeprefix("csv/")
                .removesuffix(".csv.gz")
                .removesuffix(".parquet")
            )
            if table in {*_historical_v4.TABLES, *_historical_v4.ALIASES}:
                rows = counts[_historical_v4.ALIASES.get(table, table)]
                entry["rows"] = rows
                row_counts[name] = rows
                if name.startswith("csv/"):
                    entry["role"] = (
                        "overlay" if table.startswith("overlay_") else "canonical_csv"
                    )
            files[name] = entry
        self.descriptor = {
            "schema_version": "bridge-register-v1",
            "manifest_version": 1,
            "projection_mode": _historical_v4.MODE,
            "tier": "public_bridge_v1",
            "stamp": stamp,
            "release_tag": stamp,
            "generated_at": "2026-10-23T09:24:09Z",
            "counts": counts,
            "row_counts": row_counts,
            "files": files,
        }
        self.publish_descriptor()

    def publish_descriptor(self) -> None:
        raw = encode(self.descriptor)
        key = f"releases/{self.stamp}/release.json"
        self.objects[key] = raw
        (self.root / "release.json").write_bytes(raw)
        self.selected = {
            "schema_version": "reep-public-release-latest-v1",
            "release_schema_version": "bridge-register-v1",
            "release_tier": "public_bridge_v1",
            "stamp": self.stamp,
            "release_tag": self.stamp,
            "generated_at": self.descriptor["generated_at"],
            "manifest_file": "manifest.json",
            "manifest_object_key": key,
            "manifest_sha256": sha(raw),
            "files": ["entities.csv", "entities.csv.gz"],
        }
        self.objects["releases/latest.json"] = encode(self.selected)

    def transport(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip("/")
        self.calls.append(key)
        raw = self.objects.get(key)
        return httpx.Response(
            404 if raw is None else 200, content=raw or b"", request=request
        )

    def client(self) -> ReleaseClient:
        return ReleaseClient(
            "https://fixture",
            http=httpx.Client(transport=httpx.MockTransport(self.transport)),
            retries=0,
        )
