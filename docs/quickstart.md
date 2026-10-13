# Quickstart

## Install

reep needs Python 3.11 or later.

```bash
pip install reep
```

To use only the command, install it as a tool:

```bash
uv tool install reep        # or: pipx install reep
```

Optional extras:

| Extra | Adds |
|---|---|
| `reep[pandas]` | `resolve_frame` for pandas DataFrames |
| `reep[polars]` | `resolve_frame` for polars DataFrames |
| `reep[arrow]` | `resolve_frame` for pyarrow Tables |
| `reep[keyring]` | API key storage in the operating system keyring |

## Get the register

The register is free to download and needs no key.

```bash
reep catalog sync
```

The first sync downloads the slim profile (bridges, entities and redirects, about
210 MB) and builds a DuckDB catalogue in the SDK cache directory. Later syncs replay
the release changelogs when they can, which downloads much less.

If you already have the release files, build the catalogue from them:

```bash
reep catalog import ~/Downloads/reep
```

If you have the release DuckDB file (`reep-register-v1.duckdb`), point
`REEP_DUCKDB_PATH` at it. reep reads it as it is.

## Look up IDs from the command line

```bash
reep resolve transfermarkt spieler 568177
reep translate transfermarkt/spieler/568177 --to fbref
reep current rc00cfa6784c6c23              # a merged ID: shows its survivor
reep providers --provider transfermarkt    # the namespaces of a provider
reep search Cole Palmer --type player       # discovery only, not matching
```

Add `--json` for machine-readable output. See [](cli.md).

## Look up IDs from Python

```python
import reep

reg = reep.Register.local()

res = reg.resolve(reep.ProviderId("transfermarkt", "spieler", "568177"))
if res.found:
    print(res.reep_id, res.label, res.bridge.rung)

state = reg.current("rc00cfa6784c6c23")
print(state.status, state.current_id)  # redirected rc6c1f16f6b8b702

fbref = reg.translate("transfermarkt/spieler/568177", to="fbref")
print([str(b.provider_id) for b in fbref.targets])
```

## Add Reep IDs to a DataFrame

```python
import pandas as pd

df = pd.DataFrame({"tm_id": ["568177", "35664"]})
out = reg.resolve_frame(
    df, provider="transfermarkt", namespace="spieler", column="tm_id"
)
print(out[["tm_id", "reep_id", "reep_status"]])
```

`resolve_frame` returns a new frame of the same type with a Reep ID column and a
status column. A column of floating-point numbers is refused, because an ID such as
568177 would become `568177.0`.

## Use the hosted API

The API needs a key; request one at <https://reep.football/api>.

```bash
reep auth login                   # stores the key in the keyring or a mode-600 file
reep resolve transfermarkt spieler 568177 --api
```

```python
reg = reep.Register.api()  # REEP_API_KEY, or the stored key
```

See [](sources.md) for what the API returns differently.
