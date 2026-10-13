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
