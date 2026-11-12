---
name: reep
description: Look up and convert football (soccer) provider IDs with the Reep Register and the reep command. Use it to find the Reep ID of a Transfermarkt, Opta, FBref, Sofascore, Wyscout, FotMob or other provider ID; to translate a player, coach, team, competition or match ID from one provider to another; to check whether a stored Reep ID merged or was withdrawn; or to fill Reep IDs into the user's database.
---

# Reep Register lookups

The Reep Register is an open crosswalk of football provider IDs. The `reep` command
answers from a local copy of the register (or from the hosted API with a key).
Matching is exact: `reep` does not match by name.

## Rules

1. **A provider ID is a triple: provider, namespace and external ID.** For example
   `transfermarkt / spieler / 568177`. The same number names different entities in
   different namespaces (Transfermarkt `spieler` is a player, `trainer` a coach).
   Never look up a bare number.
2. **Find the namespace before you guess it:**
   `reep providers --provider transfermarkt --json` lists the namespaces and the
   entity types in each.
3. **Add `--json` to every command** and read `data`. For `resolve`, check
   `data.status` before you use `data.reep_id`:
   `found`, `not_found`, `ambiguous` or `withdrawn`.
4. **Use the exit code:**

   | Code | Meaning | What to do |
   |---|---|---|
   | 0 | Success | Use the result. |
   | 2 | Usage error | Read `error.hint` on stderr, fix the arguments, try again. |
   | 3 | Not found | Tell the user the register has no match. Do not guess. |
   | 4 | Rows need review | `reep sync` or `reep review` left rows for a person. |
   | 5 | Authentication | The API key is missing or expired (only with `--api`). |
   | 6 | Network or database | Retry later, or check the database URL. |
   | 7 | No local catalogue | Run `reep catalog sync` (see Setup). |

5. **Search is for discovery only.** Names are not unique and the register has no
   dates of birth. Show `reep search` results to the user as candidates; match
   records by provider ID.
6. **`reep sync` writes to the user's database.** Run it without `--apply` first,
   show the user the planned counts, and add `--apply` only when the user agrees.

## Setup

```bash
reep status --json
```

When `data.backend` is `null`, or a command exits with 7, the register is not
downloaded. Tell the user that `reep catalog sync` downloads about 230 MB (free, no
key), then run it:

```bash
reep catalog sync --json
```

## Tasks

Find the Reep ID of a provider ID:

```bash
reep resolve transfermarkt spieler 568177 --json
```

Translate an ID to another provider. The IDs are in `data.targets[].external_id`;
live IDs come first, and `upstream_status` is `retired` or `removed` when the
provider no longer uses an ID. A target whose `reep_id` differs from the source's
Reep ID came through an ID that merged into it. `data.resolution` holds the source's
Reep ID, status and label, so a separate `resolve` is not needed:

```bash
reep translate transfermarkt/spieler/568177 --to fbref --json
reep translate transfermarkt/spieler/568177 --to opta --json
```

Show an entity with its label and all its provider IDs:

```bash
reep show rp53af22bbeaa667 --json
```

Check a stored Reep ID. `data.status` is `active`, `retired`, `published` (present in
a v4 download; lifecycle unreported), `redirected` (store `data.current_id`),
`withdrawn` (do not use the ID) or `unknown`. A v4 entity's lifecycle and upstream
status remain null; `published` does not mean active:

```bash
reep current rp53af22bbeaa667 --json
```

Resolve many IDs: pass several, or one per line on stdin with `-`:

```bash
reep resolve transfermarkt/spieler/568177 transfermarkt/spieler/35664 --json
printf 'transfermarkt/spieler/568177\nopta/person/p1\n' | reep resolve - --json
```

Find candidates by name, for the user to confirm:

```bash
reep search Cole Palmer --type player --json --limit 5
```

Fill Reep IDs into the user's database tables (needs `reep[postgres]`, `reep[mysql]`,
`reep[mssql]` or `reep[db]`, and a `reep.yml` that `reep init` writes):

```bash
reep init
reep sync reep --json
reep sync reep --apply --json
reep review --output reep-review.csv --json
```

## Output

Every `--json` document has this envelope:

```json
{"schema_version": 1, "command": "resolve", "release_stamp": "20261005T180536Z",
 "backend": "local", "data": {}, "truncated": false, "next_offset": null}
```

Lists (`providers`, `search`) return at most `--limit` rows (default 50). When
`truncated` is true, call again with `--offset` set to `next_offset`.
`reep schema <command>` prints the JSON Schema of a command's output.

Errors go to stderr as `{"error": {"code": ..., "message": ..., "hint": ...}}`.

## MCP

`reep mcp` (extra `reep[mcp]`) serves the same lookups as read-only MCP tools:
`resolve`, `translate`, `current`, `entity`, `search`, `providers` and `status`.
