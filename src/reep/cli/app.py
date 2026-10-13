"""The ``reep`` command.

Exit codes: 0 success, 1 internal error, 2 usage error, 3 not found, 4 needs
review, 5 authentication, 6 network or upstream error, 7 data error.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import click

from reep import auth, catalog
from reep._version import __version__
from reep.cli.output import (
    Output,
    emit_error,
    is_interactive,
    render_fields,
    render_table,
)
from reep.cli.schemas import COMMANDS, envelope_schema, error_schema
from reep.config import Settings
from reep.errors import (
    AuthError,
    CatalogMissingError,
    InvalidArgumentError,
    NetworkError,
    ReepError,
)
from reep.models import (
    Entity,
    IdState,
    ProviderId,
    Resolution,
    Translation,
)
from reep.register import Register
from reep.release import ReleaseClient

__all__ = ["cli", "main"]

F = TypeVar("F", bound=Callable[..., Any])

EXIT_OK = 0
EXIT_NOT_FOUND = 3
DEFAULT_LIMIT = 50

_CONTEXT = {"help_option_names": ["-h", "--help"], "max_content_width": 100}


class _InternalError(ReepError):
    code = "internal_error"
    exit_code = 1


class _UsageError(ReepError):
    code = "usage_error"
    exit_code = 2


# -- shared options ------------------------------------------------------------


def output_options(func: F) -> F:
    """Add ``--json`` and ``--jsonl``."""
    func = click.option(
        "--jsonl",
        "fmt",
        flag_value="jsonl",
        help="Print one JSON object per row (no envelope).",
    )(func)
    return click.option(
        "--json",
        "fmt",
        flag_value="json",
        help="Print one JSON document with the envelope (see 'reep schema').",
    )(func)


def backend_options(func: F) -> F:
    """Add ``--api``, ``--local`` and ``--db``."""
    func = click.option(
        "--db",
        type=click.Path(dir_okay=False, path_type=Path),
        help="Read this DuckDB file (a shipped reep-register-v1.duckdb or an SDK "
        "catalogue).",
    )(func)
    return click.option(
        "--api/--local",
        "use_api",
        default=None,
        help="Use the hosted API (needs a key) or the local catalogue. Default: the "
        "local catalogue when one exists, otherwise the API when a key is set.",
    )(func)


def page_options(func: F) -> F:
    """Add ``--limit`` and ``--offset``."""
    func = click.option(
        "--offset", type=click.IntRange(min=0), default=0, show_default=True
    )(func)
    return click.option(
        "--limit",
        type=click.IntRange(1, 1000),
        default=DEFAULT_LIMIT,
        show_default=True,
        help="The most rows to return.",
    )(func)


def _settings() -> Settings:
    return Settings.from_env()


def open_register(
    settings: Settings, *, use_api: bool | None, db: Path | None
) -> Register:
    """Open the register that the options and environment choose.

    Args:
        settings: The settings.
        use_api: ``True`` for the API, ``False`` for the local catalogue, ``None``
            to choose.
        db: A DuckDB file to read.

    Returns:
        The register.

    Raises:
        CatalogMissingError: If no local catalogue exists and no API key is set.
    """
    if db is not None:
        return Register.local(db, settings=settings)
    if use_api is True:
        return Register.api(settings=settings)
    if use_api is False:
        return Register.local(settings=settings)
    if settings.duckdb_path is not None or settings.catalog_path.is_file():
        return Register.local(settings=settings)
    if auth.load_key(settings):
        return Register.api(settings=settings)
    raise CatalogMissingError(
        "No local catalogue and no API key.",
        hint="Run 'reep catalog sync' to download the register (free, no key), set "
        "REEP_DUCKDB_PATH to a downloaded reep-register-v1.duckdb file, or set "
        "REEP_API_KEY.",
    )


def _emit(
    out: Output,
    reg: Register,
    data: Any,
    text: str,
    **kwargs: Any,
) -> None:
    out.emit(
        data,
        release_stamp=reg.release.stamp,
        backend=reg.backend.name,
        text=text,
        **kwargs,
    )


# -- formatting ----------------------------------------------------------------


def _resolution_text(res: Resolution) -> str:
    rung = res.bridge.rung if res.bridge else None
    upstream = res.bridge.upstream_status if res.bridge else None
    return render_fields(
        [
            ("query", str(res.query)),
            ("status", res.status.value),
            ("reep_id", res.reep_id),
            ("type", res.entity_type),
            ("label", res.label),
            ("entity status", res.entity_status),
            ("merged from", res.redirected_from),
            ("withdrawn", res.withdrawn_reason),
            ("candidates", list(res.candidates)),
            ("rung", rung),
            ("upstream", upstream),
        ]
    )


def _resolutions_table(results: Sequence[Resolution]) -> str:
    return render_table(
        [
            {
                "query": str(r.query),
                "status": r.status.value,
                "reep_id": r.reep_id or "",
                "label": r.label,
                "note": (r.redirected_from and f"merged from {r.redirected_from}")
                or r.withdrawn_reason
                or ("candidates: " + ", ".join(r.candidates) if r.candidates else ""),
            }
            for r in results
        ],
        [
            ("query", "QUERY"),
            ("status", "STATUS"),
            ("reep_id", "REEP ID"),
            ("label", "LABEL"),
            ("note", "NOTE"),
        ],
    )


def _state_text(state: IdState) -> str:
    return render_fields(
        [
            ("reep_id", state.reep_id),
            ("status", state.status.value),
            ("current_id", state.current_id),
            ("current status", state.current_status),
            ("reason", state.reason),
            ("type", state.entity_type),
            ("label", state.label),
        ]
    )


def _entity_text(entity: Entity) -> str:
    head = render_fields(
        [
            ("reep_id", entity.reep_id),
            ("type", entity.entity_type),
            ("status", entity.status),
            ("label", entity.label),
            ("gender", entity.gender),
            ("country", entity.country),
            ("grade", entity.corroboration_grade),
            ("merged from", entity.redirected_from),
            ("aliases", list(entity.aliases[:10])),
        ]
    )
    table = render_table(
        [
            {
                "id": str(b.provider_id),
                "rung": b.rung,
                "upstream": b.upstream_status,
            }
            for b in entity.bridges
        ],
        [("id", "PROVIDER ID"), ("rung", "RUNG"), ("upstream", "UPSTREAM")],
    )
    return f"{head}\n\n{table}"


def _read_stdin_lines() -> list[str]:
    return [line.strip() for line in sys.stdin if line.strip()]


def _parse_ids(values: Sequence[str]) -> list[ProviderId]:
    if values == ("-",) or list(values) == ["-"]:
        values = _read_stdin_lines()
    if len(values) == 3 and not any(("/" in v or ":" in v) for v in values):
        return [ProviderId(values[0], values[1], values[2])]
    out = []
    for value in values:
        parts = [p.strip() for p in value.replace("\t", ",").split(",")]
        if len(parts) == 3 and "/" not in value:
            out.append(ProviderId(*parts))
        else:
            out.append(ProviderId.parse(value))
    return out


# -- commands ------------------------------------------------------------------


@click.group(context_settings=_CONTEXT, invoke_without_command=True)
@click.version_option(__version__, "--version", message="reep %(version)s")
@click.option(
    "--no-input",
    is_flag=True,
    help="Never prompt. This is the behaviour when stdin or stdout is not a terminal.",
)
@click.option("-v", "--verbose", is_flag=True, help="Log progress to stderr.")
@click.pass_context
def cli(ctx: click.Context, no_input: bool, verbose: bool) -> None:
    """Look up football provider IDs in the Reep Register.

    The register is an open crosswalk of football provider IDs. Every lookup uses a
    complete provider ID: (provider, namespace, external ID). A bare number is not
    an ID: Transfermarkt uses 568177 for a player (spieler) and for a coach
    (trainer).

    \b
    Start here:
      reep catalog sync                 download the register (about 210 MB, no key)
      reep providers --provider fbref   list a provider's namespaces
      reep resolve transfermarkt spieler 568177
      reep translate transfermarkt/spieler/568177 --to fbref
      reep status                       show the catalogue and the backend in use

    Add --json to any command for machine-readable output; 'reep schema <command>'
    prints its JSON Schema. Exit codes: 0 ok, 2 usage, 3 not found, 4 needs review,
    5 authentication, 6 network, 7 data.
    """
    ctx.ensure_object(dict)
    ctx.obj["no_input"] = no_input or not is_interactive()
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
@output_options
@click.option(
    "--check-latest",
    is_flag=True,
    help="Also read the current release stamp from the release bucket (network).",
)
def status(fmt: str | None, check_latest: bool) -> int:
    """Show the local catalogue, the API key source and the backend in use.

    The API key itself is not shown.

    \b
    Examples:
      reep status
      reep status --check-latest --json
    """
    out = Output("status", fmt or "table")
    settings = _settings()
    state = catalog.state(settings)
    catalog_data: dict[str, Any] | None = None
    if state is not None:
        age = None
        if state.built_at:
            built = datetime.fromisoformat(state.built_at)
            age = round((datetime.now(UTC) - built).total_seconds() / 86400, 1)
        catalog_data = {
            "path": str(state.path),
            "stamp": state.stamp,
            "profile": state.profile,
            "source": state.source,
            "built_at": state.built_at,
            "age_days": age,
            "bridge_attributes_stamp": state.bridge_attributes_stamp,
            "tables": list(state.tables),
        }
    duck: dict[str, Any] | None = None
    if settings.duckdb_path is not None:
        duck = {
            "path": str(settings.duckdb_path),
            "exists": settings.duckdb_path.is_file(),
        }
    key_source = auth.key_source(settings)
    if settings.duckdb_path is not None or state is not None:
        backend: str | None = "local"
    elif key_source:
        backend = "api"
    else:
        backend = None
    latest: str | None = None
    update: bool | None = None
    if check_latest:
        with ReleaseClient(settings.releases_url) as rc:
            latest = rc.latest().stamp
        update = bool(state and state.stamp != latest)
    data = {
        "backend": backend,
        "catalog": catalog_data,
        "duckdb_path": duck,
        "api": {"key_source": key_source, "base_url": settings.api_url},
        "releases_url": settings.releases_url,
        "latest_stamp": latest,
        "update_available": update,
    }
    stamp = state.stamp if state else None
    text = render_fields(
        [
            ("backend", backend or "none (run 'reep catalog sync')"),
            ("catalogue", catalog_data and catalog_data["path"]),
            ("release", stamp),
            ("profile", catalog_data and catalog_data["profile"]),
            ("built", catalog_data and catalog_data["built_at"]),
            ("age (days)", catalog_data and catalog_data["age_days"]),
            ("REEP_DUCKDB_PATH", duck and duck["path"]),
            ("API key", key_source or "none"),
            ("latest release", latest),
            ("update available", update),
        ]
    )
    out.emit(data, release_stamp=stamp, backend=backend, text=text)
    return EXIT_OK


@cli.command()
@output_options
@backend_options
@page_options
@click.option("--provider", help="Show only this provider.")
def providers(
    fmt: str | None,
    use_api: bool | None,
    db: Path | None,
    limit: int,
    offset: int,
    provider: str | None,
) -> int:
    """List the (provider, namespace) pairs in the release, with bridge counts.

    \b
    Examples:
      reep providers
      reep providers --provider transfermarkt
      reep providers --json --limit 500
    """
    out = Output("providers", fmt or "table")
    with open_register(_settings(), use_api=use_api, db=db) as reg:
        rows = reg.providers()
        if provider:
            key = provider.strip().lower()
            rows = [r for r in rows if r.provider == key]
            if not rows:
                reg.namespaces(key)  # raises UnknownProviderError with a hint
        page = rows[offset : offset + limit]
        truncated = offset + limit < len(rows)
        text = render_table(
            [
                {
                    "provider": r.provider,
                    "namespace": r.namespace or "",
                    "count": r.bridge_count,
                    "types": list(r.entity_types),
                }
                for r in page
            ],
            [
                ("provider", "PROVIDER"),
                ("namespace", "NAMESPACE"),
                ("count", "BRIDGES"),
                ("types", "ENTITY TYPES"),
            ],
        )
        _emit(
            out,
            reg,
            page,
            text,
            truncated=truncated,
            next_offset=offset + limit if truncated else None,
        )
    return EXIT_OK


@cli.command()
@click.argument("ids", nargs=-1, required=True)
@output_options
@backend_options
def resolve(
    ids: tuple[str, ...], fmt: str | None, use_api: bool | None, db: Path | None
) -> int:
    """Resolve provider IDs to current Reep IDs.

    Pass one ID as three words (provider namespace external_id), or one or more IDs
    as provider/namespace/external_id. Pass '-' to read IDs from stdin, one per line.
    A merged ID resolves to its survivor ('merged from' shows the old ID).

    Exit code 3 when a single ID does not resolve (not found, withdrawn or
    ambiguous). For several IDs the exit code is 0 and each row has a status.

    \b
    Examples:
      reep resolve transfermarkt spieler 568177
      reep resolve transfermarkt/spieler/568177 opta/person/p1 --json
      cat ids.txt | reep resolve - --jsonl
    """
    out = Output("resolve", fmt or "table")
    pids = _parse_ids(ids)
    if not pids:
        raise InvalidArgumentError("No IDs to resolve.")
    with open_register(_settings(), use_api=use_api, db=db) as reg:
        results = reg.resolve_many(pids)
        if len(results) == 1 and ids != ("-",):
            single = results[0]
            _emit(out, reg, single, _resolution_text(single))
            return EXIT_OK if single.found else EXIT_NOT_FOUND
        _emit(out, reg, results, _resolutions_table(results))
    return EXIT_OK


@cli.command()
@click.argument("reep_ids", nargs=-1, required=True)
@output_options
@backend_options
def current(
    reep_ids: tuple[str, ...], fmt: str | None, use_api: bool | None, db: Path | None
) -> int:
    """Show the state of stored Reep IDs: active, retired, redirected or withdrawn.

    Store 'current_id' when the status is redirected. Exit code 3 when a single ID
    has no current ID (withdrawn or unknown).

    \b
    Examples:
      reep current rc00cfa6784c6c23
      reep current rp53af22bbeaa667 rc00cfa6784c6c23 --json
    """
    out = Output("current", fmt or "table")
    values = _read_stdin_lines() if reep_ids == ("-",) else list(reep_ids)
    with open_register(_settings(), use_api=use_api, db=db) as reg:
        states = reg.current_many(values)
        if len(states) == 1 and reep_ids != ("-",):
            state = states[0]
            _emit(out, reg, state, _state_text(state))
            return EXIT_OK if state.current_id else EXIT_NOT_FOUND
        text = render_table(
            [
                {
                    "id": s.reep_id,
                    "status": s.status.value,
                    "current": s.current_id or "",
                    "reason": s.reason,
                    "label": s.label,
                }
                for s in states
            ],
            [
                ("id", "REEP ID"),
                ("status", "STATUS"),
                ("current", "CURRENT ID"),
                ("reason", "REASON"),
                ("label", "LABEL"),
            ],
        )
        _emit(out, reg, states, text)
    return EXIT_OK


@cli.command()
@click.argument("ids", nargs=-1, required=True)
@click.option(
    "--to", "to", required=True, help="The target provider, for example fbref."
)
@click.option("--namespace", help="Only this namespace of the target provider.")
@output_options
@backend_options
def translate(
    ids: tuple[str, ...],
    to: str,
    namespace: str | None,
    fmt: str | None,
    use_api: bool | None,
    db: Path | None,
) -> int:
    """Give another provider's IDs for the entity behind a provider ID.

    Exit code 3 when the source ID does not resolve or the entity has no ID at the
    target provider. IDs that the target provider retired or removed are listed
    after the live ones.

    \b
    Examples:
      reep translate transfermarkt spieler 568177 --to fbref
      reep translate transfermarkt/spieler/568177 --to opta --json
    """
    out = Output("translate", fmt or "table")
    pids = _parse_ids(ids)
    if len(pids) != 1:
        raise InvalidArgumentError("translate takes one provider ID.")
    with open_register(_settings(), use_api=use_api, db=db) as reg:
        result: Translation = reg.translate(pids[0], to, namespace=namespace)
        res = result.resolution
        head = render_fields(
            [
                ("query", str(res.query)),
                ("status", res.status.value),
                ("reep_id", res.reep_id),
                ("label", res.label),
            ]
        )
        table = render_table(
            [
                {
                    "id": str(t.provider_id),
                    "upstream": t.upstream_status,
                    "rung": t.rung,
                }
                for t in result.targets
            ],
            [
                ("id", f"{result.to.upper()} ID"),
                ("upstream", "UPSTREAM"),
                ("rung", "RUNG"),
            ],
        )
        _emit(out, reg, result, f"{head}\n\n{table}")
        return EXIT_OK if result.targets else EXIT_NOT_FOUND


@cli.command()
@click.argument("query", nargs=-1, required=True)
@click.option(
    "--type",
    "entity_type",
    type=click.Choice(
        [
            "player",
            "coach",
            "referee",
            "team",
            "competition",
            "season",
            "stage",
            "match",
        ]
    ),
    help="Only this entity type.",
)
@output_options
@backend_options
@page_options
def search(
    query: tuple[str, ...],
    entity_type: str | None,
    fmt: str | None,
    use_api: bool | None,
    db: Path | None,
    limit: int,
    offset: int,
) -> int:
    """Search entities by name, for discovery.

    Do not use search to match records: names are not unique and the release has
    no dates of birth. Match records by provider ID with 'reep resolve'. Exit code
    3 when nothing matches.

    \b
    Examples:
      reep search Cole Palmer --type player
      reep search Chelsea --type team --json --limit 5
    """
    out = Output("search", fmt or "table")
    text_query = " ".join(query)
    with open_register(_settings(), use_api=use_api, db=db) as reg:
        matches = reg.search(
            text_query, entity_type=entity_type, limit=limit + 1, offset=offset
        )
        truncated = len(matches) > limit
        page = matches[:limit]
        text = render_table(
            [
                {
                    "id": m.reep_id,
                    "type": m.entity_type,
                    "label": m.label,
                    "gender": m.gender,
                    "country": m.country,
                    "bridges": m.bridge_count,
                    "status": m.status,
                }
                for m in page
            ],
            [
                ("id", "REEP ID"),
                ("type", "TYPE"),
                ("label", "LABEL"),
                ("gender", "GENDER"),
                ("country", "COUNTRY"),
                ("bridges", "BRIDGES"),
                ("status", "STATUS"),
            ],
        )
        _emit(
            out,
            reg,
            page,
            text,
            truncated=truncated,
            next_offset=offset + limit if truncated else None,
        )
        return EXIT_OK if page else EXIT_NOT_FOUND


@cli.command()
@click.argument("reep_id")
@click.option(
    "--no-follow",
    is_flag=True,
    help="Show a merged ID itself instead of its survivor.",
)
@output_options
@backend_options
def show(
    reep_id: str,
    no_follow: bool,
    fmt: str | None,
    use_api: bool | None,
    db: Path | None,
) -> int:
    """Show an entity with all its provider IDs.

    \b
    Examples:
      reep show rp53af22bbeaa667
      reep show rc00cfa6784c6c23 --json
    """
    out = Output("show", fmt or "table")
    with open_register(_settings(), use_api=use_api, db=db) as reg:
        entity = reg.entity(reep_id, follow_redirects=not no_follow)
        _emit(out, reg, entity, _entity_text(entity))
    return EXIT_OK


# -- catalog -------------------------------------------------------------------


@cli.group(name="catalog", context_settings=_CONTEXT)
def catalog_group() -> None:
    """Download and update the local catalogue (no key needed)."""


def _progress_printer() -> Callable[[str, int, int | None], None] | None:
    if not sys.stderr.isatty():
        return None
    state: dict[str, int] = {}

    def progress(name: str, done: int, total: int | None) -> None:
        percent = int(done * 100 / total) if total else 0
        if state.get(name) == percent:
            return
        state[name] = percent
        size = f"{done / 1e6:.1f} MB" + (f" of {total / 1e6:.1f} MB" if total else "")
        end = "\n" if total and done >= total else "\r"
        click.echo(f"  {name}: {size} ({percent}%)", err=True, nl=False)
        click.echo(end, err=True, nl=False)

    return progress


def _sync_text(result: catalog.SyncResult) -> str:
    return render_fields(
        [
            ("method", result.method),
            ("release", result.stamp_after),
            ("previous", result.stamp_before),
            ("profile", result.profile),
            ("downloaded", f"{result.downloaded_bytes / 1e6:.1f} MB"),
            ("seconds", round(result.seconds, 1)),
            ("catalogue", str(result.path)),
            ("notes", list(result.notes)),
        ]
    )


@catalog_group.command(name="sync")
@click.option(
    "--profile",
    type=click.Choice(["slim", "full"]),
    help="Default: keep the current profile, or slim.",
)
@click.option(
    "--with-aliases/--without-aliases",
    default=None,
    help="Add the aliases table to a slim catalogue (better search).",
)
@click.option("--stamp", help="Sync to this release instead of the current one.")
@click.option(
    "--method",
    type=click.Choice(["auto", "snapshot", "replay"]),
    default="auto",
    show_default=True,
    help="auto replays the changelogs when it can and downloads a snapshot otherwise.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Download a snapshot even when the catalogue is current.",
)
@click.option("--keep-downloads", is_flag=True, help="Keep the downloaded files.")
@output_options
def catalog_sync(
    profile: str | None,
    with_aliases: bool | None,
    stamp: str | None,
    method: str,
    force: bool,
    keep_downloads: bool,
    fmt: str | None,
) -> int:
    """Download the register, or update the local catalogue to the current release.

    The slim profile (bridges, entities, redirects) is about 210 MB to download;
    later syncs replay the changelogs when they can.

    \b
    Examples:
      reep catalog sync
      reep catalog sync --profile full
      reep catalog sync --json
    """
    out = Output("catalog sync", fmt or "table")
    settings = _settings()
    result = catalog.sync(
        profile,
        stamp=stamp,
        with_aliases=with_aliases,
        method=method,
        force=force,
        keep_downloads=keep_downloads,
        settings=settings,
        progress=_progress_printer(),
    )
    out.emit(
        result,
        release_stamp=result.stamp_after,
        backend="local",
        text=_sync_text(result),
    )
    return EXIT_OK


@catalog_group.command(name="import")
@click.argument(
    "directory", type=click.Path(file_okay=False, exists=True, path_type=Path)
)
@click.option(
    "--profile", type=click.Choice(["slim", "full"]), default="slim", show_default=True
)
@click.option(
    "--with-aliases", is_flag=True, help="Add the aliases table to a slim catalogue."
)
@click.option(
    "--stamp", help="The release the files come from; its manifest checks them."
)
@output_options
def catalog_import(
    directory: Path,
    profile: str,
    with_aliases: bool,
    stamp: str | None,
    fmt: str | None,
) -> int:
    """Build the local catalogue from release files you already downloaded.

    The directory holds bridges.csv.gz, entities.csv.gz and redirects.csv.gz
    (flat, or in csv/). A release.json in the directory, or --stamp, lets the
    import check the files.

    \b
    Examples:
      reep catalog import ~/Downloads/reep
      reep catalog import ./release --stamp 20261005T180536Z --json
    """
    out = Output("catalog import", fmt or "table")
    result = catalog.import_release(
        directory,
        profile=profile,
        with_aliases=with_aliases,
        stamp=stamp,
        settings=_settings(),
    )
    out.emit(
        result,
        release_stamp=result.stamp_after,
        backend="local",
        text=_sync_text(result),
    )
    return EXIT_OK


# -- auth ----------------------------------------------------------------------


@cli.group(name="auth", context_settings=_CONTEXT)
def auth_group() -> None:
    """Store, check or remove the API key (only the hosted API needs one)."""


@auth_group.command(name="login")
@click.option(
    "--key-stdin", is_flag=True, help="Read the key from stdin instead of a prompt."
)
@click.option(
    "--no-verify", is_flag=True, help="Store the key without checking it with the API."
)
@click.option(
    "--no-keyring",
    is_flag=True,
    help="Store the key in the config file, not the keyring.",
)
@output_options
@click.pass_context
def auth_login(
    ctx: click.Context,
    key_stdin: bool,
    no_verify: bool,
    no_keyring: bool,
    fmt: str | None,
) -> int:
    """Store an API key in the keyring (or a config file with mode 600).

    \b
    Examples:
      reep auth login
      printf '%s' "$KEY" | reep auth login --key-stdin
    """
    out = Output("auth login", fmt or "table")
    if key_stdin:
        key = sys.stdin.read().strip()
    elif ctx.obj.get("no_input"):
        raise _UsageError(
            "No terminal for a prompt.",
            hint="Pass the key on stdin with --key-stdin, or set REEP_API_KEY.",
        )
    else:
        key = str(click.prompt("Reep API key", hide_input=True)).strip()
    if not key:
        raise InvalidArgumentError("The key is empty.")
    settings = _settings()
    if not no_verify:
        with Register.api(key, settings=settings) as reg:
            api = reg.backend
            api.key_status()  # type: ignore[attr-defined]
    stored = auth.save_key(key, settings, use_keyring=not no_keyring)
    data = {"stored_in": stored, "verified": not no_verify}
    out.emit(data, release_stamp=None, backend="api", text=f"Key stored in {stored}.")
    return EXIT_OK


@auth_group.command(name="status")
@output_options
def auth_status(fmt: str | None) -> int:
    """Show where the key comes from, its plan and its expiry.

    \b
    Examples:
      reep auth status --json
    """
    out = Output("auth status", fmt or "table")
    settings = _settings()
    key = auth.load_key(settings)
    if not key:
        raise AuthError(
            "No Reep API key.",
            hint="Set REEP_API_KEY or run 'reep auth login'. Keys: https://reep.football/api.",
        )
    with Register.api(key, settings=settings) as reg:
        info = reg.backend.key_status().get("key") or {}  # type: ignore[attr-defined]
    data = {
        "key_source": auth.key_source(settings),
        "key": auth.mask(key),
        "plan": info.get("plan"),
        "expires_at": info.get("expires_at"),
        "days_remaining": info.get("days_remaining"),
        "credits_remaining": info.get("credits_remaining"),
    }
    text = render_fields([(k.replace("_", " "), v) for k, v in data.items()])
    out.emit(data, release_stamp=None, backend="api", text=text)
    return EXIT_OK


@auth_group.command(name="logout")
@output_options
def auth_logout(fmt: str | None) -> int:
    """Remove the stored key (REEP_API_KEY is not changed).

    \b
    Examples:
      reep auth logout
    """
    out = Output("auth logout", fmt or "table")
    removed = auth.delete_key(_settings())
    text = (
        f"Removed the key from {', '.join(removed)}." if removed else "No stored key."
    )
    out.emit({"removed_from": removed}, release_stamp=None, backend=None, text=text)
    return EXIT_OK


# -- schema --------------------------------------------------------------------


@cli.command()
@click.argument("command", nargs=-1)
def schema(command: tuple[str, ...]) -> int:
    """Print the JSON Schema of a command's --json output.

    Without a command, print the list of commands and the error schema.

    \b
    Examples:
      reep schema resolve
      reep schema catalog sync
    """
    name = " ".join(command)
    if not name:
        click.echo(
            json.dumps(
                {"commands": sorted(COMMANDS), "error": error_schema()}, indent=2
            )
        )
        return EXIT_OK
    if name not in COMMANDS:
        raise InvalidArgumentError(
            f"No schema for {name!r}.", hint=f"Commands: {', '.join(sorted(COMMANDS))}."
        )
    click.echo(json.dumps(envelope_schema(name), indent=2))
    return EXIT_OK


# -- entry point ---------------------------------------------------------------


def _format_hint(args: Sequence[str]) -> str:
    if "--json" in args:
        return "json"
    if "--jsonl" in args:
        return "jsonl"
    return "table"


def _guard(func: Callable[[], int]) -> int:
    try:
        return func()
    except KeyboardInterrupt:
        click.echo("Interrupted.", err=True)
        return 130


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``reep`` command.

    Args:
        argv: Command-line arguments without the program name. ``None`` reads
            them from ``sys.argv``.

    Returns:
        The process exit code.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    fmt = _format_hint(args)

    def run() -> int:
        try:
            result = cli.main(args=args, prog_name="reep", standalone_mode=False)
        except click.exceptions.Exit as exc:
            return int(exc.exit_code)
        except click.UsageError as exc:
            emit_error(_UsageError(exc.format_message()), fmt)
            return 2
        except click.Abort:
            click.echo("Aborted.", err=True)
            return 130
        except ReepError as exc:
            emit_error(exc, fmt)
            return exc.exit_code
        except OSError as exc:
            emit_error(
                NetworkError(str(exc))
                if _is_network(exc)
                else _InternalError(str(exc)),
                fmt,
            )
            return 6 if _is_network(exc) else 1
        except Exception as exc:
            if os.environ.get("REEP_DEBUG"):
                traceback.print_exc()
            emit_error(
                _InternalError(
                    f"{type(exc).__name__}: {exc}",
                    hint="This is a bug. Report it at "
                    "https://github.com/withqwerty/reep-python/issues (set "
                    "REEP_DEBUG=1 for the traceback).",
                ),
                fmt,
            )
            return 1
        return int(result) if isinstance(result, int) else EXIT_OK

    return _guard(run)


def _is_network(exc: OSError) -> bool:
    return isinstance(exc, (ConnectionError, TimeoutError))
