"""One v4 table boundary for downloaded catalogues and read-only shipped files."""

from __future__ import annotations

from typing import TYPE_CHECKING

from reep.errors import UnsupportedReleaseError

if TYPE_CHECKING:
    import duckdb

# Exact public projection columns from reep-v4/public_release.py at 5a5088d.
COLUMNS: dict[str, list[str]] = {
    "entities": [
        "reep_id",
        "entity_type",
        "canonical_name",
        "gender",
        "country",
        "birth_date",
        "birth_date_iso",
        "dob_precision",
        "grade",
        "bucket_count",
        "corroboration_buckets",
        "metadata_json",
        "updated_at",
    ],
    "bridges": [
        "reep_id",
        "provider",
        "namespace",
        "external_id",
        "entity_type",
        "independence_bucket",
        "route_scope",
        "rule_id",
        "confidence",
        "attached_at",
    ],
    "matches": [
        "reep_id",
        "fixture_label",
        "gender",
        "country",
        "match_date_iso",
        "season_id",
        "home_team_id",
        "away_team_id",
        "home_score",
        "away_score",
        "match_state",
        "result_form",
        "shootout_home",
        "shootout_away",
        "score_basis",
        "leg",
        "aggregate_of",
        "grade",
        "bucket_count",
        "corroboration_buckets",
        "metadata_json",
    ],
    "edges": [
        "edge_id",
        "edge_kind",
        "source_reep_id",
        "source_type",
        "target_reep_id",
        "target_type",
        "provider_count",
        "metadata_json",
    ],
    "redirects": ["from_id", "to_id", "reason", "created_at"],
    "overlay_links": [
        "reep_id",
        "source",
        "external_id",
        "qid",
        "property",
        "confidence",
        "dob_check",
        "entity_type",
        "evidence",
        "created_at",
    ],
    "overlay_aliases": [
        "reep_id",
        "source",
        "qid",
        "name",
        "alias",
        "language",
        "lang",
        "name_source",
        "confidence",
        "link_confidence",
        "dob_check",
        "evidence",
        "created_at",
    ],
    "provider_aliases": ["reep_id", "alias", "alias_kind", "provider"],
    "entity_search": [
        "reep_id",
        "entity_type",
        "label",
        "canonical_name",
        "search_text",
        "bridge_count",
        "alias_count",
    ],
}


def validate_columns(table: str, columns: list[str]) -> None:
    """Reject an unknown table layout before interpreting any v4 column."""
    if table not in COLUMNS or columns != COLUMNS[table]:
        raise UnsupportedReleaseError(f"Unsupported v4 {table} column layout.")


def projection(table: str, relation: str) -> str:
    """Expose equivalent SDK fields, keeping unpublished lifecycle values NULL."""
    additions = {
        "entities": ", canonical_name AS label, NULL::VARCHAR AS status, "
        "grade AS corroboration_grade, "
        "CAST(bucket_count AS VARCHAR) AS corroboration_count, "
        "NULL::VARCHAR AS source_tier_band",
        "bridges": ", NULL::VARCHAR AS rung, NULL::VARCHAR AS upstream_status",
        "matches": ", NULL::VARCHAR AS status, match_date_iso AS kickoff_date, "
        "NULL::VARCHAR AS competition_id",
    }
    # relation is a fixed SELECT/quoted catalogue name supplied by the store owner.
    return f"SELECT *{additions.get(table, '')} FROM {relation}"  # noqa: S608


def check_content(con: duckdb.DuckDBPyConnection) -> None:
    """Check keys and redirect termination without inventing legacy reasons."""
    checks = {
        "empty/duplicate entity IDs": (
            "SELECT count(*) FROM (SELECT reep_id FROM entities GROUP BY "
            "reep_id HAVING reep_id IS NULL OR reep_id='' OR count(*)<>1)"
        ),
        "missing entity type": (
            "SELECT count(*) FROM entities WHERE entity_type IS NULL OR entity_type=''"
        ),
        "empty bridge keys": (
            "SELECT count(*) FROM bridges WHERE provider IS NULL OR provider='' "
            "OR namespace IS NULL OR namespace='' OR external_id IS NULL OR "
            "external_id='' OR reep_id IS NULL OR reep_id=''"
        ),
        "duplicate exact bridge keys": (
            "SELECT count(*) FROM (SELECT provider,namespace,external_id FROM "
            "bridges GROUP BY ALL HAVING count(*)<>1)"
        ),
        "dangling bridges": (
            "SELECT count(*) FROM bridges b ANTI JOIN entities e USING(reep_id)"
        ),
        "empty redirects": (
            "SELECT count(*) FROM redirects WHERE from_id IS NULL OR from_id='' "
            "OR to_id IS NULL OR to_id='' OR reason IS NULL OR reason=''"
        ),
        "duplicate redirect sources": (
            "SELECT count(*) FROM (SELECT from_id FROM redirects GROUP BY "
            "from_id HAVING count(*)<>1)"
        ),
    }
    for label, query in checks.items():
        row = con.execute(query).fetchone()
        if row and row[0]:
            raise UnsupportedReleaseError(f"Invalid v4 catalogue: {label}.")
    # A target chain must end at a published entity within the reader's exact bound.
    row = con.execute("""WITH RECURSIVE chain AS (
        SELECT from_id AS original, to_id AS target, [from_id] AS seen, 1 AS hops
        FROM redirects
        UNION ALL
        SELECT c.original,r.to_id,list_append(c.seen,c.target),c.hops+1
        FROM chain c JOIN redirects r ON r.from_id=c.target
        WHERE c.hops < 8 AND NOT list_contains(c.seen,c.target)
    ), terminal AS (
        SELECT original FROM chain c JOIN entities e ON e.reep_id=c.target
        WHERE NOT EXISTS(SELECT 1 FROM redirects r WHERE r.from_id=c.target)
    ) SELECT count(*) FROM redirects r
      ANTI JOIN terminal t ON r.from_id=t.original""").fetchone()
    if row and row[0]:
        raise UnsupportedReleaseError(
            "Invalid v4 catalogue: dangling, cyclic or overlong redirect chain."
        )


def aliases_query(tables: set[str]) -> str:
    """Combine only names actually provided by the downloaded projection."""
    sources = []
    if "provider_aliases" in tables:
        sources.append(
            "SELECT reep_id,alias FROM provider_aliases WHERE alias IS NOT NULL"
        )
    if "overlay_aliases" in tables:
        sources.append(
            "SELECT reep_id,name AS alias FROM overlay_aliases WHERE name IS NOT NULL"
        )
        sources.append(
            "SELECT reep_id,alias FROM overlay_aliases WHERE alias IS NOT NULL"
        )
    union = (
        " UNION ".join(sources)
        if sources
        else "SELECT NULL::VARCHAR reep_id,NULL::VARCHAR alias WHERE false"
    )
    return f"SELECT DISTINCT reep_id,alias,NULL::VARCHAR rank FROM ({union})"  # noqa: S608
