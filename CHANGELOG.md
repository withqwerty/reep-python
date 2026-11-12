# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-11-12

### Fixed

- `reep catalog sync` and `Register.local` work again against release 2026-10-19.1.
  The release descriptor's SHA-256 and size bind every downloaded file. The producer
  manifest still binds its row counts, and its checksums bind only the six
  mapping-table parquet files (bridges, entities, redirects, overlay links, overlay
  aliases, provider aliases); a republish leaves its other checksums stale. A shipped
  DuckDB file may omit the internal `_*` tables and `edges` (which left the public
  scope on 2026-11-05); their columns are still checked when present, a missing
  public table or an unknown extra table still refuses the file. A bridge key that
repeats the same mapping (release 2026-10-19.1 holds three) no longer refuses; a
key that names two entities still does.
- Remote `Register.matches()` now reads the real API match envelope and translates
  date and home/away filters. Offset traversal follows generation-bound cursors;
  malformed pages and stale cursors raise errors instead of returning false misses.
  Stadium `venue_id` filters are explicitly unsupported by the hosted API.

### Added

- Versioned v4 downloads: the download selector and immutable descriptor are checked
  independently of the API channel, including object hashes, lengths, inventory and
  bound table counts, for both `reep-public-release/v4` and the retained
  `bridge-register-v1` / `v4_physical_views` (`bridge-register-v1/v4_physical_views/1`)
  variant. Slim/full catalogues and local imports support the declared CSV layouts;
  shipped v4 DuckDB files are adapted in memory without exposing internal `_*`
  relations, remain read-only, and refuse unpublished match parent filters.
- `IdStatus.PUBLISHED` reports a present downloaded ID whose lifecycle is unreported.
  `Entity.status` can now be `None`, as can resolution/current lifecycle fields.
  Missing upstream status and legacy rung remain unknown. V4 redirect reasons are
  preserved, and database sync handles published IDs without inventing lifecycle.
- Declared venue/transfer/award IDs and historical underscore IDs are accepted;
  unknown prefixes remain errors. CLI JSON schemas include these IDs and nullable
  entity status. V4 catalogue updates use snapshots; explicit v4 replay raises
  `ReplayError` until a supported change feed exists. Legacy replay is unchanged.

- `Register.matches_page()` returns a validated match page with its cursor,
  unresolved anchors, generation and per-response API provenance. API freshness
  remains independent of local downloadable snapshots.

## [0.2.0] - 2026-10-17

### Added

- `Register.translate_many()`, `Register.translate_frame()` (pandas, polars, pyarrow) and
  `Register.entities_many()` across both local and API backends.
- `Register.last_call_meta` (`ApiCallMeta`) exposing rate-limit, credit and release
  headers from the most recent API request.
- `Register.matches()`, `Register.changes()`, `Register.changes_summary()`,
  `Register.changes_releases()` and `Register.key_status()` on `Register` for API
  backends (raising a clear `InvalidArgumentError` with a hint on local backends).
- Source namespace auto-inference on `Register.resolve()` and `Register.translate()` when
  a provider has a single namespace or when `entity_type` narrows it to one namespace.
- MCP batch tools `resolve_many` and `translate_many` (up to 50 items per call).

### Changed

- `ApiBackend.entity()` now fetches an entity with its bridges, aliases and relationships
  in one HTTP request (`GET /api/v1/entities/{id}?include=bridges,aliases,relationships`)
  instead of four sequential requests, and supports `include_overlay=True`.
- `ApiBackend.resolve_many()` batches up to 50 items per `POST /api/v1/batch` call.
- `LocalBackend.search()` folds diacritics and extended Latin ligatures (`ø`, `ł`, `ß`,
  `đ`, `æ`, `œ`, Turkish `ı`) in Python before token matching so offline search matches
  the hosted API on ASCII queries.

## [0.1.0] - 2026-10-13

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
- Merged IDs resolve to their survivor, through chains of merges, in the local and API
  backends; only `active` and `retired` lifecycles are live. Missing or other API
  lifecycles, withdrawn IDs, redirect loops and chains without a survivor return a
  typed status and no ID. An entity, `translate` and the database sync also list
  the bridges that still name an ID merged into the entity (local backend).
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
  NULL, and writes all cells of a run in one transaction. Each row gets one `UPDATE`
  that runs only while every column the sync read is unchanged. The key must be a
  primary key or a unique key over plain columns, each role needs its own column, an
  integer or UUID column must keep the provider's exact ID. Two chosen entries cannot
  name the same physical table. Provider fills need both release and bridge status
  stamps, present and equal. A blank or uncheckable stored Reep ID blocks provider
  fills when the row's provider IDs name an entity. `--repair-redirected` replaces
  stored Reep IDs that merged. `reep review` writes a CSV of the cells that need
  review; both commands exit with 4 when such cells exist. Each run appends a line
  to a run log (`REEP_LOG_DIR`). Extras: `reep[db]`, `reep[postgres]`, `reep[mysql]`
  and `reep[mssql]`.
- `DatabaseError` (exit code 6) for database connection and query failures. Messages
  show only the exception class and a short structured driver code, and suppress the
  original exception's traceback. Database URLs show only the driver, host, port and
  database, without credentials or query parameters. Invalid config values are not
  quoted. URL parsing and engine construction failures raise `ConfigError` (exit 2).
- `reep mcp`: a read-only MCP server over stdio (extra `reep[mcp]`) with the tools
  `resolve`, `translate`, `current`, `entity`, `search`, `providers` and `status`. The
  tools return the `--json` envelope; errors carry the error code and hint.
- An agent skill file that ships in the package, with `reep skill print` and
  `reep skill install`.
- The docs build writes `llms-full.txt`: every guide page, the skill file and the help
  text of every command.

### Changed

- The `reep` command replaces the 0.0.1 placeholder message.

## [0.0.1] - 2026-10-12

### Added

- Package name reservation. The `reep` command prints where to get the register today.

[Unreleased]: https://github.com/withqwerty/reep-python/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/withqwerty/reep-python/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/withqwerty/reep-python/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/withqwerty/reep-python/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/withqwerty/reep-python/releases/tag/v0.0.1
