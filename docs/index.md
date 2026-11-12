# reep

`reep` is the Python SDK and command-line tool for the
[Reep Register](https://reep.football), the open crosswalk of football provider IDs.
It takes you from "I hold provider IDs" to "I hold current Reep IDs", and from one
provider's IDs to another's.

```bash
pip install reep            # or: uv tool install reep
reep catalog sync           # download the register (about 230 MB, no key)
reep resolve transfermarkt spieler 568177
reep translate transfermarkt/spieler/568177 --to fbref
```

```python
import reep

reep.catalog.sync()
reg = reep.Register.local()
reg.resolve("transfermarkt/spieler/568177").reep_id  # 'rp53af22bbeaa667'
```

What the package does:

- downloads the dated public release and checks every file against its SHA-256;
- updates the local copy from the release changelogs, without a full download;
- resolves merged IDs to their survivor, and reports withdrawn IDs with a status;
- answers lookups offline, or through the hosted API with a key;
- fills Reep IDs and provider IDs into your own database tables, with a review report;
- gives AI agents a stable JSON output, clear errors and fixed exit codes.

Matching is exact: every lookup uses the complete provider ID
`(provider, namespace, external_id)`. The package does not match by name.

```{toctree}
:maxdepth: 2
:caption: Guide

quickstart
concepts
sources
dbsync
cli
agents
```

```{toctree}
:maxdepth: 2
:caption: Reference

api
changelog
```

The code is MIT-licensed. The register data is CC0 1.0. To report a wrong ID or a
bad merge, use <https://reep.football/submit>.
