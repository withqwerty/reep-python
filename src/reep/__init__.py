"""Python SDK for the Reep football entity register.

The Reep Register is an open crosswalk of football provider IDs. This package
downloads and verifies the public release, keeps a local catalogue current, and
answers lookups offline or through the hosted API.

Example:
    >>> import reep
    >>> reep.catalog.sync()  # doctest: +SKIP
    >>> reg = reep.Register.local()  # doctest: +SKIP
    >>> reg.resolve("transfermarkt/spieler/568177").reep_id  # doctest: +SKIP
    'rp53af22bbeaa667'
"""

import logging

from reep import catalog
from reep._version import __version__
from reep.errors import (
    ApiError,
    AuthError,
    CatalogMissingError,
    ChecksumMismatchError,
    ConfigError,
    InvalidArgumentError,
    InvalidProviderIdError,
    InvalidReepIdError,
    NetworkError,
    NotFoundError,
    QuotaExceededError,
    RateLimitError,
    ReepError,
    ReplayError,
    UnknownNamespaceError,
    UnknownProviderError,
    UnsupportedReleaseError,
)
from reep.models import (
    Bridge,
    Entity,
    EntityMatch,
    IdState,
    IdStatus,
    ProviderId,
    ProviderNamespace,
    ReleaseInfo,
    Resolution,
    ResolutionStatus,
    Translation,
)
from reep.register import Register

__all__ = [
    "ApiError",
    "AuthError",
    "Bridge",
    "CatalogMissingError",
    "ChecksumMismatchError",
    "ConfigError",
    "Entity",
    "EntityMatch",
    "IdState",
    "IdStatus",
    "InvalidArgumentError",
    "InvalidProviderIdError",
    "InvalidReepIdError",
    "NetworkError",
    "NotFoundError",
    "ProviderId",
    "ProviderNamespace",
    "QuotaExceededError",
    "RateLimitError",
    "ReepError",
    "Register",
    "ReleaseInfo",
    "ReplayError",
    "Resolution",
    "ResolutionStatus",
    "Translation",
    "UnknownNamespaceError",
    "UnknownProviderError",
    "UnsupportedReleaseError",
    "__version__",
    "catalog",
]

logging.getLogger(__name__).addHandler(logging.NullHandler())
