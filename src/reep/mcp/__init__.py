"""A read-only MCP server for Reep lookups (``reep mcp``).

The server gives AI agents the SDK lookups as MCP tools. Each tool returns the same
JSON envelope as the command line's ``--json`` output, and lists are bounded. The
server does not write anything: database sync is not available over MCP.

It needs the ``mcp`` extra: ``pip install 'reep[mcp]'``.

Example:
    Register the server with an MCP host, for example Claude Code::

        claude mcp add reep -- reep mcp
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from reep._status import status_data
from reep._version import __version__
from reep.cli.output import Output
from reep.config import Settings
from reep.errors import InvalidArgumentError, ReepError

if TYPE_CHECKING:
    from collections.abc import Callable

    from reep.register import Register

__all__ = ["INSTRUCTIONS", "MAX_LIMIT", "TOOLS", "create_server"]

#: The most rows a list tool returns in one call.
MAX_LIMIT = 50

#: The tool names, in the order the server lists them.
TOOLS = (
    "resolve",
    "resolve_many",
    "translate",
    "translate_many",
    "current",
    "entity",
    "search",
    "providers",
    "status",
)

#: The instructions the server gives to MCP hosts.
INSTRUCTIONS = """\
Look up football (soccer) provider IDs in the Reep Register, an open crosswalk of
provider IDs. Rules:
1. A provider ID is a triple: provider, namespace and external ID, for example
   transfermarkt / spieler / 568177. The same number names different entities in
   different namespaces. Use the providers tool when you do not know the namespace.
2. Check data.status before you use data.reep_id: found, not_found, ambiguous or
   withdrawn.
3. A stored Reep ID can merge into another or be withdrawn: use the current tool.
4. Use search only to find candidates for a person to confirm. Names are not unique,
   and the register has no dates of birth.
5. Do not guess IDs. When a lookup finds nothing, say so.
"""

EntityType = Literal[
    "player", "coach", "referee", "team", "competition", "season", "stage", "match"
]


def _error_text(exc: ReepError) -> str:
    text = f"{exc.code}: {exc.message}"
    if exc.hint:
        text += f" Hint: {exc.hint}"
    return text


def create_server(
    open_register: Callable[[], Register],
    *,
    settings: Settings | None = None,
    open_world: bool = False,
    name: str = "reep",
) -> MCPServer:
    """Create the MCP server.

    The server opens the register on the first tool call, so that a missing
    catalogue shows up as a tool error with a hint that an agent can act on.

    Args:
        open_register: Returns the register that answers the tools.
        settings: The settings for the status tool; read from the environment
            when ``None``.
        open_world: ``True`` when the register calls the hosted API.
        name: The server name.

    Returns:
        The server. Call ``run()`` to serve over stdio.
    """
    server: MCPServer = MCPServer(
        name,
        title="Reep Register",
        description="Read-only lookups of football provider IDs in the Reep Register.",
        instructions=INSTRUCTIONS,
        website_url="https://reep.football",
        version=__version__,
    )
    lock = threading.Lock()
    state: dict[str, Register] = {}
    annotations = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=open_world,
    )

    def run(
        command: str,
        answer: Callable[[Register], Any],
        *,
        page: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        with lock:
            try:
                if "register" not in state:
                    state["register"] = open_register()
                reg = state["register"]
                data = answer(reg)
                next_offset: int | None = None
                more = False
                if page is not None and isinstance(data, tuple):
                    rows, more = data
                    data = rows
                    offset, limit = page
                    next_offset = offset + limit if more else None
                return Output(command, "json").envelope(
                    data,
                    release_stamp=reg.release.stamp,
                    backend=reg.backend.name,
                    truncated=more,
                    next_offset=next_offset,
                )
            except ReepError as exc:
                raise ToolError(_error_text(exc)) from exc

    def check_page(limit: int, offset: int) -> None:
        if not 1 <= limit <= MAX_LIMIT:
            raise ToolError(
                f"invalid_argument: limit must be between 1 and {MAX_LIMIT}."
            )
        if offset < 0:
            raise ToolError("invalid_argument: offset must be 0 or more.")

    @server.tool(title="Resolve a provider ID", annotations=annotations)
    def resolve(provider: str, namespace: str, external_id: str) -> dict[str, Any]:
        """Resolve a provider ID to its current Reep ID.

        Args:
            provider: The provider slug, for example "transfermarkt".
            namespace: The provider's namespace, for example "spieler".
            external_id: The provider's own ID, as text, for example "568177".
        """
        return run(
            "resolve",
            lambda reg: reg.resolve((provider, namespace, external_id)),
        )

    @server.tool(title="Resolve many provider IDs", annotations=annotations)
    def resolve_many(ids: list[str]) -> dict[str, Any]:
        """Resolve up to 50 provider IDs ("provider/namespace/external_id") in one call.

        Args:
            ids: Provider ID triples such as ["transfermarkt/spieler/568177"].
        """
        if not 1 <= len(ids) <= MAX_LIMIT:
            raise ToolError(
                f"invalid_argument: ids must contain between 1 and {MAX_LIMIT} items."
            )
        return run("resolve", lambda reg: reg.resolve_many(ids))

    @server.tool(title="Translate a provider ID", annotations=annotations)
    def translate(
        provider: str,
        namespace: str,
        external_id: str,
        to: str,
        to_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Return another provider's IDs for the entity behind a provider ID.

        Args:
            provider: The source provider slug, for example "transfermarkt".
            namespace: The source namespace, for example "spieler".
            external_id: The source provider's ID, for example "568177".
            to: The target provider, for example "fbref".
            to_namespace: Only this namespace of the target provider.
        """
        return run(
            "translate",
            lambda reg: reg.translate(
                (provider, namespace, external_id), to, namespace=to_namespace
            ),
        )

    @server.tool(title="Translate many provider IDs", annotations=annotations)
    def translate_many(
        ids: list[str],
        to: str,
        to_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Translate up to 50 provider IDs to another provider.

        Args:
            ids: Source provider ID triples such as ["transfermarkt/spieler/568177"].
            to: The target provider, for example "fbref".
            to_namespace: Only this namespace of the target provider.
        """
        if not 1 <= len(ids) <= MAX_LIMIT:
            raise ToolError(
                f"invalid_argument: ids must contain between 1 and {MAX_LIMIT} items."
            )
        return run(
            "translate",
            lambda reg: reg.translate_many(ids, to, namespace=to_namespace),
        )

    @server.tool(title="Check a stored Reep ID", annotations=annotations)
    def current(reep_id: str) -> dict[str, Any]:
        """Return the state of a stored Reep ID.

        The status is active, retired, published (present, lifecycle unreported),
        redirected (store current_id), withdrawn (do not use the ID) or unknown.

        Args:
            reep_id: The Reep ID, for example "rp53af22bbeaa667".
        """
        return run("current", lambda reg: reg.current(reep_id))

    @server.tool(title="Show an entity", annotations=annotations)
    def entity(reep_id: str) -> dict[str, Any]:
        """Return an entity with its label and all its provider IDs.

        Args:
            reep_id: The Reep ID, for example "rp53af22bbeaa667".
        """
        return run("show", lambda reg: reg.entity(reep_id))

    @server.tool(title="Search entities by name", annotations=annotations)
    def search(
        query: str,
        entity_type: EntityType | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Search entities by name, for discovery only.

        Names are not unique and the register has no dates of birth: show the
        candidates to a person, or match records by provider ID with resolve.

        Args:
            query: The name, for example "Cole Palmer".
            entity_type: Only this entity type.
            limit: The most results (1 to 50).
            offset: The number of results to skip.
        """
        check_page(limit, offset)

        def answer(reg: Register) -> tuple[list[Any], bool]:
            found = reg.search(
                query, entity_type=entity_type, limit=limit + 1, offset=offset
            )
            return found[:limit], len(found) > limit

        return run("search", answer, page=(offset, limit))

    @server.tool(title="List providers and namespaces", annotations=annotations)
    def providers(
        provider: str | None = None, limit: int = MAX_LIMIT, offset: int = 0
    ) -> dict[str, Any]:
        """List the (provider, namespace) pairs in the register with bridge counts.

        Args:
            provider: Only this provider, for example "transfermarkt".
            limit: The most rows (1 to 50).
            offset: The number of rows to skip.
        """
        check_page(limit, offset)

        def answer(reg: Register) -> tuple[list[Any], bool]:
            rows = reg.providers()
            if provider is not None:
                key = provider.strip().lower()
                rows = [r for r in rows if r.provider == key]
                if not rows:
                    raise InvalidArgumentError(
                        f"The release has no provider {provider!r}.",
                        hint="Call providers without a provider to list them all.",
                    )
            return rows[offset : offset + limit], len(rows) > offset + limit

        return run("providers", answer, page=(offset, limit))

    @server.tool(title="Show the register in use", annotations=annotations)
    def status() -> dict[str, Any]:
        """Return the local catalogue, its release and age, and the backend in use.

        When backend is null, the register is not downloaded: tell the user that
        'reep catalog sync' downloads about 230 MB (no key needed).
        """
        try:
            data, stamp = status_data(settings or Settings.from_env())
        except ReepError as exc:
            raise ToolError(_error_text(exc)) from exc
        return Output("status", "json").envelope(
            data, release_stamp=stamp, backend=data["backend"]
        )

    return server
