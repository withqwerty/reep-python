# Development plan

Status: draft, 2026-10-12. This plan covers the work from the 0.0.1 name reservation to
the first feature-complete releases. Each unit (U0–U5) is one tracked work item with its
own acceptance criteria.

## 1. Purpose

`reep` is the Python SDK and command-line tool for the [Reep Register](https://reep.football),
the open crosswalk of football provider IDs. It gives a consumer a short, correct path from
"I hold provider IDs" to "I hold current Reep IDs":

- verified downloads of the dated public release;
- incremental updates between releases;
- correct handling of merged and withdrawn IDs;
- lookups and translations between providers, offline or through the hosted API;
- a guarded write-back of Reep IDs into the user's own database;
- interfaces that AI agents can use for end users without guessing.

### Out of scope

- Fuzzy or name-based matching of rows that carry no provider ID. The public release holds
  no dates of birth, so name matching gives unreliable results. Rows without a provider ID
  go to a review report instead.
- Partner mapping catalogues.
- Writing to the register. Corrections go through <https://reep.football/submit>.

## 2. Decisions

| Topic | Decision |
|---|---|
| Matching | Exact match on `(provider, namespace, external_id)` only. |
| Repository | `withqwerty/reep-python`; private until U3 lands, then public with the 0.1.0 release. |
| Package name | `reep` on PyPI, reserved with 0.0.1; 0.1.0 is the first working release. |
| Licence | Code: MIT. Register data: CC0-1.0. |
| Incremental sync | Clients without an API key follow the changelog chain on the public release bucket. |
| Python support | CPython versions that have not reached end of life: 3.11 to 3.14 at the time of writing. |
| Database writes | Dry run by default; `--apply` writes. |
| Docs site | Open: chosen in U3 (see section 12). |

## 3. Users and surfaces

| User | Main surface | Typical task |
|---|---|---|
| Analyst in a notebook | Python API | Add Reep IDs to a DataFrame of Opta or Transfermarkt IDs. |
| Data engineer | CLI and `reep sync` | Keep Reep IDs current in a Postgres table from a cron job. |
| An end user's AI agent | CLI with `--json`, MCP server, skill file | Answer "what is this player's FBref ID?" for the user. |
| Partner or evaluator with a key | Python API, API backend | Hosted lookups without a local copy. |

## 4. Architecture

```text
                 +---------------------------------------------+
  surfaces       |  CLI (reep)   MCP server (reep mcp)   dbsync |
                 +----------------------+----------------------+
                                        |
  facade                         Register (one interface)
                                        |
                 +----------------------+----------------------+
  backends       |  LocalBackend (DuckDB)   |   ApiBackend (HTTP) |
                 +-----------+--------------+---------------------+
                             |
  release I/O    ReleaseClient: latest.json -> release.json -> files
                 (checksums, cache, changelog replay)
```

- **`Register`** is the one public entry point. Both backends implement the same protocol,
  so code and tests can switch between them.
- **`LocalBackend`** reads a DuckDB file. It builds that file from the release CSVs (the
  "slim" or "full" profile), or it opens the shipped DuckDB file read-only when the user
  points at one (`REEP_DUCKDB_PATH`).
- **`ApiBackend`** calls the keyed `/api/v1` service.
- **`ReleaseClient`** downloads and verifies release files. It holds no lookup logic.
- The surfaces (CLI, MCP server, database sync) use only the `Register` interface.

### Module layout

```text
src/reep/
  __init__.py        public API re-exports, __all__, __version__
  py.typed
  errors.py          ReepError hierarchy
  models.py          ProviderId, Entity, Resolution, Bridge, ReleaseInfo, ... (U1)
  config.py          environment variables, cache and config directories (U1)
  release.py         ReleaseClient: manifest, checksums, downloads (U1)
  changelog.py       changelog parsing and replay (U2)
  catalog.py         catalogue sync: snapshot or chain replay, atomic swap (U2)
  backends/
    base.py          Backend protocol (U1)
    local.py         DuckDB backend (U1)
    api.py           HTTP backend (U3)
  register.py        Register facade (U1, extended in U3)
  cli/               command-line interface (U3)
  dbsync/            reep.yml, database sync, review report (U4)
  mcp/               MCP server (U5)
  skill/SKILL.md     agent skill file shipped with the package (U5)
```

Modules whose names start with an underscore are private. Everything in `__all__` is
public and follows the versioning policy in section 10.

## 5. Public Python API (target for 0.1.0)

```python
import reep

reg = reep.Register.local()               # SDK cache, or REEP_DUCKDB_PATH if set
reg = reep.Register.local(csv_dir="dl/")   # build from CSVs already on disk
reg = reep.Register.api()                 # REEP_API_KEY from the environment

pid = reep.ProviderId("transfermarkt", "spieler", "568177")

res = reg.resolve(pid)                    # -> Resolution
res.status                                # ResolutionStatus.FOUND | NOT_FOUND
res.reep_id                               # "rp…" or None
res.bridge.rung                           # "crosswalk", "name-dob", ... or None
res.bridge.upstream_status                # None | "retired" | "removed"
res.release_stamp                         # "20261005T180536Z"

reg.current("rp9f56e8195ab573")           # -> IdState for a stored Reep ID:
                                          #    ACTIVE, RETIRED, REDIRECTED (survivor),
                                          #    WITHDRAWN (reason), or UNKNOWN
reg.translate(pid, to="fbref")            # -> Translation (resolution, targets: Bridge)
reg.entity("rp4ef85015b8b5d6")            # -> Entity (type, status, label, bridges)
reg.search("Cole Palmer", entity_type="player", limit=10)   # -> list[EntityMatch]
reg.resolve_many(pids)                    # -> list[Resolution], batched
reg.resolve_frame(df, provider="opta", namespace="person", column="opta_id")
reg.providers()                           # -> list[ProviderNamespace] with row counts
reg.release                               # -> ReleaseInfo

reep.catalog.sync(profile="slim")         # -> SyncResult (stamp before and after, method)
reep.catalog.import_release(Path("dl/"))  # -> SyncResult, checked against release.json
```

Rules for the API:

- `ProviderId` requires all three parts. A bare integer or a two-part ID raises
  `InvalidProviderIdError` with the valid namespaces for that provider in the message.
- Reep IDs are validated against `^r[cglmopst][0-9a-f]{14}$`.
- Models are frozen dataclasses with `slots=True`. They have no runtime dependency on
  pydantic.
- Every result carries the release stamp it came from.
- `resolve_frame` accepts pandas or polars objects through DuckDB and returns the same
  type. pandas and polars are optional extras, not core dependencies.

## 6. Data contract handling

The SDK consumes the public release contract and treats it as the source of truth.

| Release item | Use in the SDK |
|---|---|
| `releases/latest.json` | Finds the current stamp and `release.json`. |
| `release.json` | File list, SHA-256 checksums, row counts, schema version. |
| `schemas/*.schema.json` | Column checks at load time; an unknown schema version stops the load. |
| `csv/bridges.csv.gz` | `(provider, namespace, external_id) -> reep_id`, plus `rung` and `upstream_status`. |
| `csv/entities.csv.gz` | Type, status (`active`, `redirected`, `retired`), label. |
| `csv/redirects.csv.gz` | Stored-ID handling (table below). |
| `csv/aliases.csv.gz` | Name search (full profile, or slim profile with `--with-aliases`). |
| `changelog.json.gz` | Incremental updates (U2). |

**Profiles.** "slim" loads bridges, entities and redirects (about 210 MB compressed in
October 2026). "full" loads every CSV table.

**Stored-ID states** from `redirects.csv.gz`:

| `reason` | `to_id` | SDK state | Database sync action |
|---|---|---|---|
| `merged` | survivor ID | `REDIRECTED` | Writes the survivor (with `--repair-redirected`). |
| `target_not_published` | empty | `WITHDRAWN` | Records the state; writes no ID. |
| `de_corroborated` | empty | `WITHDRAWN` | Records the state; writes no ID. |
| `withheld` | empty | `WITHDRAWN` | Records the state; writes no ID. |

Reasons outside this list stop the load with `UnsupportedReleaseError`, so that a new
reason is handled on purpose and not by accident.

## 7. Agent contract (CLI and MCP)

The CLI is designed for people and for AI agents that act for them. The contract below is
part of the public API. A change to it follows the versioning policy.

### Output

- The default output is a table when stdout is a terminal.
- `--json` prints one JSON document. `--jsonl` prints one JSON object per line for
  commands that return many rows.
- Every JSON document has this envelope:

  ```json
  {
    "schema_version": 1,
    "command": "resolve",
    "release_stamp": "20261005T180536Z",
    "backend": "local",
    "data": {"...": "..."},
    "truncated": false,
    "next_offset": null
  }
  ```

- Field names are stable snake_case. New fields can be added in a minor release. Removing
  or renaming a field needs a new `schema_version`.
- `reep schema <command>` prints the JSON Schema of that command's output.

### Errors and exit codes

Errors go to stderr. With `--json`, the error is a JSON object:

```json
{"error": {"code": "invalid_namespace", "message": "...", "hint": "Valid namespaces for transfermarkt: spieler, trainer"}}
```

| Exit code | Meaning |
|---|---|
| 0 | Success. |
| 1 | Internal error (a bug). |
| 2 | Usage error: bad arguments, invalid ID, unknown provider or namespace. |
| 3 | Not found: the lookup ran and found nothing. |
| 4 | Needs review: database sync left rows unresolved or withdrawn. |
| 5 | Authentication: missing, invalid or expired API key. |
| 6 | Network or upstream error: download, checksum or API failure. |
| 7 | Data error: no local catalogue, or an unsupported release schema. |

### Behaviour

- When stdin or stdout is not a terminal, the CLI shows no prompts, spinners or colour.
  `--no-input` forces this; `NO_COLOR` is honoured.
- Commands that write (`reep sync`) run as a dry run by default and print the planned
  changes. `--apply` writes.
- Every list command has a default `--limit` (50) and returns `truncated` and
  `next_offset`, so an agent does not receive millions of rows by mistake.
- Discovery commands remove guesswork: `reep providers` lists every
  `(provider, namespace)` pair in the release with counts; `reep status` shows the
  local catalogue, its stamp, its age and the backend in use.
- `--help` on each command includes worked examples.

### MCP server (U5)

`reep mcp` starts a stdio MCP server (extra `reep[mcp]`). It exposes read-only tools
with `readOnlyHint`: `resolve`, `translate`, `search`, `entity`, `current`, `providers`
and `status`. Results use the same envelope and limits as `--json`. Database sync is not
exposed over MCP.

### Environment variables

| Variable | Purpose |
|---|---|
| `REEP_API_KEY` | API key for the API backend. |
| `REEP_DUCKDB_PATH` | Use this DuckDB file (for example the shipped release file) read-only. |
| `REEP_CACHE_DIR` | Location of the SDK's own catalogue (default: the platform cache directory). |
| `REEP_BASE_URL` | Override the API base URL (testing). |
| `REEP_RELEASES_URL` | Override the release bucket URL (testing and mirrors). |

`REEP_API_KEY` and `REEP_DUCKDB_PATH` match the names other Reep tools use, so one setup
serves all of them.

## 8. Units

Sizes are estimates of production code plus tests.

### U0: Repository foundation and plan

- **Goal:** a standard open-source Python project with CI, release automation and
  community files, plus this plan.
- **Content:** `uv` with a lock file and dependency groups; ruff lint and format; mypy
  strict; pytest with coverage; pre-commit; CI with SHA-pinned actions; a release workflow
  for PyPI Trusted Publishing; Dependabot; CONTRIBUTING, CODE_OF_CONDUCT, SECURITY,
  CITATION.cff, issue and PR templates, CHANGELOG, AGENTS.md.
- **Acceptance:** local checks pass; CI is green on GitHub.

### U1: Core models, release client and local backend

- **Tasks:**
  1. `errors.py`: `ReepError` and subclasses (`InvalidProviderIdError`, `NotFoundError`,
     `ChecksumMismatchError`, `UnsupportedReleaseError`, `CatalogMissingError`,
     `AuthError`, `NetworkError`).
  2. `models.py`: `ProviderId`, `Resolution`, `IdState`, `Bridge`, `Entity`,
     `EntityMatch`, `ProviderNamespace`, `ReleaseInfo`.
  3. `config.py`: environment variables, `platformdirs` cache and config directories.
  4. `release.py`: fetch `latest.json` and `release.json`; download with streaming,
     resume and SHA-256 checks; a `reep-python/<version>` User-Agent; timeouts and
     retries with back-off.
  5. `backends/local.py`: build slim or full DuckDB from the CSVs into a temporary file,
     then rename it into place; open a shipped DuckDB file read-only; indexes on the
     bridge triple and on `reep_id`.
  6. `register.py`: `Register.local()` with `resolve`, `current`, `translate`, `entity`,
     `search`, `resolve_many`, `resolve_frame`, `providers`.
- **Dependencies added:** `httpx`, `duckdb`, `platformdirs`.
- **Tests:** a small fixture release (a few hundred rows, every redirect reason, retired
  and removed bridges) served by a local HTTP fixture; property tests for `ProviderId`
  parsing; one `network` test against the live release.
- **Acceptance:** fixture tests pass; on a developer machine the SDK builds a slim store
  from the live release, resolves `transfermarkt/spieler/568177`, and maps a `merged`
  redirect to its survivor.
- **Size:** about 900 lines of code and 600 lines of tests.

### U2: Incremental catalogue sync

- **Tasks:**
  1. `changelog.py`: parse `changelog_version` 1 and replay the event types
     (`entity_added`, `entity_removed`, `entity_status_changed`, `entity_type_changed`,
     `label_updated`, `provider_updated`, `redirect_changed` with its empty-string
     forms, `score_updated`, `fixture_updated`). An unknown version or event type stops the
     replay and falls back to a snapshot.
  2. `catalog.py`: walk back from `latest.json` through each changelog's `prior.stamp`
     to the local stamp; replay forward on a copy of the store; reload the tables the
     changelog does not cover; check row counts against the manifest; swap atomically.
  3. Fall back to a full snapshot when the chain is broken, longer than the retention
     window, or starts with `prior: null`.
- **Cross-repository task:** a parity test in the release pipeline confirms that this
  replay gives the same entities, bridges and redirects as the pipeline's own replay.
  The public bucket gets a written retention rule for old releases; the SDK's chain limit
  matches it.
- **Acceptance:** a store at stamp N, after `reep catalog sync`, has the same bridge and
  redirect sets as a fresh build of stamp N+1.
- **Built (2026-10-13):** the changelog carries bridge triples, entity type, status and
  label, and merge redirects, but not the bridge `rung` and `upstream_status`, the other
  entity columns, or the redirect rows without a target. The sync therefore downloads
  `entities` and `redirects` again and replays only the bridges (about 65 MB in place of
  207 MB for the 2026-10-05 release). Bridges added by a replay have an empty rung until
  the next snapshot; the store records the stamp of its bridge attributes. The chain
  limit is 30 releases until the retention rule is written.
- **Size:** about 500 lines of code and 500 lines of tests.

### U3: API backend, CLI and 0.1.0 release

- **Tasks:**
  1. `backends/api.py`: `resolve`, `batch` (resolve items in groups of 20, entity items
     in groups of 100, the API's limits), `entities`,
     `search`, `changes`, `key`; back-off on 429; a warning from `X-Reep-Key-Expires`;
     ETag support. There is no command that pages through the whole register.
  2. CLI (`reep`): `status`, `providers`, `resolve`, `current`, `translate`, `search`,
     `show`, `catalog sync`, `auth login|status|logout`, `schema`. It implements the
     agent contract in section 7.
  3. API key storage: `keyring` when available, otherwise a config file with mode 600.
     The CLI does not print the key.
  4. Choose and set up the docs site; publish API reference and CLI reference.
  5. Release 0.1.0 through the release workflow; make the repository public (section 11).
- **Dependencies added:** a CLI framework (Typer or Click, chosen at the start of U3 by
  start-up time and help-text quality), `keyring` as an optional extra.
- **Tests:** contract tests against recorded API responses; CLI tests for every exit code
  and for the JSON envelope; a test that both backends give the same result for the
  fixture triples.
- **Acceptance:** with an evaluation key, the same triple resolves through both backends
  with the same result; `uv tool install reep` works on a clean machine.
- **Size:** about 900 lines of code and 700 lines of tests.

### U4: Database sync and review report

- **Tasks:**
  1. `reep.yml` schema: per table, the entity type, table name, primary key, source ID
     column with its `(provider, namespace)`, and target columns. `reep init` writes a
     commented example.
  2. `reep sync reep` and `reep sync <provider>`: exact joins through the local store;
     a dry run by default; `--apply` writes with an `IS NULL` guard on each target column;
     idempotent and safe to run from cron.
  3. Stored-ID handling as in section 6; `--repair-redirected` is off by default.
  4. `reep review`: counts by table, entity type and provider; a CSV of unresolved and
     withdrawn rows; each run's log records the release stamp.
- **Dependencies added:** SQLAlchemy Core; drivers as extras: `reep[postgres]`,
  `reep[mysql]`, `reep[mssql]`.
- **Tests:** dialect tests on Postgres, MySQL and SQL Server in CI containers; a fixture
  table with known, merged, withdrawn and unknown IDs and pre-filled columns.
- **Acceptance:** one run gives the expected counts; a second run writes nothing; no
  pre-filled value changes.
- **Size:** about 700 lines of code and 600 lines of tests.

### U5: Agent surfaces for end users

- **Tasks:**
  1. `reep mcp`: an MCP server over stdio with the read-only tools in section 7, built on
     the official MCP Python SDK (`mcp`, extra `reep[mcp]`).
  2. `skill/SKILL.md`: a skill file that teaches an agent the triple rule, the discovery
     commands, the JSON envelope and the exit codes; `reep skill print` and
     `reep skill install` (for Claude Code and similar tools).
  3. `llms.txt` and `llms-full.txt` on the docs site.
- **Tests:** an MCP client test over stdio that lists the tools and resolves a fixture
  triple; a test that the skill file's examples run against the fixture release.
- **Acceptance:** an agent with only the skill file resolves a Transfermarkt ID to its
  FBref ID with the CLI.
- **Size:** about 400 lines of code and 300 lines of tests.

### Order

U0, then U1, U2 and U3 in sequence; 0.1.0 ships at the end of U3. U4 and U5 both depend on
U3 and can run in parallel. Each ships as a minor release when it lands.

## 9. Engineering standards

- **Style:** ruff format (Black-compatible) and the ruff rule set in `pyproject.toml`.
  Docstrings follow the Google style on every public object.
- **Types:** full type hints; `mypy --strict` passes; the package ships `py.typed`.
- **Errors:** library code raises subclasses of `ReepError`. Messages say what failed
  and what to do next.
- **Logging:** library modules use `logging.getLogger(__name__)`. The package adds a
  `NullHandler`. Library code does not print; only the CLI writes to stdout and stderr.
- **HTTP:** one `httpx.Client` per backend; explicit timeouts; retries only for idempotent
  requests; the User-Agent is `reep-python/<version>` and carries no personal data.
- **State:** no module-level mutable state; configuration passes through objects.
- **Dependencies:** a small core (`httpx`, `duckdb`, `platformdirs`); everything else
  is an optional extra. Lower bounds in `pyproject.toml`; exact versions in `uv.lock` for
  development only.
- **Tests:** pytest; tests that reach the network carry the `network` marker and are
  deselected by default; coverage of 90 % or more for the package.
- **Privacy:** no telemetry. The SDK sends only the API key (to the API) and standard
  HTTP headers.

## 10. Releases and compatibility

- **Versioning:** Semantic Versioning. Before 1.0, a minor release can contain breaking
  changes; the changelog lists them under "Changed" or "Removed".
- **Changelog:** [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) in
  `CHANGELOG.md`, updated in the same pull request as the change.
- **Release steps:**
  1. Move the "Unreleased" section to the new version and set the version in
     `pyproject.toml` and `CITATION.cff`.
  2. Commit, then tag `vX.Y.Z` on `main`.
  3. The release workflow builds the sdist and wheel, checks that the tag matches the
     version, publishes to PyPI through Trusted Publishing with attestations, and creates
     a GitHub release.
- **Python support:** each CPython version that has not reached end of life. Support for a
  version is dropped in the first minor release after its end-of-life date. A new CPython
  version is added once the core dependencies publish wheels for it.
- **Deprecation:** a public API is deprecated with a `DeprecationWarning` for at least
  one minor release before removal.
- **Release-contract changes:** the SDK checks the release `schema_version` and
  `changelog_version`. An unknown version stops with a clear error and a pointer to
  upgrade the package.

## 11. Go-public checklist

Done at the end of U3, before the 0.1.0 tag:

- [ ] Change the repository visibility to public.
- [ ] Turn on private vulnerability reporting and secret scanning with push protection.
- [ ] Widen the CI matrix: every supported Python on Linux, macOS and Windows.
- [ ] Add the OpenSSF Scorecard workflow and its badge.
- [x] Configure the PyPI Trusted Publisher for the `release.yml` workflow and the `pypi`
      environment. Done 2026-10-13: the `withqwerty` PyPI organisation owns the project,
      and the 0.0.1 release ran the workflow end to end.
- [ ] Add the maintainer as a required reviewer on the `pypi` environment (GitHub offers
      this for public repositories on the current plan).
- [ ] Publish the docs site; add the docs URL to `pyproject.toml` and the README.
- [ ] Point the legacy `reep-cli` repository to this repository and archive it.
- [ ] Link the SDK from reep.football (API and downloads pages).
- [ ] Decide whether `main` requires pull requests and status checks.

## 12. Open questions

| Question | Needed by | Notes |
|---|---|---|
| Docs site framework | U3 | Candidates: Sphinx with the Furo theme, or a MkDocs-family tool. Check the state of the MkDocs ecosystem at the time. |
| CLI framework | U3 | Typer or Click. |
| Python 3.15 | After 0.1.0 | Released 2026-10-09; add it once DuckDB publishes wheels. |
| Integration with other Reep tools | U5 | Other tools can call the SDK instead of their own lookup code. |

## 13. Risks

| Risk | Effect | Mitigation |
|---|---|---|
| A release changes its schema or changelog format | Load or replay fails | Version checks, a clear error, and fixture releases per format version. |
| Old releases leave the bucket earlier than the chain limit | Incremental sync falls back to a snapshot | A written retention rule; the fallback is automatic. |
| Large downloads on slow links | Long first sync | The slim profile, resumable downloads, and `REEP_DUCKDB_PATH` for users who already hold the file. |
| Agents misuse bare integer IDs | Wrong joins | `ProviderId` validation and namespace hints in errors and in the skill file. |
