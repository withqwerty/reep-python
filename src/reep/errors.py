"""Exceptions raised by the reep package.

Every exception derives from :class:`ReepError`. Each class has a stable ``code``
(used in JSON error output) and the CLI ``exit_code`` that it maps to.
"""

from __future__ import annotations

from typing import Any, ClassVar

__all__ = [
    "ApiError",
    "AuthError",
    "CatalogInUseError",
    "CatalogMissingError",
    "ChecksumMismatchError",
    "ConfigError",
    "InvalidArgumentError",
    "InvalidProviderIdError",
    "InvalidReepIdError",
    "NetworkError",
    "NotFoundError",
    "QuotaExceededError",
    "RateLimitError",
    "ReepError",
    "ReplayError",
    "UnknownNamespaceError",
    "UnknownProviderError",
    "UnsupportedReleaseError",
]


class ReepError(Exception):
    """Base class for all errors raised by the reep package.

    Attributes:
        message: What failed.
        hint: What to do next, or ``None``.
    """

    code: ClassVar[str] = "reep_error"
    exit_code: ClassVar[int] = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        """Create the error.

        Args:
            message: What failed.
            hint: What to do next.
        """
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        """Return the message, followed by the hint when there is one."""
        if self.hint:
            return f"{self.message} Hint: {self.hint}"
        return self.message

    def to_dict(self) -> dict[str, Any]:
        """Return the error as a JSON-ready dictionary."""
        return {"code": self.code, "message": self.message, "hint": self.hint}


class InvalidProviderIdError(ReepError, ValueError):
    """A provider ID is not a complete ``(provider, namespace, external_id)`` triple."""

    code = "invalid_provider_id"
    exit_code = 2


class UnknownProviderError(InvalidProviderIdError):
    """The provider is not in the release."""

    code = "unknown_provider"


class UnknownNamespaceError(InvalidProviderIdError):
    """The namespace is not in the release for this provider."""

    code = "invalid_namespace"


class InvalidReepIdError(ReepError, ValueError):
    """A string is not a valid Reep ID."""

    code = "invalid_reep_id"
    exit_code = 2


class InvalidArgumentError(ReepError, ValueError):
    """An argument has a value that the SDK cannot use."""

    code = "invalid_argument"
    exit_code = 2


class ConfigError(ReepError, ValueError):
    """A configuration value or file is not valid."""

    code = "config_error"
    exit_code = 2


class NotFoundError(ReepError, LookupError):
    """A lookup ran and found nothing."""

    code = "not_found"
    exit_code = 3


class AuthError(ReepError):
    """The API key is missing, invalid or expired."""

    code = "auth_error"
    exit_code = 5


class QuotaExceededError(AuthError):
    """The API key has used its quota."""

    code = "quota_exceeded"


class NetworkError(ReepError):
    """A download or an API request failed."""

    code = "network_error"
    exit_code = 6


class ChecksumMismatchError(NetworkError):
    """A downloaded file does not match the checksum in the release manifest."""

    code = "checksum_mismatch"


class ApiError(NetworkError):
    """The API returned an error response.

    Attributes:
        status: The HTTP status code.
        api_code: The ``error`` value from the response body, if any.
    """

    code = "api_error"

    def __init__(
        self,
        message: str,
        *,
        status: int,
        api_code: str | None = None,
        hint: str | None = None,
    ) -> None:
        """Create the error.

        Args:
            message: What failed.
            status: The HTTP status code.
            api_code: The ``error`` value from the response body.
            hint: What to do next.
        """
        super().__init__(message, hint=hint)
        self.status = status
        self.api_code = api_code

    def to_dict(self) -> dict[str, Any]:
        """Return the error as a JSON-ready dictionary."""
        data = super().to_dict()
        data["status"] = self.status
        data["api_code"] = self.api_code
        return data


class RateLimitError(ApiError):
    """The API rate limit stopped the request, and the retries did not succeed."""

    code = "rate_limited"


class UnsupportedReleaseError(ReepError):
    """The release uses a format or a value that this version of reep does not know."""

    code = "unsupported_release"
    exit_code = 7


class CatalogMissingError(ReepError):
    """No local catalogue exists at the expected location."""

    code = "catalog_missing"
    exit_code = 7


class CatalogInUseError(ReepError):
    """The catalogue file cannot be replaced because another process has it open.

    Windows does not allow a file to be replaced while it is open.
    """

    code = "catalog_in_use"
    exit_code = 7


class ReplayError(ReepError):
    """A changelog cannot be replayed onto the local catalogue.

    The catalogue sync catches this error and downloads a full snapshot instead.
    """

    code = "replay_error"
    exit_code = 7
