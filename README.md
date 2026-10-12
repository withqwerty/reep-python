# reep

Python SDK and command-line tool for the [Reep Register](https://reep.football), the
open crosswalk of football provider IDs.

**Status: in development.** This 0.0.1 release reserves the package name. It has no
SDK functions yet. The first working release will be 0.1.0.

## Use the register today

- Download the complete release (CSV and DuckDB, CC0): <https://reep.football/downloads>
- Hosted lookups with an API key: <https://reep.football/api>

## What 0.1.0 will do

- Download and verify a dated release, and keep it current from the release changelogs.
- Resolve a provider ID, always as the full `(provider, namespace, external_id)` triple,
  to its Reep ID, and follow merge redirects.
- Translate an ID from one provider to another.
- Fill Reep IDs into your own Postgres, MySQL or SQL Server tables, without overwriting
  an existing value, and report the rows it could not resolve.

## Licences

The code in this repository is MIT-licensed. The Reep Register data is CC0-1.0.
