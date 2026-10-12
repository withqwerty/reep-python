# AGENTS.md

Instructions for coding agents that work in this repository. Human contributors can read
[CONTRIBUTING.md](CONTRIBUTING.md) instead; the rules are the same.

## Orientation

- `docs/plan.md` is the development plan: architecture, public API, the agent contract
  for the CLI and MCP server, and the units of work. Read the section for your unit
  before you start.
- Source is in `src/reep/`, tests in `tests/`.

## Commands

```bash
uv sync                  # set up .venv
uv run ruff format .     # format
uv run ruff check .      # lint
uv run mypy              # strict type check
uv run pytest --cov      # tests (network tests are deselected by default)
uv build                 # package
```

Run all of them before you commit. A change is complete when they pass.

## Rules

- Provider IDs are the triple `(provider, namespace, external_id)`. Do not join or look
  up on a bare external ID.
- Keep the public API in `__all__`; private modules start with an underscore.
- Library code raises `ReepError` subclasses, logs through `logging`, and does not print.
- The CLI output envelope, error codes and exit codes in `docs/plan.md` section 7 are a
  public contract. Change them only together with the plan and the changelog.
- Tests must not reach the network unless they carry the `network` marker.
- Add a changelog entry under "Unreleased" for each user-visible change.
- Use British English in prose. Code identifiers follow the existing names.
- Do not put personal data (for example an email address) in HTTP headers or logs.
