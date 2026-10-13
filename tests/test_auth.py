from __future__ import annotations

import json
import stat
import sys
import types
from pathlib import Path

import pytest

from reep import auth
from reep.config import Settings
from tests.conftest import make_settings


class FakeKeyring(types.ModuleType):
    def __init__(self, *, broken: bool = False) -> None:
        super().__init__("keyring")
        self.store: dict[tuple[str, str], str] = {}
        self.broken = broken

    def get_password(self, service: str, user: str) -> str | None:
        if self.broken:
            raise RuntimeError("no backend")
        return self.store.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        if self.broken:
            raise RuntimeError("no backend")
        self.store[service, user] = value

    def delete_password(self, service: str, user: str) -> None:
        if self.broken:
            raise RuntimeError("no backend")
        del self.store[service, user]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


def test_file_storage(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    assert auth.load_key(settings) is None
    assert auth.key_source(settings) is None
    where = auth.save_key("  secret-key-123456  ", settings)
    path = Path(where)
    assert json.loads(path.read_text()) == {"api_key": "secret-key-123456"}
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth.load_key(settings) == "secret-key-123456"
    assert auth.key_source(settings) == "file"
    assert auth.delete_key(settings) == [where]
    assert auth.load_key(settings) is None


def test_environment_key_comes_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    settings = Settings(
        api_key="env-key-0000000000",
        duckdb_path=None,
        cache_dir=tmp_path,
        config_dir=tmp_path,
    )
    auth.save_key("file-key-000000000", settings)
    assert auth.load_key(settings) == "env-key-0000000000"
    assert auth.key_source(settings) == "env"


def test_keyring_storage(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert auth.save_key("ring-key-000000000", settings) == "keyring"
    assert auth.load_key(settings) == "ring-key-000000000"
    assert auth.key_source(settings) == "keyring"
    assert auth.delete_key(settings) == ["keyring"]
    assert auth.load_key(settings) is None


def test_keyring_failures_fall_back_to_the_file(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring(broken=True))
    where = auth.save_key("file-key-000000000", settings)
    assert where != "keyring"
    assert auth.load_key(settings) == "file-key-000000000"
    assert auth.delete_key(settings) == [where]


def test_no_keyring_option(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    where = auth.save_key("file-key-000000000", settings, use_keyring=False)
    assert where.endswith("credentials.json")
    assert fake.store == {}


def test_keyring_delete_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeKeyring()
    fake.store[auth.KEYRING_SERVICE, auth.KEYRING_USERNAME] = "x" * 20

    def refuse(service: str, user: str) -> None:
        raise RuntimeError("locked")

    fake.delete_password = refuse  # type: ignore[method-assign]
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert auth.delete_key(settings) == []


def test_damaged_credentials_file(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    path = settings.config_dir / "credentials.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert auth.load_key(settings) is None
    path.write_text(json.dumps(["x"]))
    assert auth.load_key(settings) is None


def test_missing_keyring_module(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)
    assert auth._keyring() is None
    assert auth.load_key(settings) is None


def test_mask() -> None:
    assert auth.mask("abcdefghijklmnop") == "…mnop"
    assert auth.mask("short") == "…"
