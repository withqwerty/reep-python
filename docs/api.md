# Python API

Everything below is importable from the `reep` package and follows the versioning
policy in the changelog.

## Register

```{eval-rst}
.. autoclass:: reep.Register
   :members:
```

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
