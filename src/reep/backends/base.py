"""The interface that every backend implements."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from reep.models import Bridge, IdStatus, Resolution, ResolutionStatus

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from reep.models import (
        Entity,
        EntityMatch,
        IdState,
        ProviderId,
        ProviderNamespace,
        ReleaseInfo,
    )

__all__ = ["Backend", "resolution_from_states"]


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

    def bridges_many(
        self, reep_ids: Sequence[str], provider: str, namespace: str | None = None
    ) -> dict[str, list[Bridge]]:
        """Return the bridges of many entities at one provider."""
        ...

    def entities_many(
        self, reep_ids: Sequence[str], *, follow_redirects: bool = True
    ) -> dict[str, Entity]:
        """Return many entities with their bridges and aliases."""
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


def resolution_from_states(
    pid: ProviderId,
    found: Sequence[tuple[str, str | None, str | None]],
    states: Mapping[str, IdState],
    stamp: str | None,
) -> Resolution:
    """Return the resolution of a provider ID from its bridges and their states.

    Both backends use this function, so they decide each case the same way.

    Args:
        pid: The provider ID.
        found: ``(reep_id, rung, upstream_status)`` of each bridge of ``pid``.
        states: The state of each Reep ID in ``found``, after its redirects.
        stamp: The release stamp.

    Returns:
        The resolution: not found, ambiguous (bridges whose current IDs differ),
        withdrawn, or found (with ``redirected_from`` for a merged ID).
    """
    if not found:
        return Resolution(
            query=pid, status=ResolutionStatus.NOT_FOUND, release_stamp=stamp
        )
    if len({rid for rid, _, _ in found}) > 1:
        candidates = tuple(
            sorted({states[rid].current_id or rid for rid, _, _ in found})
        )
        if len(candidates) > 1:
            return Resolution(
                query=pid,
                status=ResolutionStatus.AMBIGUOUS,
                candidates=candidates,
                release_stamp=stamp,
            )
    rid, rung, upstream = found[0]
    bridge = Bridge(provider_id=pid, reep_id=rid, rung=rung, upstream_status=upstream)
    state = states[rid]
    if state.status not in (
        IdStatus.ACTIVE,
        IdStatus.PUBLISHED,
        IdStatus.RETIRED,
        IdStatus.REDIRECTED,
    ):
        # Withdrawn, or an ID the release does not publish (UNKNOWN): no current ID.
        reason = state.reason
        if state.status is IdStatus.UNKNOWN:
            reason = "target_not_published"
        return Resolution(
            query=pid,
            status=ResolutionStatus.WITHDRAWN,
            bridge=bridge,
            withdrawn_reason=reason,
            entity_type=state.entity_type,
            release_stamp=stamp,
        )
    if state.status is IdStatus.REDIRECTED:
        return Resolution(
            query=pid,
            status=ResolutionStatus.FOUND,
            reep_id=state.current_id,
            entity_type=state.entity_type,
            label=state.label,
            entity_status=state.current_status,
            bridge=bridge,
            redirected_from=rid,
            release_stamp=stamp,
        )
    return Resolution(
        query=pid,
        status=ResolutionStatus.FOUND,
        reep_id=rid,
        entity_type=state.entity_type,
        label=state.label,
        entity_status=state.current_status,
        bridge=bridge,
        release_stamp=stamp,
    )
