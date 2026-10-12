import re
import runpy
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

import pytest

import reep
from reep.cli import main


def test_version_matches_distribution_metadata() -> None:
    assert reep.__version__ == version("reep")


def test_citation_version_matches_package_version() -> None:
    citation = Path(__file__).parents[1] / "CITATION.cff"
    match = re.search(r"^version:\s*(\S+)\s*$", citation.read_text(), re.MULTILINE)
    assert match is not None
    assert match.group(1) == reep.__version__


def test_package_ships_type_information() -> None:
    assert files("reep").joinpath("py.typed").is_file()


def test_public_api_is_declared() -> None:
    assert {"Register", "ProviderId", "ReepError", "catalog", "__version__"} <= set(
        reep.__all__
    )
    for name in reep.__all__:
        assert hasattr(reep, name), name
    assert issubclass(reep.ReepError, Exception)


def test_cli_prints_where_to_get_the_register(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert f"reep {reep.__version__}" in out
    assert "https://reep.football/downloads" in out


def test_cli_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"reep {reep.__version__}"


def test_python_dash_m_runs_the_cli(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["reep"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("reep", run_name="__main__")
    assert exc.value.code == 0
    assert "reep.football" in capsys.readouterr().out
