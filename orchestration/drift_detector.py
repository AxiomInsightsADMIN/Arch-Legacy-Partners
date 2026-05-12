"""Source-drift detector — locked decision 8.7.

For each source registered in the `source` table, compares the latest
`source_signature` row (where the parent `scraper_run.status='success'`)
against the immediately previous successful signature, applies the four
pause conditions, and writes a JSON report to `exports/drift_report.json`.

Pause conditions (any one triggers):
  - HTTP status non-200 (skipped when http_status IS NULL, e.g. manual-drop
    sources whose payload didn't come over HTTP)
  - row count drop > 30% vs prior signature
  - schema_hash mismatch vs prior signature (both must be non-null)
  - response byte size delta > 50% vs prior signature

A source with only one successful signature passes by default (no prior to
compare against). A source with zero signatures passes by default too —
the workflow's scraper step would have already failed before this runs if
something was wrong upstream.

Exit code:
  0  — all sources pass
  1  — at least one source paused; resolver MUST NOT run until the operator
        clears the cause and re-triggers
  2  — usage / config error (DB unreachable, etc.)

Usage:
  python -m orchestration.drift_detector                     # default output
  python -m orchestration.drift_detector --output-path /tmp/d.json
  python -m orchestration.drift_detector --quiet            # no stdout report
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Project-root path shim so this runs via `python -m orchestration.drift_detector`
# and via `python orchestration/drift_detector.py`.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scrapers._loader_utils import db_connect  # noqa: E402

DEFAULT_OUTPUT_PATH = _PROJECT_ROOT / "exports" / "drift_report.json"

# Pause thresholds — locked decision 8.7 verbatim.
ROW_COUNT_DROP_THRESHOLD: float = 0.30  # > 30%
BYTE_SIZE_DELTA_THRESHOLD: float = 0.50  # > 50%


def _latest_two_signatures(cur, source_slug: str) -> list[dict]:
    """Return up to 2 most-recent successful signatures for one source.
    Index 0 is the latest; index 1 is the immediately prior one. Returns
    an empty list if no successful signatures exist."""
    cur.execute(
        """
        SELECT ss.id, ss.http_status, ss.response_byte_size,
               ss.schema_hash, ss.row_count, ss.captured_at,
               ss.scraper_run_id
          FROM source_signature ss
          JOIN scraper_run sr ON sr.id = ss.scraper_run_id
          JOIN source s ON s.id = ss.source_id
         WHERE s.slug = %s
           AND sr.status = 'success'
         ORDER BY ss.captured_at DESC
         LIMIT 2
        """,
        (source_slug,),
    )
    out = []
    for row in cur.fetchall():
        out.append(
            {
                "signature_id": row[0],
                "http_status": row[1],
                "response_byte_size": row[2],
                "schema_hash": row[3],
                "row_count": row[4],
                "captured_at": row[5].isoformat() if row[5] else None,
                "scraper_run_id": row[6],
            }
        )
    return out


def _evaluate(latest: dict, prior: dict | None) -> tuple[str, str | None, dict]:
    """Apply the four pause checks. Returns (status, reason, details).

    status   ∈ {'pass', 'pause'}
    reason    is None for pass; a short slug for pause
    details   is a dict with the trigger numerics so the operator can audit
              without re-querying.
    """
    details: dict[str, Any] = {
        "latest_signature_id": latest["signature_id"],
        "latest_row_count": latest["row_count"],
        "latest_byte_size": latest["response_byte_size"],
        "latest_http_status": latest["http_status"],
        "latest_schema_hash_prefix": (latest["schema_hash"] or "")[:12] or None,
    }

    # Check 1: HTTP non-200. Skip when http_status is NULL (manual-drop path).
    http = latest["http_status"]
    if http is not None and http != 200:
        return (
            "pause",
            f"http_status_{http}",
            details | {"trigger": "http_status non-200"},
        )

    # If no prior signature exists, pass by default (first ever run).
    if prior is None:
        return ("pass", None, details | {"comparison": "first_run_no_prior"})

    details |= {
        "prior_signature_id": prior["signature_id"],
        "prior_row_count": prior["row_count"],
        "prior_byte_size": prior["response_byte_size"],
        "prior_schema_hash_prefix": (prior["schema_hash"] or "")[:12] or None,
    }

    # Check 2: row count drop > 30%.
    prior_rows = prior["row_count"]
    latest_rows = latest["row_count"]
    if prior_rows and prior_rows > 0 and latest_rows is not None:
        drop = (prior_rows - latest_rows) / prior_rows
        details["row_count_drop_fraction"] = round(drop, 4)
        if drop > ROW_COUNT_DROP_THRESHOLD:
            return (
                "pause",
                f"row_count_drop_{int(drop * 100)}_pct",
                details | {"trigger": "row_count drop > 30%"},
            )

    # Check 3: schema_hash mismatch (when both sides non-null).
    if (
        latest["schema_hash"]
        and prior["schema_hash"]
        and latest["schema_hash"] != prior["schema_hash"]
    ):
        return (
            "pause",
            "schema_hash_mismatch",
            details | {"trigger": "schema_hash mismatch"},
        )

    # Check 4: byte size delta > 50% (absolute, either direction).
    prior_bytes = prior["response_byte_size"]
    latest_bytes = latest["response_byte_size"]
    if prior_bytes and prior_bytes > 0 and latest_bytes is not None:
        delta = abs(latest_bytes - prior_bytes) / prior_bytes
        details["byte_size_delta_fraction"] = round(delta, 4)
        if delta > BYTE_SIZE_DELTA_THRESHOLD:
            return (
                "pause",
                f"byte_size_delta_{int(delta * 100)}_pct",
                details | {"trigger": "byte_size delta > 50%"},
            )

    return ("pass", None, details)


def run(*, output_path: Path | None = None) -> dict:
    """Walk every source row in the `source` table that has at least one
    scraper_run, evaluate it, and write the JSON report. Returns the
    report dict for in-process callers."""
    output_path = output_path or DEFAULT_OUTPUT_PATH

    conn = db_connect()
    cur = conn.cursor()

    # Sources with any scraper_run — order alphabetically for stable output.
    cur.execute(
        """
        SELECT DISTINCT s.slug
          FROM source s
          JOIN scraper_run sr ON sr.source_id = s.id
         ORDER BY s.slug
        """
    )
    source_slugs = [row[0] for row in cur.fetchall()]

    per_source: dict[str, dict] = {}
    pause_count = 0
    for slug in source_slugs:
        sigs = _latest_two_signatures(cur, slug)
        if not sigs:
            per_source[slug] = {
                "status": "pass",
                "reason": None,
                "details": {"comparison": "no_signatures_yet"},
            }
            continue
        latest = sigs[0]
        prior = sigs[1] if len(sigs) > 1 else None
        status, reason, details = _evaluate(latest, prior)
        per_source[slug] = {
            "status": status,
            "reason": reason,
            "details": details,
        }
        if status == "pause":
            pause_count += 1

    cur.close()
    conn.close()

    overall = "pause" if pause_count > 0 else "pass"
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "overall_status": overall,
        "pause_count": pause_count,
        "source_count": len(source_slugs),
        "sources": per_source,
        "thresholds": {
            "row_count_drop": ROW_COUNT_DROP_THRESHOLD,
            "byte_size_delta": BYTE_SIZE_DELTA_THRESHOLD,
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    return report


def _print_report(report: dict) -> None:
    print(f"=== Drift report ({report['generated_at']}) ===", flush=True)
    print(
        f"  overall:       {report['overall_status']}",
        flush=True,
    )
    print(
        f"  sources seen:  {report['source_count']}",
        flush=True,
    )
    print(
        f"  paused:        {report['pause_count']}",
        flush=True,
    )
    print(flush=True)
    for slug, s in report["sources"].items():
        line = f"  {slug:35s}  {s['status']:6s}"
        if s["reason"]:
            line += f"  reason={s['reason']}"
        print(line, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--output-path",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="JSON output path (default: exports/drift_report.json).",
    )
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="Skip the human-readable summary on stdout.",
    )
    args = ap.parse_args()

    report = run(output_path=args.output_path)
    if not args.quiet:
        _print_report(report)

    # Exit code: non-zero on any pause so the calling workflow halts before
    # the resolver runs against drift-paused source data.
    return 1 if report["overall_status"] == "pause" else 0


if __name__ == "__main__":
    sys.exit(main())
