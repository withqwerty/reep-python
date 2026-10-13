# For AI agents

An agent that answers questions such as "what is this player's FBref ID?" can use
the `reep` command directly. These rules give correct answers:

1. Use the complete provider ID: provider, namespace and external ID. Run
   `reep providers --provider <name> --json` when you do not know the namespace.
2. Add `--json` and read `data`. Check `data.status` before you use
   `data.reep_id`.
3. Use the exit code: 0 success, 2 fix the arguments (the error has a `hint`),
   3 not found, 5 no valid API key, 6 network, 7 no catalogue (run
   `reep catalog sync`).
4. Use `reep translate <id> --to <provider> --json` to go from one provider to
   another.
5. Use `reep search` only to discover candidates for a person to confirm. Names are
   not unique, and the release has no dates of birth.

```bash
reep catalog sync --json
reep resolve transfermarkt spieler 568177 --json
reep translate transfermarkt/spieler/568177 --to fbref --json
reep current rc00cfa6784c6c23 --json
```
