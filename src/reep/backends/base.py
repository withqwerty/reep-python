"""The interface that every backend implements."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from reep.models import (
        Entity,
        EntityMatch,
        IdState,
        ProviderId,
        ProviderNamespace,
        ReleaseInfo,
        Resolution,
    )

__all__ = ["Backend"]


class Backend(Protocol):
    """Answers lookups against one release.

    ``Register`` validates its arguments before it calls a backend, so a backend
    receives valid provider IDs and Reep IDs only.
    """

    #: ``"local"`` or ``"api"``.
    name: str

    def release_info(self) -> ReleaseInfo:
        """Return the release that the backend reads."""
        ...

    def providers(self) -> list[ProviderNamespace]:
        """Return the ``(provider, namespace)`` pairs with their bridge counts."""
        ...

    def resolve_many(self, pids: Sequence[ProviderId]) -> list[Resolution]:
        """Resolve provider IDs, in the order given."""
        ...

    def resolve_ids(
        self, provider: str, namespace: str, external_ids: Sequence[str]
    ) -> list[tuple[str | None, str]]:
        """Resolve many IDs of one ``(provider, namespace)`` pair.

        Returns:
            One ``(reep_id, status)`` pair per input ID, in the order given. The
            status is a ``ResolutionStatus`` value.
        """
        ...

    def current_many(self, reep_ids: Sequence[str]) -> list[IdState]:
        """Return the state of stored Reep IDs, in the order given."""
        ...

    def entity(self, reep_id: str, *, follow_redirects: bool = True) -> Entity | None:
        """Return an entity with its bridges, or ``None`` if the release lacks it."""
        ...

    def search(
        self,
        query: str,
        *,
        entity_type: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[EntityMatch]:
        """Search entities by name."""
        ...

    def close(self) -> None:
        """Release the backend's resources."""
        ...
