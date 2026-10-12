# reep

[![CI](https://github.com/withqwerty/reep-python/actions/workflows/ci.yml/badge.svg)](https://github.com/withqwerty/reep-python/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/reep)](https://pypi.org/project/reep/)
[![Python versions](https://img.shields.io/pypi/pyversions/reep)](https://pypi.org/project/reep/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Python SDK and command-line tool for the [Reep Register](https://reep.football), the
open crosswalk of football provider IDs.

**Status: in development.** Version 0.0.1 reserves the package name and has no SDK
functions yet. The first working release is 0.1.0. The [development plan](docs/plan.md)
describes what it will do and in which order.

## Use the register today

- Download the complete release (CSV and DuckDB, CC0): <https://reep.football/downloads>
- Hosted lookups with an API key: <https://reep.football/api>

## What 0.1.0 will do

- Download and verify a dated release, and keep it current from the release changelogs.
- Resolve a provider ID, given as the full `(provider, namespace, external_id)` triple,
  to its Reep ID, and follow merge redirects.
- Translate an ID from one provider to another.
- Give AI agents a stable interface: JSON output with a fixed envelope, documented exit
  codes, and an MCP server.

Later releases add a guarded sync of Reep IDs into your own Postgres, MySQL or SQL Server
tables, with a report of the rows it could not resolve.

## Reporting a wrong ID

Data corrections (a wrong ID, a bad merge, a missing entity) go to
<https://reep.football/submit>. Issues in this repository are for the SDK and CLI code.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). This project follows the
[Contributor Covenant](CODE_OF_CONDUCT.md).

## Licences

The code in this repository is [MIT-licensed](LICENSE). The Reep Register data is
CC0-1.0.
