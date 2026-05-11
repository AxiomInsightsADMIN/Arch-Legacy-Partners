"""
Shared loader utilities — DB connection, scraper_run lifecycle, source_signature
writes. Used by every per-source loader in scrapers/*.

This module exists because the ECHO and CWNS loaders (and future TCEQ/NC-DEQ
loaders) all need identical Supabase-side bookkeeping. Centralizing here so a
schema change to scraper_run or source_signature is a one-file edit.

No source-specific HTTP, parsing, or upsert logic lives here. Those belong in
the per-source modules. This file is plumbing only.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


# --------------------------------------------------------------------------
# DB connection
# --------------------------------------------------------------------------
def db_connect() -> psycopg2.extensions.connection:
    """Open a session-mode pooler connection to the linked Supabase project.

    Reads SUPABASE_DB_* env vars from .env. The .env's pooler values are
    canonical (see memory: project_supabase_pooler — Tokyo aws-1 fleet).
    """
    return psycopg2.connect(
        host=os.environ["SUPABASE_DB_HOST"],
        port=int(os.environ["SUPABASE_DB_PORT"]),
        user=os.environ["SUPABASE_DB_USER"],
        password=os.environ["SUPABASE_DB_PASSWORD"],
        dbname=os.environ["SUPABASE_DB_NAME"],
        sslmode="require",
        connect_timeout=15,
    )


# --------------------------------------------------------------------------
# source lookup
# --------------------------------------------------------------------------
def get_source_id(cur, slug: str) -> int:
    """Return source.id for the given slug. Raises if the seed migration
    hasn't been applied or the slug is unknown."""
    cur.execute("SELECT id FROM source WHERE slug = %s", (slug,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"source slug {slug!r} not found in DB — seed migration not applied?")
    return row[0]


# --------------------------------------------------------------------------
# scraper_run lifecycle
# --------------------------------------------------------------------------
def begin_run(cur, source_id: int) -> int:
    """Insert a fresh scraper_run row with status='running'. Returns the new id."""
    cur.execute(
        "INSERT INTO scraper_run (source_id, status) VALUES (%s, 'running') RETURNING id",
        (source_id,),
    )
    return cur.fetchone()[0]


def finish_run(
    cur,
    run_id: int,
    status: str,
    *,
    rows_in: int,
    rows_inserted: int,
    rows_updated: int,
    error_message: str | None = None,
) -> None:
    """Close out a scraper_run with counts + status + finished_at = NOW()."""
    cur.execute(
        """
        UPDATE scraper_run
           SET status         = %s,
               rows_in        = %s,
               rows_inserted  = %s,
               rows_updated   = %s,
               error_message  = %s,
               finished_at    = NOW()
         WHERE id = %s
        """,
        (status, rows_in, rows_inserted, rows_updated, error_message, run_id),
    )


# --------------------------------------------------------------------------
# source_signature
# --------------------------------------------------------------------------
def write_signature(
    cur,
    source_id: int,
    run_id: int,
    *,
    http_status: int | None,
    byte_size: int,
    schema_hash: str | None,
    row_count: int,
    selectors_hit_count: int | None = None,
) -> None:
    """Record drift-detection baseline for this run."""
    cur.execute(
        """
        INSERT INTO source_signature
            (source_id, scraper_run_id, http_status, response_byte_size,
             schema_hash, row_count, selectors_hit_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            source_id,
            run_id,
            http_status,
            byte_size,
            schema_hash,
            row_count,
            selectors_hit_count,
        ),
    )


# --------------------------------------------------------------------------
# Hashing helpers
# --------------------------------------------------------------------------
def hash_payload(payload_str: str) -> str:
    """sha256 hex of a canonicalized JSON string. Used for payload_hash and
    schema_hash."""
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
