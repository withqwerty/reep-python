# Command line

The `reep` command is for people and for AI agents that act for them. Its JSON
output, error format and exit codes are part of the public interface.

## Output

- The default output is a table or a list of fields.
- `--json` prints one JSON document with this envelope:

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

- `--jsonl` prints one JSON object per row, without the envelope.
- `reep schema <command>` prints the JSON Schema of a command's `--json` output.

Field names are stable. A minor release can add fields. Removing or renaming a field
needs a new `schema_version`.

## Errors and exit codes

Errors go to stderr. With `--json` or `--jsonl`, the error is a JSON object:

```json
{"error": {"code": "invalid_namespace", "message": "transfermarkt has no namespace 'player'. Did you mean 'spieler'?", "hint": "Valid namespaces for transfermarkt: ..."}}
```

| Exit code | Meaning |
|---|---|
| 0 | Success. |
| 1 | Internal error (a bug). |
| 2 | Usage error: bad arguments, invalid ID, unknown provider or namespace. |
| 3 | Not found: the lookup ran and found nothing usable. |
| 4 | Needs review: database sync left rows unresolved or withdrawn. |
| 5 | Authentication: missing, invalid or expired API key, or no quota left. |
| 6 | Network or upstream error: download, checksum or API failure. |
| 7 | Data error: no local catalogue, an unsupported release, or a catalogue in use. |

## Behaviour

- When stdin or stdout is not a terminal, the CLI shows no prompts and no colour.
  `--no-input` forces this, and `NO_COLOR` turns colour off.
- List commands (`providers`, `search`) have a default `--limit` of 50 and return
  `truncated` and `next_offset`.
- `reep providers` and `reep status` show what exists, so an agent does not need to
  guess a provider, a namespace or the backend.

## Commands

```{eval-rst}
.. click:: reep.cli.app:cli
   :prog: reep
   :nested: full
```
