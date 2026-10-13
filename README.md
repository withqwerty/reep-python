# reep

[![CI](https://github.com/withqwerty/reep-python/actions/workflows/ci.yml/badge.svg)](https://github.com/withqwerty/reep-python/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/reep)](https://pypi.org/project/reep/)
[![Python versions](https://img.shields.io/pypi/pyversions/reep)](https://pypi.org/project/reep/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Python SDK and command-line tool for the [Reep Register](https://reep.football), the
open crosswalk of football provider IDs.

```bash
pip install reep            # or: uv tool install reep
reep catalog sync           # download the register (about 210 MB, no key)
reep resolve transfermarkt spieler 568177
reep translate transfermarkt/spieler/568177 --to fbref
```

```python
import reep

reep.catalog.sync()
reg = reep.Register.local()
res = reg.resolve("transfermarkt/spieler/568177")
print(res.status, res.reep_id, res.label)  # found rp53af22bbeaa667 Cole Jermaine Palmer
```

## What it does

- Downloads the dated public release and checks every file against its SHA-256.
- Keeps the local copy current by replaying the release changelogs, and downloads a
  snapshot when it cannot.
- Resolves a provider ID, given as the full `(provider, namespace, external_id)`
  triple, to its current Reep ID. Merged IDs resolve to their survivor; withdrawn IDs
  return a status, never an empty ID.
- Translates an ID from one provider to another, and adds Reep IDs to pandas, polars
  or pyarrow tables.
- Fills Reep IDs and other providers' IDs into your own PostgreSQL, MySQL, SQL Server
  or SQLite tables (`reep sync`). It runs as a dry run unless you add `--apply`, writes
  only to empty cells, and lists the rows that need review (`reep review`).
- Reads the SDK catalogue, release CSVs you already have, the release DuckDB file
  (`REEP_DUCKDB_PATH`), or the hosted API with a key (`REEP_API_KEY`).
- Gives AI agents a stable interface: `--json` output with a versioned envelope, JSON
  errors with hints, fixed exit codes, a skill file (`reep skill install`) and a
  read-only MCP server (`reep mcp`, extra `reep[mcp]`).

Matching is exact. The package does not match records by name: the public release has
no dates of birth, so name matching gives unreliable results.

## Documentation

The documentation is at <https://reep.football/docs/python/> (source in
[docs/](docs/index.md)): a quickstart, the concepts (provider ID
triples, stored-ID states, releases), the data sources, the database sync, the command
line and the Python API. Build it with `uv run --group docs sphinx-build docs docs/_build/html`.

## Reporting a wrong ID

Data corrections (a wrong ID, a bad merge, a missing entity) go to
<https://reep.football/submit>. Issues in this repository are for the SDK and CLI code.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). This project follows the
[Contributor Covenant](CODE_OF_CONDUCT.md). The [development plan](docs/plan.md)
shows the units of work.

## Licences

The code in this repository is [MIT-licensed](LICENSE). The Reep Register data is
CC0-1.0.
