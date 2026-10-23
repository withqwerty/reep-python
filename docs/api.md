# Python API

Everything below is importable from the `reep` package and follows the versioning
policy in the changelog.

## Register

```{eval-rst}
.. autoclass:: reep.Register
   :members:
```

### Remote match pages

`Register.api().matches()` retains its list return type and keyword arguments.
`date` selects an exact day; `date_from` and `date_to` are inclusive bounds.
`home_team_id` and `away_team_id` select the respective sides. The hosted API
requires a team, competition or season anchor. It does not support a stadium
`venue_id` filter; this raises `InvalidArgumentError`, as do conflicting filters.
Without a season or explicit date bounds, the API uses its last 30 days.

Use `matches_page()` to retain pagination and provenance:

```python
reg = reep.Register.api()
query = {"team_id": "rt00000000000000", "season_id": "rs00000000000001", "limit": 50}
page = reg.matches_page(**query)
# Persist page["generation"] and page["api_metadata"] with these results.
while page["next_cursor"] is not None:
    page = reg.matches_page(**query, cursor=page["next_cursor"])
```

IDs above are illustrative. Pass the same filters and limit on every continuation.
Each page includes `matches`, `count` (this page only), `generation`, `unresolved`
and `next_cursor`. `api_metadata` snapshots that response's `release_stamp`,
`release_generated_at`, `selection`, `channel`, `source_generation` and
`source_as_of`. Absent headers remain `None`; preparation time does not establish
source freshness. Match rows retain nullable facts and their own generation.

The API can advance hourly while downloads publish less often. These are separate
channels; a newer API version does not invalidate a local snapshot. A stale or
expired cursor raises `ApiError` with `api_code="stale_cursor"`; start a new query
explicitly and keep its results separate. The list method implements `offset` by
walking pages, which can cost several API requests, and refuses mixed identities.
Malformed successful responses raise `ApiError` with
`api_code="invalid_match_response"`; they are not empty search results.

These methods are remote-only. They do not claim catalogue/download compatibility
or change the separate local backend's table contract.

## Catalogue

```{eval-rst}
.. autofunction:: reep.catalog.sync

.. autofunction:: reep.catalog.import_release

.. autofunction:: reep.catalog.state

.. autoclass:: reep.catalog.SyncResult

.. autoclass:: reep.catalog.CatalogState
```

## Database sync

Needs the `db` extra and a database driver; see [](dbsync.md).

```{eval-rst}
.. autofunction:: reep.dbsync.sync

.. autofunction:: reep.dbsync.review

.. autoclass:: reep.dbsync.SyncReport
   :members: planned, written, needs_review, outcomes, to_dict

.. autoclass:: reep.dbsync.TargetReport
   :members: needs_review, to_dict

.. autofunction:: reep.dbsync.load_config

.. autofunction:: reep.dbsync.parse_config

.. autofunction:: reep.dbsync.config_template

.. autoclass:: reep.dbsync.Config
   :members: database_url, select

.. autoclass:: reep.dbsync.TableConfig
   :members: qualified_name, columns_for

.. autoclass:: reep.dbsync.ProviderColumn
   :members: key

.. autodata:: reep.dbsync.OUTCOMES

.. autodata:: reep.dbsync.REVIEW_OUTCOMES

.. autodata:: reep.dbsync.STATUS_VALUES

.. autodata:: reep.dbsync.REVIEW_COLUMNS
```

## MCP server

Needs the `mcp` extra; see [](agents.md).

```{eval-rst}
.. autofunction:: reep.mcp.create_server

.. autodata:: reep.mcp.TOOLS

.. autodata:: reep.mcp.MAX_LIMIT
```

## Models

```{eval-rst}
.. autoclass:: reep.ProviderId
   :members: parse, to_dict

.. autoclass:: reep.Resolution
   :members: found, to_dict

.. autoclass:: reep.ResolutionStatus
   :members:
   :undoc-members:

.. autoclass:: reep.IdState
   :members: to_dict

.. autoclass:: reep.IdStatus
   :members:
   :undoc-members:

.. autoclass:: reep.Bridge
   :members: to_dict

.. autoclass:: reep.Entity
   :members: to_dict

.. autoclass:: reep.Translation
   :members: to_dict

.. autoclass:: reep.EntityMatch
   :members: to_dict

.. autoclass:: reep.ProviderNamespace
   :members: to_dict

.. autoclass:: reep.ReleaseInfo
   :members: to_dict
```

## Errors

Every error has a `code` (used in JSON output), a `hint` and the CLI `exit_code`.

```{eval-rst}
.. autoclass:: reep.ReepError
   :members: to_dict

.. autoclass:: reep.InvalidProviderIdError
.. autoclass:: reep.UnknownProviderError
.. autoclass:: reep.UnknownNamespaceError
.. autoclass:: reep.InvalidReepIdError
.. autoclass:: reep.InvalidArgumentError
.. autoclass:: reep.ConfigError
.. autoclass:: reep.DatabaseError
.. autoclass:: reep.NotFoundError
.. autoclass:: reep.AuthError
.. autoclass:: reep.QuotaExceededError
.. autoclass:: reep.NetworkError
.. autoclass:: reep.ChecksumMismatchError
.. autoclass:: reep.ApiError
.. autoclass:: reep.RateLimitError
.. autoclass:: reep.UnsupportedReleaseError
.. autoclass:: reep.CatalogMissingError
.. autoclass:: reep.CatalogInUseError
.. autoclass:: reep.ReplayError
```
