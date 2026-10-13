# Data sources

One `Register` interface reads four sources. The same Python calls and CLI
commands work with each.

| Source | How to use it | Notes |
|---|---|---|
| The SDK catalogue | `reep catalog sync`, then `Register.local()` | Built from the release CSVs in the SDK cache directory; updated from the changelogs. |
| Release CSVs on disk | `reep catalog import DIR`, or `Register.local(csv_dir=DIR)` | Checked against a `release.json` in the folder, or against `--stamp`. |
| The release DuckDB file | `REEP_DUCKDB_PATH=/path/reep-register-v1.duckdb`, or `Register.local(path)` | Opened read-only. To update it, download the new file. |
| The hosted API | `REEP_API_KEY` or `reep auth login`, then `Register.api()` or `--api` | Needs a key; each lookup costs one credit. |

Without `--api` or `--local`, the CLI uses the local catalogue when one exists
(or `REEP_DUCKDB_PATH`), and otherwise the API when a key is set.

Provider fills need both a release stamp and bridge statuses from that release.
SDK snapshots and imports checked against a manifest record both stamps. A shipped
DuckDB file uses its own release stamp for its bridge statuses. A CSV folder without
a manifest has no stamps, so it can fill Reep IDs but cannot fill provider IDs.
Changelog replay keeps the older bridge status stamp; take a snapshot before filling
provider IDs.

## What the API returns differently

The local data is the full release. The API serves the same release with these
differences:

- Resolve candidates and redirect targets count as live only when their lifecycle
  is `active` or `retired`. Any other or missing lifecycle is treated as withdrawn,
  with `target_not_published` when there is no other reason.
- Bridges carry no `rung` or `upstream_status`.
- An entity lists only the bridges that name its own ID. The bridges that still name
  an ID merged into it are missing, so `translate` can return fewer IDs.
- A withdrawn ID that the release removed (`de_corroborated`, `withheld`), or an ID
  merged into an entity that the release does not publish, reads as `unknown` in
  `current`. A withdrawn state carries a reason only when a redirect chain has no
  current ID (`redirect_loop`, `target_not_published`).
- Search returns at most 20 results, and no entity status.
- `providers()` reports no namespaces, so the SDK cannot check a namespace before
  it asks the API.
- Database sync uses the local catalogue only; a large table through the API would
  use one credit per row.

## Environment variables

| Variable | Purpose |
|---|---|
| `REEP_API_KEY` | API key for the API backend. |
| `REEP_DUCKDB_PATH` | Use this DuckDB file (for example the release file) read-only. |
| `REEP_CACHE_DIR` | Location of the SDK catalogue (default: the platform cache directory). |
| `REEP_CONFIG_DIR` | Location of the stored key file (default: the platform config directory). |
| `REEP_BASE_URL` | Override the API base URL. |
| `REEP_RELEASES_URL` | Override the release bucket URL (mirrors and tests). |

`REEP_API_KEY` and `REEP_DUCKDB_PATH` match the names that other Reep tools use, so
one set-up serves all of them.
