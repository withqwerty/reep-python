# Database sync

`reep sync` fills Reep IDs, and other providers' IDs, into tables in your own
database. `reep review` lists the rows that need a person to look at them.

The sync matches exactly on the provider ID `(provider, namespace, external_id)`
through the local catalogue. It does not match by name, and it does not use the
hosted API: run `reep catalog sync` first.

## Install

The sync needs SQLAlchemy and a driver for your database:

| Database | Install | URL |
|---|---|---|
| PostgreSQL | `pip install 'reep[postgres]'` | `postgresql://user:password@host/dbname` |
| MySQL, MariaDB | `pip install 'reep[mysql]'` | `mysql+pymysql://user:password@host/dbname` |
| SQL Server | `pip install 'reep[mssql]'` | `mssql+pymssql://user:password@host/dbname` |
| SQLite | `pip install 'reep[db]'` | `sqlite:///path/to/file.db` |

Any SQLAlchemy URL works when its driver is installed, for example
`mssql+pyodbc://...` with pyodbc.

## Configure

`reep init` writes a commented `reep.yml`. Each table entry names the table, its
entity type, its primary key, the Reep ID column and the provider ID columns:

```yaml
version: 1
database: ${DATABASE_URL}     # ${NAME} reads an environment variable

tables:
  players:
    table: players
    entity_type: player
    primary_key: id
    reep_id: reep_id          # VARCHAR(16) or longer
    status: reep_status       # optional: the sync status of each row
    provider_ids:             # provider/namespace: column
      transfermarkt/spieler: tm_id
      opta/person: opta_id
      fbref/person: fbref_id
```

- `reep providers` lists every `provider/namespace` pair.
- `primary_key` must be the table's primary key or a unique key over plain columns.
  It can be a list. A partial (filtered) unique index or a unique index on an
  expression is not a key: it does not name one row. A row whose key holds NULL
  stops the run before anything is written.
- Two chosen entries must not name the same physical table, including an explicit
  default schema. Put all target columns in one entry.
- Each role needs its own column: `reep_id`, `status` and every provider ID column.
  Names match without regard to case, so `reep_id` and `REEP_ID` are one column. A
  provider ID column can also be the key.
- Provider ID columns can be text, integer or UUID columns. Floating-point columns
  are refused, because an ID such as 568177 becomes 568177.0. An integer or UUID
  column must give back the provider's exact ID: `0007` does not go into an integer
  column (it would read back as `7`), and the cell is reported as
  `column_mismatch`.
- Without `database`, reep reads `REEP_DATABASE_URL`, then `DATABASE_URL`. Output,
  errors and logs show only `drivername://host[:port]/database`. They omit the
  username, password and all query parameters. SQLite keeps its file path.
  Database errors show only the exception class and a short structured error code,
  when available. They suppress the original exception's traceback. Config errors
  name the field and expected type without quoting its value.
- The commands read `./reep.yml`, the file in `REEP_CONFIG`, or `--config PATH`.

## Run

```bash
reep sync reep                 # dry run: shows what would change, writes nothing
reep sync reep --apply         # writes the Reep IDs
reep sync fbref --apply        # fills empty FBref ID cells through the Reep ID
reep sync reep fbref --apply   # both, in this order
reep review                    # writes reep-review.csv; writes nothing to the database
```

The sync works out each row's Reep ID from its stored Reep ID and its provider IDs.
They must agree. When two of them point to different entities, the row is a
`conflict` and the sync writes nothing to it.

### What the sync writes

- The sync writes a cell only while it is NULL. A value that is already there does
  not change, and an empty string counts as a value. A second run with no new data
  writes nothing.
- `--repair-redirected` is the one exception: it replaces a stored Reep ID that
  merged into another ID with the survivor. It is off by default.
- Each row gets one `UPDATE` with all its planned cells. It changes the row only
  while every column the sync read (the Reep ID, the status and the provider ID
  columns) still holds the value the sync saw, compared exactly (a case-insensitive
  collation does not hide a change from `alpha` to `ALPHA`). When another program
  changed the row in the meantime, the sync leaves it as it is; the report's
  `written` count is lower than `planned`.
- All writes of a run happen in one transaction.
- Filling provider ID columns (`reep sync fbref`) needs a release stamp and a
  bridge status stamp from that same release. Missing stamps stop provider fills.
  Snapshots, manifest-backed imports and stamped release DuckDB files provide them.
  CSV imports without a manifest do not. A catalogue that `reep catalog sync`
  updated from changelogs keeps each bridge's
  `upstream_status` from its last snapshot, so the sync stops and asks you to run
  `reep catalog sync --method snapshot` first. Filling Reep IDs does not need it.
- A provider ID can reach a row through an ID that merged into the row's entity.
  The sync writes it like any other bridge of the entity.
- Before it writes a provider ID, the sync resolves that ID and writes it only when
  it leads back to the row's Reep ID and to no other entity. Otherwise the cell is
  `ambiguous`.
- A row whose stored Reep ID cannot be checked (unknown to the release, withdrawn,
  blank, whitespace or not a Reep ID) while its provider IDs name an entity is a
  `conflict`: the sync writes no provider ID to it. A blank or whitespace Reep ID
  cell itself still has outcome `invalid_value`.
- The status column, when you configure one, belongs to the sync. `reep sync reep`
  sets it to `resolved`, `retired` or a review outcome below, and changes it only
  when the value changes.

### Outcomes

Each cell gets one outcome. The outcomes in the second table need review.

| Outcome | Meaning | Written |
|---|---|---|
| `new` | The cell was empty and the register gives a value. | yes |
| `repair` | A stored Reep ID merged; `--repair-redirected` replaces it. | yes |
| `unchanged` | The stored value is current. | no |
| `retired` | The entity is retired. Keep the ID; an empty cell gets it. | when empty |

| Outcome | Meaning |
|---|---|
| `redirected` | The stored Reep ID merged into another ID. |
| `withdrawn` | The register withdrew the ID (`target_not_published`, `de_corroborated` or `withheld`). |
| `unresolved` | The register has no bridge for the row's provider IDs. |
| `unknown_id` | The stored Reep ID is not in the register. |
| `conflict` | The row's IDs point to different entities. |
| `ambiguous` | A provider ID names several entities, or the entity has several IDs at the target provider. |
| `type_mismatch` | The ID belongs to another entity type, for example a coach in a player table. |
| `invalid_value` | The cell holds a value that cannot be an ID (an empty string, a float, a malformed Reep ID). |
| `no_source` | The row has no provider ID and no Reep ID. |
| `no_target` | The entity has no ID at the target provider. |
| `upstream_retired` | The provider retired the ID. |
| `upstream_removed` | The provider removed the ID. |
| `column_mismatch` | The provider's ID does not fit the column (for example text in an integer column). |

## Exit codes and cron

Both commands exit with 4 when cells need review, and with 0 when none do. The
other exit codes are the same as for every command (see [](cli.md)): 2 for a
config error, 6 when the database cannot be reached, 7 when there is no local
catalogue.

A cron job that accepts rows for review:

```bash
reep catalog sync --json > /dev/null \
  && reep sync reep fbref --apply --json > last-sync.json
status=$?
[ "$status" -eq 0 ] || [ "$status" -eq 4 ] || exit "$status"
```

## Review report

`reep review` plans every target (the Reep ID column and each provider column) and
writes the cells that need review to a CSV file (`--output`, default
`reep-review.csv`) with these columns: `table`, `entity_type`, `target`, `column`,
`primary_key`, `value`, `outcome`, `reep_id`, `detail` and `release_stamp`.

Filters: `--table NAME`, `--entity-type TYPE` and `--target NAME`.

## Run log

Each run appends one JSON line with the time, the release stamp, the counts and the
database location without credentials or query parameters. The default file is
`dbsync-runs.jsonl` in the platform log directory (`REEP_LOG_DIR` changes it). Set `run_log: path` or
`run_log: false` in `reep.yml`, or use `--log-file PATH` or `--no-log`.

## From Python

```python
import reep
from reep import dbsync

with reep.Register.local() as reg:
    report = dbsync.sync("reep.yml", reg, ["reep"], apply=True)
    print(report.written, report.needs_review)
    dbsync.review("reep.yml", reg, output="review.csv")
```

`sync()` and `review()` also accept a SQLAlchemy `engine=`. See [](api.md).
