# Concepts

## Provider IDs are triples

A provider ID has three parts: `(provider, namespace, external_id)`. The provider
alone is not enough. Transfermarkt uses `568177` for a player (namespace `spieler`)
and for a coach (namespace `trainer`), and these are different people.

reep refuses a bare number or a two-part ID with `InvalidProviderIdError`. When the
namespace is wrong, the error names the valid ones:

```text
Error: transfermarkt has no namespace 'player'. Did you mean 'spieler'?
Hint: Valid namespaces for transfermarkt: match, saison, schiedsrichter, ...
```

Run `reep providers --provider <name>` to list a provider's namespaces.

## Reep IDs

New Reep IDs use `r`, one declared entity-type letter and 14 hex digits; retained
underscore-prefixed IDs have 16 hex digits. For example
`rp53af22bbeaa667`. The letter gives the type:

| Letter | Type | Letter | Type |
|---|---|---|---|
| `p` | player | `t` | team |
| `c` | coach | `l` | competition |
| `o` | referee | `s` | season |
| `m` | match | `g` | stage |

Reep IDs are stable across releases. An ID can merge into another (a duplicate was
found), or the release can withdraw it.

## Resolving a provider ID

`resolve` returns a `Resolution` with one of these statuses:

| Status | Meaning |
|---|---|
| `found` | `reep_id` is the current Reep ID. When the bridge names a merged ID, `reep_id` is the survivor and `redirected_from` is the merged ID. |
| `not_found` | The release has no bridge for this provider ID. |
| `withdrawn` | The bridge names an ID that the release withdraws and gives no survivor for. |
| `ambiguous` | The provider ID names more than one entity; see `candidates`. |

## Stored Reep IDs

`current` tells you what to do with a Reep ID you stored earlier:

| Status | Redirect reason | What to do |
|---|---|---|
| `active` |  | Keep the ID. |
| `retired` |  | Keep the ID; the entity is retired. |
| `redirected` | `merged` | Store `current_id`, the survivor. |
| `withdrawn` | `target_not_published` | Review the row. The release names no survivor. |
| `withdrawn` | `de_corroborated` | Review the row. The entity no longer has enough independent sources. |
| `withdrawn` | `withheld` | Review the row. A publication rule keeps the entity out of this release. |
| `unknown` |  | The release does not know the ID. |

reep never writes an empty string as an ID. A release with a redirect reason that
this version does not know stops with `UnsupportedReleaseError`.

## Bridge metadata

Each bridge (provider ID to Reep ID) carries two columns from the local catalogue:

- `rung`: the kind of agreement that attached the ID. From weakest to strongest:
  `crosswalk`, `name-nationality`, `name-dob`, `footprint`, `corroborated-mint`,
  `first-party`, `operator`. `None` means the release records no rung.
- `upstream_status`: `None` for a live ID, `retired` when the provider retired the ID
  in favour of another one it holds, `removed` when the provider's newest complete
  registry no longer lists it. The ID still resolves.

A bridge can still name an ID that merged into another. Lookups of the provider ID
give the survivor (`redirected_from` names the merged ID). The survivor's entity,
`translate` and the database sync list such a bridge with the survivor's own
bridges; its `reep_id` is the merged ID, which shows where it came from.

## Releases

The register is published as dated releases, for example `20261005T180536Z`. Each
result carries the `release_stamp` it came from. `releases/latest.json` on
`data.reep.football` names the current release; its `release.json` lists every file
with its SHA-256.

### Profiles

| Profile | Tables | Use |
|---|---|---|
| `slim` | bridges, entities, redirects | Every lookup. About 210 MB to download. |
| `slim` with aliases | the above and aliases | Better name search. |
| `full` | every CSV table | Relationships, per-type tables and overlays. |

### Incremental sync

Each release has a `changelog.json.gz` with the changes from the release before it.
`reep catalog sync` walks back through these changelogs to the release you hold,
replays the bridge changes, and downloads the `entities` and `redirects` tables
again (the changelog does not carry all their columns). It checks the result
against the release and downloads a full snapshot when it cannot replay.

Bridges added by a replay have no `rung` or `upstream_status` until the next full
snapshot, and the bridges that were already there keep the values of that
snapshot. `reep status` shows the release that these two columns come from. The
database sync refuses to fill provider ID columns from such a catalogue.
