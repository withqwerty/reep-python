"""Value types returned by the reep SDK.

All models are frozen dataclasses. Each one has a ``to_dict`` method that returns
JSON-ready data, which the CLI and the MCP server use for their output.
"""

from __future__ import annotations

import dataclasses
import enum
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from reep.errors import InvalidProviderIdError, InvalidReepIdError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "ENTITY_TYPES",
    "ENTITY_TYPE_BY_LETTER",
    "REEP_ID_PATTERN",
    "WITHDRAWN_REASONS",
    "Bridge",
    "Entity",
    "EntityMatch",
    "IdState",
    "IdStatus",
    "ProviderId",
    "ProviderNamespace",
    "ReleaseInfo",
    "Resolution",
    "ResolutionStatus",
    "Translation",
    "entity_type_of",
    "to_jsonable",
    "validate_reep_id",
]

#: A Reep ID: ``r``, one entity-type letter, then 14 lower-case hex digits.
REEP_ID_PATTERN = re.compile(
    r"^(?:r[acglmopstvx][0-9a-f]{14}|[aclmoprstvx]_[0-9a-f]{16})$"
)

#: The entity type that each Reep ID letter stands for.
ENTITY_TYPE_BY_LETTER: dict[str, str] = {
    "c": "coach",
    "g": "stage",
    "l": "competition",
    "m": "match",
    "o": "referee",
    "p": "player",
    "s": "season",
    "t": "team",
    "v": "venue",
    "x": "transfer",
    "a": "award",
}

#: The entity types in the register.
ENTITY_TYPES: tuple[str, ...] = (
    "player",
    "coach",
    "referee",
    "team",
    "competition",
    "season",
    "stage",
    "match",
    "venue",
    "transfer",
    "award",
)

#: Redirect reasons that withdraw an ID without naming a survivor.
WITHDRAWN_REASONS = frozenset({"target_not_published", "de_corroborated", "withheld"})

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


def validate_reep_id(value: object) -> str:
    """Return ``value`` if it is a valid Reep ID.

    Args:
        value: The value to check.

    Returns:
        The Reep ID, with surrounding whitespace removed.

    Raises:
        InvalidReepIdError: If the value is not a Reep ID.
    """
    if not isinstance(value, str):
        raise InvalidReepIdError(
            f"A Reep ID must be a string, not {type(value).__name__}.",
            hint="Reep IDs look like 'rp53af22bbeaa667'.",
        )
    text = value.strip()
    if not REEP_ID_PATTERN.match(text):
        raise InvalidReepIdError(
            f"{value!r} is not a Reep ID.",
            hint=(
                "A Reep ID is 'r', a type letter (c, g, l, m, o, p, s or t) and 14 "
                "hex digits, for example 'rp53af22bbeaa667'. To look up a provider "
                "ID, use resolve with provider, namespace and external ID."
            ),
        )
    return text


def entity_type_of(reep_id: str) -> str:
    """Return the entity type encoded in a Reep ID.

    Args:
        reep_id: A valid Reep ID.

    Returns:
        The entity type, for example ``"player"``.
    """
    rid = validate_reep_id(reep_id)
    letter = ("g" if rid[0] == "r" else rid[0]) if rid[1] == "_" else rid[1]
    return ENTITY_TYPE_BY_LETTER[letter]


@dataclass(frozen=True, slots=True)
class ProviderId:
    """A provider's ID for an entity: the ``(provider, namespace, external_id)`` triple.

    A provider ID is only meaningful with all three parts. For example, Transfermarkt
    uses the same number in different namespaces for a player (``spieler``) and a
    coach (``trainer``).

    Provider and namespace are stored in lower case. Surrounding whitespace is removed
    from all three parts.

    Args:
        provider: The provider slug, for example ``"transfermarkt"``.
        namespace: The provider's ID namespace, for example ``"spieler"``.
        external_id: The provider's own ID, as a string.

    Raises:
        InvalidProviderIdError: If a part is missing, empty or not a string.

    Example:
        >>> ProviderId("transfermarkt", "spieler", "568177")
        ProviderId(provider='transfermarkt', namespace='spieler', external_id='568177')
    """

    provider: str
    namespace: str
    external_id: str

    def __post_init__(self) -> None:
        """Validate and normalise the three parts."""
        for name in ("provider", "namespace", "external_id"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise InvalidProviderIdError(
                    f"ProviderId.{name} must be a string, not {type(value).__name__} "
                    f"({value!r}).",
                    hint=(
                        "Pass all three parts as strings, for example "
                        "ProviderId('transfermarkt', 'spieler', '568177'). A bare "
                        "number is not an ID: the same number names different "
                        "entities in different namespaces."
                    ),
                )
            stripped = value.strip()
            if not stripped:
                raise InvalidProviderIdError(
                    f"ProviderId.{name} is empty.",
                    hint="A provider ID needs a provider, a namespace and an ID.",
                )
            if name != "external_id":
                stripped = stripped.lower()
                if not _SLUG.match(stripped):
                    raise InvalidProviderIdError(
                        f"ProviderId.{name} {value!r} is not a valid slug.",
                        hint="Providers and namespaces are lower-case slugs, "
                        "for example 'transfermarkt' and 'spieler'.",
                    )
            object.__setattr__(self, name, stripped)

    @classmethod
    def parse(cls, text: object) -> ProviderId:
        """Parse ``provider/namespace/external_id`` (or with ``:`` separators).

        The external ID can contain the separator; only the first two separators
        split the text.

        Args:
            text: The text to parse.

        Returns:
            The provider ID.

        Raises:
            InvalidProviderIdError: If the text does not have three parts.

        Example:
            >>> ProviderId.parse("transfermarkt/spieler/568177").namespace
            'spieler'
        """
        if not isinstance(text, str):
            raise InvalidProviderIdError(
                f"Cannot parse {type(text).__name__} {text!r} as a provider ID.",
                hint="Use the form provider/namespace/external_id, "
                "for example transfermarkt/spieler/568177.",
            )
        stripped = text.strip()
        separator = "/" if "/" in stripped else ":"
        parts = stripped.split(separator, 2)
        if len(parts) != 3:
            raise InvalidProviderIdError(
                f"{text!r} does not have three parts.",
                hint="Use the form provider/namespace/external_id, for example "
                "transfermarkt/spieler/568177. Run 'reep providers' to list the "
                "namespaces of each provider.",
            )
        return cls(parts[0], parts[1], parts[2])

    def __str__(self) -> str:
        """Return the ID as ``provider/namespace/external_id``."""
        return f"{self.provider}/{self.namespace}/{self.external_id}"

    def to_dict(self) -> dict[str, str]:
        """Return the three parts as a dictionary."""
        return {
            "provider": self.provider,
            "namespace": self.namespace,
            "external_id": self.external_id,
        }


class ResolutionStatus(enum.StrEnum):
    """The outcome of resolving a provider ID."""

    #: The provider ID resolves to a current Reep ID.
    FOUND = "found"
    #: The release has no bridge for the provider ID.
    NOT_FOUND = "not_found"
    #: The provider ID names more than one entity.
    AMBIGUOUS = "ambiguous"
    #: The provider ID names an entity that the release withdraws, with no survivor.
    WITHDRAWN = "withdrawn"


class IdStatus(enum.StrEnum):
    """The state of a stored Reep ID in a release."""

    #: The ID is current.
    ACTIVE = "active"
    #: Present in a v4 download; entity lifecycle is not published.
    PUBLISHED = "published"
    #: The entity is retired; the ID is not reused.
    RETIRED = "retired"
    #: The ID merged into a survivor; use ``IdState.current_id``.
    REDIRECTED = "redirected"
    #: The release withdraws the ID and names no survivor; see ``IdState.reason``.
    WITHDRAWN = "withdrawn"
    #: The release does not know the ID.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Bridge:
    """One provider ID attached to a Reep entity.

    Attributes:
        provider_id: The provider ID.
        reep_id: The Reep ID that the bridge attaches the provider ID to.
        rung: The kind of agreement that attached the ID (for example
            ``"name-dob"``), or ``None`` when the release records no rung or the
            backend does not return it.
        upstream_status: ``None`` when unreported (including all v4 downloads),
            ``"retired"`` when the provider
            retired the ID in favour of another, or ``"removed"`` when the provider's
            newest complete registry no longer lists it.
    """

    provider_id: ProviderId
    reep_id: str
    rung: str | None = None
    upstream_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the bridge as a JSON-ready dictionary."""
        return {
            **self.provider_id.to_dict(),
            "reep_id": self.reep_id,
            "rung": self.rung,
            "upstream_status": self.upstream_status,
        }


@dataclass(frozen=True, slots=True)
class Resolution:
    """The result of resolving one provider ID.

    Attributes:
        query: The provider ID that was resolved.
        status: The outcome.
        reep_id: The current Reep ID when ``status`` is ``FOUND``. When the bridge
            names an ID that merged into a survivor, this is the survivor.
        entity_type: The entity type, when found.
        label: The entity's public label, when found.
        entity_status: ``"active"`` or ``"retired"``, when found.
        bridge: The matched bridge with its rung and upstream status.
        redirected_from: The Reep ID that the bridge names, when it merged into
            ``reep_id``.
        withdrawn_reason: The redirect reason when ``status`` is ``WITHDRAWN``.
        candidates: The Reep IDs when ``status`` is ``AMBIGUOUS``.
        release_stamp: The release that answered the lookup.
    """

    query: ProviderId
    status: ResolutionStatus
    reep_id: str | None = None
    entity_type: str | None = None
    label: str | None = None
    entity_status: str | None = None
    bridge: Bridge | None = None
    redirected_from: str | None = None
    withdrawn_reason: str | None = None
    candidates: tuple[str, ...] = ()
    release_stamp: str | None = None

    @property
    def found(self) -> bool:
        """Return ``True`` when the provider ID resolves to a current Reep ID."""
        return self.status is ResolutionStatus.FOUND

    def to_dict(self) -> dict[str, Any]:
        """Return the resolution as a JSON-ready dictionary."""
        return _fields_dict(self)


@dataclass(frozen=True, slots=True)
class IdState:
    """The state of a stored Reep ID.

    Attributes:
        reep_id: The Reep ID that was checked.
        status: The state.
        current_id: The ID to store now: the same ID when active or retired, the
            survivor when redirected, and ``None`` when withdrawn or unknown.
        current_status: The status of the entity behind ``current_id``:
            ``"active"`` or ``"retired"``.
        reason: The redirect reason, when redirected or withdrawn.
        entity_type: The entity type.
        label: The public label of the entity behind ``current_id`` (or of the
            requested ID when it is withdrawn).
        release_stamp: The release that answered the lookup.
    """

    reep_id: str
    status: IdStatus
    current_id: str | None = None
    current_status: str | None = None
    reason: str | None = None
    entity_type: str | None = None
    label: str | None = None
    release_stamp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the state as a JSON-ready dictionary."""
        return _fields_dict(self)


@dataclass(frozen=True, slots=True)
class Entity:
    """A Reep entity with its bridges.

    Attributes:
        reep_id: The entity's Reep ID.
        entity_type: The entity type.
        status: ``"active"``, ``"redirected"``, ``"retired"`` or ``None``
            when unreported.
        label: The public label.
        gender: ``"men"``, ``"women"`` or ``None``.
        country: The country claim, or ``None``.
        corroboration_grade: ``"A"``, ``"B"`` or ``"S"``, or ``None``.
        corroboration_count: The number of independence buckets, or ``None``.
        bridges: The provider IDs attached to the entity.
        aliases: Alternative names, when the backend has them.
        redirected_from: The Reep ID that was requested, when it merged into this
            entity.
        release_stamp: The release that answered the lookup.
    """

    reep_id: str
    entity_type: str
    status: str | None
    label: str | None = None
    gender: str | None = None
    country: str | None = None
    corroboration_grade: str | None = None
    corroboration_count: int | None = None
    bridges: tuple[Bridge, ...] = ()
    aliases: tuple[str, ...] = ()
    redirected_from: str | None = None
    release_stamp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the entity as a JSON-ready dictionary."""
        return _fields_dict(self)


@dataclass(frozen=True, slots=True)
class Translation:
    """The provider IDs of another provider for the entity behind a provider ID.

    Attributes:
        resolution: The resolution of the source provider ID.
        to: The target provider.
        targets: The target provider's bridges. Live IDs come first; IDs that the
            provider retired or removed follow.
    """

    resolution: Resolution
    to: str
    targets: tuple[Bridge, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the translation as a JSON-ready dictionary."""
        return _fields_dict(self)


@dataclass(frozen=True, slots=True)
class EntityMatch:
    """One result of a name search.

    Attributes:
        reep_id: The entity's Reep ID.
        entity_type: The entity type.
        label: The public label.
        status: The entity status, when the backend returns it.
        bridge_count: The number of provider IDs attached to the entity.
        rank: 0 for an exact label match, 1 for a label prefix, 2 when the label
            contains the query, 3 when only another name matches.
        gender: ``"men"``, ``"women"`` or ``None``; separates namesakes such as a
            club's men's and women's teams.
        country: The country claim, or ``None``.
    """

    reep_id: str
    entity_type: str
    label: str | None
    status: str | None
    bridge_count: int
    rank: int
    gender: str | None = None
    country: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the match as a JSON-ready dictionary."""
        return _fields_dict(self)


@dataclass(frozen=True, slots=True)
class ProviderNamespace:
    """A ``(provider, namespace)`` pair in a release.

    Attributes:
        provider: The provider slug.
        namespace: The namespace, or ``None`` when the backend does not report
            namespaces.
        bridge_count: The number of bridges in the pair.
        entity_types: The entity types that the pair's IDs name.
    """

    provider: str
    namespace: str | None
    bridge_count: int
    entity_types: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the pair as a JSON-ready dictionary."""
        return _fields_dict(self)


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    """The release that a backend reads.

    Attributes:
        stamp: The release stamp, for example ``"20261005T180536Z"``, or ``None``
            when unknown.
        schema_version: The release schema version.
        backend: ``"local"`` or ``"api"``.
        source: How the data got here: ``"download"``, ``"import"``,
            ``"replay"``, ``"shipped_duckdb"`` or ``"api"``.
        profile: ``"slim"``, ``"full"`` or ``None``.
        generated_at: When the release was generated, if known.
        path: The local DuckDB file, for the local backend.
        tables: The tables in the local store.
        bridge_attributes_stamp: The release that the bridge ``rung`` and
            ``upstream_status`` values come from. A changelog sync updates the
            bridges but not these two columns.
        built_at: When the local store was built, if known.
        extra: Other metadata that the backend reports.
    """

    stamp: str | None
    schema_version: str | None
    backend: str
    source: str
    profile: str | None = None
    generated_at: str | None = None
    path: Path | None = None
    tables: tuple[str, ...] = ()
    bridge_attributes_stamp: str | None = None
    built_at: str | None = None
    extra: dict[str, str] = field(default_factory=dict[str, str])

    def to_dict(self) -> dict[str, Any]:
        """Return the release information as a JSON-ready dictionary."""
        return _fields_dict(self)


def to_jsonable(value: Any) -> Any:
    """Convert models, enums, paths and containers into JSON-ready values.

    Args:
        value: The value to convert.

    Returns:
        A structure of dictionaries, lists, strings, numbers, booleans and ``None``.
    """
    if isinstance(value, ProviderId):
        return value.to_dict()
    if isinstance(value, Bridge):
        return value.to_dict()
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        return {str(k): to_jsonable(v) for k, v in mapping.items()}
    if isinstance(value, (list, tuple)):
        items = cast("list[object] | tuple[object, ...]", value)
        return [to_jsonable(v) for v in items]
    if hasattr(value, "__fspath__"):
        return str(value)
    return value


def _fields_dict(value: object) -> dict[str, Any]:
    """Return a dataclass instance's fields as JSON-ready values."""
    return {
        f.name: to_jsonable(getattr(value, f.name))
        for f in dataclasses.fields(cast("Any", value))
    }
