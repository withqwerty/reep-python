# For AI agents

An agent that answers questions such as "what is this player's FBref ID?" can use
the `reep` command directly. These rules give correct answers:

1. Use the complete provider ID: provider, namespace and external ID. Run
   `reep providers --provider <name> --json` when you do not know the namespace.
2. Add `--json` and read `data`. Check `data.status` before you use
   `data.reep_id`.
3. Use the exit code: 0 success, 2 fix the arguments (the error has a `hint`),
   3 not found, 4 rows need review (database sync), 5 no valid API key,
   6 network or database, 7 no catalogue (run `reep catalog sync`).
4. Use `reep translate <id> --to <provider> --json` to go from one provider to
   another.
5. Use `reep search` only to discover candidates for a person to confirm. Names are
   not unique, and the release has no dates of birth.
6. `reep sync` writes to the user's database. Run it without `--apply` first, show
   the user the planned counts, and add `--apply` only when the user agrees. Use
   `reep review` to list the rows that need review.

```bash
reep catalog sync --json
reep resolve transfermarkt spieler 568177 --json
reep translate transfermarkt/spieler/568177 --to fbref --json
reep current rc00cfa6784c6c23 --json
```

## Skill file

The package ships a skill file (`SKILL.md`) that teaches an agent these rules, the
commands and the exit codes. Install it for Claude Code, or print it for another
agent:

```bash
reep skill install             # ~/.claude/skills/reep/SKILL.md (all projects)
reep skill install --project   # ./.claude/skills/reep/SKILL.md (this project)
reep skill print               # print it, to copy into another agent's setup
```

## MCP server

`reep mcp` serves the lookups as read-only MCP tools over stdio. It needs the `mcp`
extra:

```bash
uv tool install 'reep[mcp]'    # or: pip install 'reep[mcp]'
claude mcp add reep -- reep mcp
```

| Tool | Does |
|---|---|
| `resolve` | Resolves a provider ID (provider, namespace, external ID) to its Reep ID. |
| `resolve_many` | Resolves up to 50 provider IDs to their Reep IDs in one call. |
| `translate` | Returns another provider's IDs for the same entity. |
| `translate_many` | Translates up to 50 provider IDs to another provider's IDs in one call. |
| `current` | Returns the state of a stored Reep ID (active, redirected, withdrawn, ...). |
| `entity` | Returns an entity with its label and all its provider IDs. |
| `search` | Searches by name, for discovery only (at most 50 results per call). |
| `providers` | Lists the provider and namespace pairs (at most 50 per call). |
| `status` | Shows the local catalogue, its release and the backend in use. |

Each tool returns the same envelope as `--json`. Errors are MCP tool errors whose
text starts with the error code and ends with the hint. The server uses the local
catalogue when one exists; `reep mcp --api` uses the hosted API. Database sync is
not available over MCP.

## Machine-readable docs

`llms.txt` lists the documentation pages. `llms-full.txt` holds every guide page,
the skill file and the help text of every command in one file; the docs build
writes it.
