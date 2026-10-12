# Contributing to reep

Thank you for your interest in the project. This guide explains how to set up a
development environment, which checks a change must pass, and how releases work.

## Before you start

- **Data corrections are not code changes.** A wrong ID, a bad merge or a missing entity
  goes to <https://reep.football/submit>.
- **Large changes:** open an issue first, so that we can agree on the approach before you
  write the code. The [development plan](docs/plan.md) shows what is planned.
- **Security issues:** follow [SECURITY.md](SECURITY.md). Do not open a public issue.

## Development set-up

You need [uv](https://docs.astral.sh/uv/) 0.13 or later.

```bash
git clone https://github.com/withqwerty/reep-python.git
cd reep-python
uv sync                       # creates .venv with the dev dependency group
uv run pre-commit install     # optional: runs the format and lint hooks on commit
```

## Checks

Run these before you open a pull request. CI runs the same checks.

```bash
uv run ruff format .          # format
uv run ruff check .           # lint
uv run mypy                   # strict type check
uv run pytest --cov           # tests with coverage (90 % minimum)
uv build                      # sdist and wheel
```

Tests that call the live Reep release or API carry the `network` marker and do not run by
default. Run them with `uv run pytest -m network`.

## Code standards

- Format with ruff; the lint rules are in `pyproject.toml`.
- Add type hints to all code; `mypy --strict` must pass.
- Write Google-style docstrings for public modules, classes and functions.
- Raise subclasses of `reep.ReepError` from library code.
- Library code logs through `logging.getLogger(__name__)` and does not print.
- Add or update tests for each behaviour change.
- Add an entry under "Unreleased" in [CHANGELOG.md](CHANGELOG.md) for each user-visible
  change.

## Pull requests

- Keep each pull request to one change.
- Write commit messages in the imperative mood ("Add translate command").
- Describe what changed and how you tested it; the pull request template lists the
  points.

## Releases

Maintainers release from `main`:

1. Move the "Unreleased" changelog entries to the new version and set `version` in
   `pyproject.toml`.
2. Commit, then push a tag `vX.Y.Z`.
3. The release workflow builds the package, checks that the tag matches the version,
   publishes to PyPI through Trusted Publishing, and creates a GitHub release.

The project uses [Semantic Versioning](https://semver.org/). Before 1.0, a minor release
can contain breaking changes; the changelog lists them.
