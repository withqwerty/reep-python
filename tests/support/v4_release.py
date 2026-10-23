"""Actual v4 exporter bytes with explicitly synthetic site publication references."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path
from typing import Any

import httpx

from reep import _v4
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
