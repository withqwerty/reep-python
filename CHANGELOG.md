# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `ProviderId`, a validated `(provider, namespace, external_id)` triple, and the result
  models `Resolution`, `IdState`, `Entity`, `EntityMatch`, `Translation`,
  `ProviderNamespace` and `ReleaseInfo`.
- `ReleaseClient`: reads `latest.json` and `release.json`, downloads files with resume and
  retries, and checks each file's SHA-256 against the manifest.
- `reep.catalog.sync()`: builds a local DuckDB catalogue (slim or full profile) from the
  public release. `reep.catalog.import_release()` builds it from release files that are
  already on disk.
- `Register.local()`: offline lookups against the SDK catalogue, a shipped
  `reep-register-v1.duckdb` file (`REEP_DUCKDB_PATH`) or a folder of release CSVs.
  Methods: `resolve`, `resolve_many`, `resolve_frame` (pandas, polars, pyarrow),
  `current`, `translate`, `entity`, `search`, `providers` and `namespaces`.
- Merged IDs resolve to their survivor; withdrawn IDs return a typed status and no ID.
- An error hierarchy under `ReepError`; each error has a code, a hint and a CLI exit code.
- Incremental sync: `reep.catalog.sync()` replays the changelog chain from the local
  release to the target release and downloads a snapshot when it cannot (no prior,
  unknown format, broken or long chain, or a chain larger than the bridges file).
  `SyncResult.notes` gives the reason. `method="replay"` raises `ReplayError` instead.
- `Register.api()`: the same lookups through the hosted API (`/api/v1`) with a key.
  Resolve batches go 20 at a time and entity batches 100 at a time (the API's limits);
  rate-limited requests back off; a key that expires within 7 days logs a warning.
- API key storage: `REEP_API_KEY`, the OS keyring (extra `reep[keyring]`) or a config
  file with mode 600.
- The `reep` command: `status`, `providers`, `resolve`, `current`, `translate`,
  `search`, `show`, `catalog sync`, `catalog import`, `auth login|status|logout` and
  `schema`. `--json` prints a versioned envelope, `--jsonl` prints one row per line,
  errors are JSON on stderr with a hint, and exit codes 0–7 each have one meaning.
- Database sync: `reep init`, `reep sync TARGET...` and `reep review`, and the
  `reep.dbsync` module. A `reep.yml` file names the tables, their primary keys, the Reep
  ID column and the provider ID columns. The sync matches exactly through the local
  catalogue, runs as a dry run unless `--apply` is given, writes a cell only while it is
  NULL, and writes all cells of a run in one transaction. `--repair-redirected`
  replaces stored Reep IDs that merged. `reep review` writes a CSV of the cells that
  need review; both commands exit with 4 when such cells exist. Each run appends a line
  to a run log (`REEP_LOG_DIR`). Extras: `reep[db]`, `reep[postgres]`, `reep[mysql]`
  and `reep[mssql]`.
- `DatabaseError` (exit code 6) for database connection and query failures.

### Changed

- The `reep` command replaces the 0.0.1 placeholder message.

## [0.0.1] - 2026-10-12

### Added

- Package name reservation. The `reep` command prints where to get the register today.

[Unreleased]: https://github.com/withqwerty/reep-python/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/withqwerty/reep-python/releases/tag/v0.0.1
