from __future__ import annotations

import io
import json
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import reep
from reep.backends import api as api_module
from reep.backends.api import ApiBackend
from reep.cli import main
from reep.cli.schemas import COMMANDS, envelope_schema, error_schema
from reep.config import Settings
from tests.conftest import no_sleep
from tests.support.fake_api import BASE, VALID_KEY, FakeApi
from tests.support.fixture_release import (
    MERGED,
    PALMER,
    STAMP_1,
    STAMP_2,
    UNKNOWN,
    FixtureBucket,
)
from tests.support.httpserver import FileServer


class Cli:
    """Run the CLI in-process and check its JSON output against the schemas."""

    def __init__(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.capsys = capsys
        self.monkeypatch = monkeypatch

    def __call__(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        if stdin is not None:
            self.monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = main(list(args))
        out, err = self.capsys.readouterr()
        if "--json" in args and code in (0, 3) and out.strip():
            doc = json.loads(out)
            command = doc["command"]
            jsonschema.validate(doc, envelope_schema(command))
        if ("--json" in args or "--jsonl" in args) and err.strip() and code not in (0,):
            jsonschema.validate(json.loads(err), error_schema())
        return code, out, err

    def json(self, *args: str, stdin: str | None = None) -> tuple[int, dict[str, Any]]:
        code, out, err = self(*args, "--json", stdin=stdin)
        assert out.strip(), err
        return code, json.loads(out)


@pytest.fixture
def cli(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    store_s1: Settings,
) -> Cli:
    monkeypatch.setenv("REEP_CACHE_DIR", str(store_s1.cache_dir))
    monkeypatch.setenv("REEP_CONFIG_DIR", str(store_s1.config_dir))
    monkeypatch.setenv("REEP_RELEASES_URL", store_s1.releases_url)
    monkeypatch.setattr("reep.auth._keyring", lambda: None)
    return Cli(capsys, monkeypatch)


@pytest.fixture
def empty_cli(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    server: FileServer,
) -> Cli:
    monkeypatch.setenv("REEP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("REEP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("REEP_RELEASES_URL", server.url)
    monkeypatch.setattr("reep.auth._keyring", lambda: None)
    return Cli(capsys, monkeypatch)


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()

    class TestBackend(ApiBackend):
        def __init__(self, key: str, **kwargs: Any) -> None:
            kwargs["http"] = fake.client()
            kwargs["sleep"] = no_sleep
            super().__init__(key, **kwargs)

    monkeypatch.setattr("reep.register.ApiBackend", TestBackend)
    monkeypatch.setenv("REEP_BASE_URL", BASE)
    return fake


def test_no_arguments_prints_help(cli: Cli) -> None:
    code, out, _ = cli()
    assert code == 0
    assert "reep catalog sync" in out


def test_version(cli: Cli) -> None:
    code, out, _ = cli("--version")
    assert code == 0
    assert out.strip() == f"reep {reep.__version__}"


def test_help_has_examples(cli: Cli) -> None:
    for command in ("resolve", "current", "translate", "search", "show", "providers"):
        code, out, _ = cli(command, "--help")
        assert code == 0
        assert "Examples:" in out


def test_status(cli: Cli, server: FileServer) -> None:
    code, out, _ = cli("status")
    assert code == 0
    assert STAMP_1 in out
    code, doc = cli.json("status", "--check-latest")
    assert code == 0
    assert doc["backend"] == "local"
    assert doc["release_stamp"] == STAMP_1
    assert doc["data"]["catalog"]["profile"] == "slim"
    assert doc["data"]["latest_stamp"] == STAMP_2
    assert doc["data"]["update_available"] is True
    assert doc["data"]["api"]["key_source"] is None


def test_status_without_a_catalogue(empty_cli: Cli) -> None:
    code, doc = empty_cli.json("status")
    assert code == 0
    assert doc["backend"] is None
    assert doc["data"]["catalog"] is None


def test_resolve(cli: Cli) -> None:
    code, out, _ = cli("resolve", "transfermarkt", "spieler", "568177")
    assert code == 0
    assert PALMER in out
    code, doc = cli.json("resolve", "transfermarkt/spieler/568177")
    assert code == 0
    assert doc["data"]["reep_id"] == PALMER
    assert doc["backend"] == "local"
    assert doc["truncated"] is False


def test_resolve_not_found_and_withdrawn_exit_3(cli: Cli) -> None:
    code, doc = cli.json("resolve", "transfermarkt", "spieler", "1")
    assert code == 3
    assert doc["data"]["status"] == "not_found"
    code, doc = cli.json("resolve", "wyscout/player/444")
    assert code == 3
    assert doc["data"]["status"] == "withdrawn"


def test_resolve_usage_errors_exit_2(cli: Cli) -> None:
    code, _, err = cli("resolve", "transfermarkt/player/568177", "--json")
    assert code == 2
    error = json.loads(err)["error"]
    assert error["code"] == "invalid_namespace"
    assert "spieler" in error["hint"]
    code, _, err = cli("resolve", "transfermarkt", "568177")
    assert code == 2
    assert "three parts" in err
    code, _, err = cli("resolve")
    assert code == 2
    code, _, err = cli("resolve", "x/y/z", "--nope", "--json")
    assert code == 2
    assert json.loads(err)["error"]["code"] == "usage_error"


def test_resolve_many_and_stdin(cli: Cli) -> None:
    code, doc = cli.json(
        "resolve", "transfermarkt/spieler/568177", "whoscored/player/333"
    )
    assert code == 0
    assert [r["reep_id"] for r in doc["data"]] == [PALMER, PALMER]
    assert doc["data"][1]["redirected_from"] == MERGED
    code, out, _ = cli(
        "resolve",
        "-",
        "--jsonl",
        stdin="transfermarkt/spieler/568177\n\ntransfermarkt,spieler,1\n",
    )
    assert code == 0
    rows = [json.loads(line) for line in out.splitlines()]
    assert [r["status"] for r in rows] == ["found", "not_found"]
    code, out, _ = cli("resolve", "-", stdin="transfermarkt/spieler/568177\n")
    assert code == 0
    assert "QUERY" in out


def test_current(cli: Cli) -> None:
    code, doc = cli.json("current", MERGED)
    assert code == 0
    assert doc["data"]["status"] == "redirected"
    assert doc["data"]["current_id"] == PALMER
    code, doc = cli.json("current", UNKNOWN)
    assert code == 3
    code, doc = cli.json("current", PALMER, MERGED)
    assert code == 0
    assert len(doc["data"]) == 2
    code, out, _ = cli("current", "-", stdin=f"{PALMER}\n{UNKNOWN}\n")
    assert code == 0
    assert "unknown" in out
    code, _, err = cli("current", "568177")
    assert code == 2
    assert "not a Reep ID" in err


def test_translate(cli: Cli) -> None:
    code, out, _ = cli(
        "translate", "transfermarkt", "spieler", "568177", "--to", "fbref"
    )
    assert code == 0
    assert "fbref/person/dc7f8a28" in out
    code, doc = cli.json("translate", "transfermarkt/spieler/568177", "--to", "fbref")
    assert doc["data"]["targets"][0]["external_id"] == "dc7f8a28"
    code, doc = cli.json(
        "translate", "transfermarkt/spieler/568177", "--to", "skillcorner"
    )
    assert code == 3
    assert doc["data"]["targets"] == []
    code, _, _ = cli("translate", "transfermarkt/spieler/568177")
    assert code == 2
    code, _, _ = cli("translate", "a/b/c", "d/e/f", "--to", "fbref")
    assert code == 2


def test_search(cli: Cli) -> None:
    code, out, _ = cli("search", "Cole", "Palmer")
    assert code == 0
    assert "Cole Palmer" in out
    code, doc = cli.json("search", "chelsea", "--limit", "1")
    assert code == 0
    assert doc["truncated"] is True
    assert doc["next_offset"] == 1
    code, out, err = cli("search", "chelsea", "--limit", "1")
    assert "--offset 1" in err
    code, doc = cli.json("search", "zzzz")
    assert code == 3
    assert doc["data"] == []
    code, _, _ = cli("search", "chelsea", "--type", "club")
    assert code == 2


def test_show(cli: Cli) -> None:
    code, out, _ = cli("show", PALMER)
    assert code == 0
    assert "transfermarkt/spieler/568177" in out
    code, doc = cli.json("show", MERGED)
    assert doc["data"]["reep_id"] == PALMER
    code, doc = cli.json("show", MERGED, "--no-follow")
    assert doc["data"]["reep_id"] == MERGED
    code, _, err = cli("show", UNKNOWN, "--json")
    assert code == 3
    assert json.loads(err)["error"]["code"] == "not_found"


def test_providers(cli: Cli) -> None:
    code, doc = cli.json("providers", "--provider", "transfermarkt")
    assert code == 0
    assert {r["namespace"] for r in doc["data"]} == {
        "schiedsrichter",
        "spieler",
        "trainer",
        "verein",
    }
    code, doc = cli.json("providers", "--limit", "2")
    assert doc["truncated"] is True
    assert doc["next_offset"] == 2
    code, out, _ = cli("providers")
    assert "PROVIDER" in out
    code, _, err = cli("providers", "--provider", "transfermarket", "--json")
    assert code == 2
    assert "transfermarkt" in json.loads(err)["error"]["message"]


def test_db_option_reads_a_shipped_file(cli: Cli, bucket: FixtureBucket) -> None:
    shipped = bucket.release_dir(STAMP_2) / "duckdb" / "reep-register-v1.duckdb"
    code, doc = cli.json("resolve", "understat/player/u1", "--db", str(shipped))
    assert code == 0
    assert doc["release_stamp"] == STAMP_2


def test_catalog_sync_and_import(
    empty_cli: Cli, bucket: FixtureBucket, tmp_path: Path
) -> None:
    code, doc = empty_cli.json("catalog", "sync", "--stamp", STAMP_1)
    assert code == 0
    assert doc["data"]["method"] == "snapshot"
    code, doc = empty_cli.json("catalog", "sync")
    assert doc["data"]["method"] == "replay"
    code, doc = empty_cli.json("catalog", "sync")
    assert doc["data"]["method"] == "up_to_date"
    code, out, _ = empty_cli("catalog", "sync", "--method", "snapshot", "--force")
    assert code == 0
    assert "snapshot" in out
    source = tmp_path / "files"
    shutil.copytree(bucket.release_dir(STAMP_1) / "csv", source)
    shutil.copyfile(
        bucket.release_dir(STAMP_1) / "release.json", source / "release.json"
    )
    code, doc = empty_cli.json("catalog", "import", str(source), "--with-aliases")
    assert code == 0
    assert doc["data"]["method"] == "import"
    assert doc["release_stamp"] == STAMP_1
    code, out, _ = empty_cli("catalog", "import", str(source))
    assert "import" in out


def test_no_catalogue_and_no_key_exit_7(empty_cli: Cli) -> None:
    code, _, err = empty_cli("resolve", "transfermarkt/spieler/568177", "--json")
    assert code == 7
    assert json.loads(err)["error"]["code"] == "catalog_missing"


def test_network_error_exit_6(empty_cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REEP_RELEASES_URL", "http://127.0.0.1:9")
    monkeypatch.setattr("reep.release.time.sleep", no_sleep)
    code, _, err = empty_cli("catalog", "sync", "--json")
    assert code == 6
    assert json.loads(err)["error"]["code"] == "network_error"


def test_data_error_exit_7(cli: Cli, tmp_path: Path) -> None:
    import duckdb

    path = tmp_path / "other.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t (x INTEGER)")
    con.close()
    code, _, err = cli("resolve", "a/b/c", "--db", str(path), "--json")
    assert code == 7
    assert json.loads(err)["error"]["code"] == "unsupported_release"


def test_internal_error_exit_1(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("reep.register.Register.resolve_many", broken)
    code, _, err = cli("resolve", "transfermarkt/spieler/568177", "--json")
    assert code == 1
    error = json.loads(err)["error"]
    assert error["code"] == "internal_error"
    assert "boom" in error["message"]
    code, _, err = cli("resolve", "transfermarkt/spieler/568177")
    assert "This is a bug" in err


def test_interrupt_exit_130(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("reep.register.Register.resolve_many", interrupted)
    code, _, _ = cli("resolve", "transfermarkt/spieler/568177")
    assert code == 130


def test_api_backend_through_the_cli(
    cli: Cli, fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REEP_API_KEY", VALID_KEY)
    code, doc = cli.json("resolve", "transfermarkt/spieler/568177", "--api")
    assert code == 0
    assert doc["backend"] == "api"
    assert doc["data"]["reep_id"] == PALMER
    code, doc = cli.json("providers", "--api", "--provider", "transfermarkt")
    assert doc["data"][0]["namespace"] is None


def test_api_is_chosen_when_there_is_no_catalogue(
    empty_cli: Cli, fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REEP_API_KEY", VALID_KEY)
    code, doc = empty_cli.json("current", MERGED)
    assert code == 0
    assert doc["backend"] == "api"
    code, doc = empty_cli.json("status")
    assert doc["data"]["backend"] == "api"
    assert doc["data"]["api"]["key_source"] == "env"


def test_auth_flow(empty_cli: Cli, fake_api: FakeApi, tmp_path: Path) -> None:
    code, _, err = empty_cli("auth", "status", "--json")
    assert code == 5
    assert json.loads(err)["error"]["code"] == "auth_error"
    code, _, err = empty_cli("auth", "login")
    assert code == 2
    assert "--key-stdin" in err
    code, _, err = empty_cli(
        "auth", "login", "--key-stdin", stdin="bad-key-0000000000\n"
    )
    assert code == 5
    code, doc = empty_cli.json("auth", "login", "--key-stdin", stdin=f"{VALID_KEY}\n")
    assert code == 0
    assert doc["data"]["verified"] is True
    stored = Path(doc["data"]["stored_in"])
    assert stored.is_file()
    code, doc = empty_cli.json("auth", "status")
    assert code == 0
    assert doc["data"]["key_source"] == "file"
    assert doc["data"]["key"] == f"…{VALID_KEY[-4:]}"
    assert VALID_KEY not in json.dumps(doc)
    assert doc["data"]["plan"] == "evaluation"
    code, out, _ = empty_cli("auth", "status")
    assert "evaluation" in out
    code, doc = empty_cli.json("auth", "logout")
    assert doc["data"]["removed_from"] == [str(stored)]
    code, out, _ = empty_cli("auth", "logout")
    assert "No stored key" in out
    code, _, _ = empty_cli("auth", "login", "--key-stdin", stdin="   \n")
    assert code == 2
    code, out, _ = empty_cli(
        "auth", "login", "--key-stdin", "--no-verify", stdin="k" * 20
    )
    assert code == 0
    assert "stored" in out


def test_auth_login_prompts_on_a_terminal(
    empty_cli: Cli, fake_api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("reep.cli.app.is_interactive", lambda: True)
    monkeypatch.setattr("click.prompt", lambda *a, **k: VALID_KEY)
    code, out, _ = empty_cli("auth", "login", "--no-keyring")
    assert code == 0
    assert "Key stored" in out


def test_schema(cli: Cli) -> None:
    code, out, _ = cli("schema")
    assert code == 0
    listing = json.loads(out)
    assert "resolve" in listing["commands"]
    for command in COMMANDS:
        code, out, _ = cli("schema", *command.split())
        assert code == 0
        jsonschema.Draft202012Validator.check_schema(json.loads(out))
    code, _, _ = cli("schema", "nope")
    assert code == 2


def test_verbose_flag(cli: Cli) -> None:
    code, _, _ = cli("-v", "status")
    assert code == 0


def test_api_module_constants() -> None:
    assert api_module.MAX_RESOLVE_BATCH == 20
