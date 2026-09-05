"""
Run the full Regulatory Intelligence Tracker refresh pipeline.

Order: scrapers → setup_db → summarize → classify → match_fintechs
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "regulatory_tracker.db"
PYTHON = sys.executable


def run_step(label: str, script: Path) -> int:
    print()
    print(f"=== {label} ===")
    if not script.exists():
        print(f"ERROR: script not found: {script}", file=sys.stderr)
        return 1

    result = subprocess.run(
        [PYTHON, str(script)],
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        print(
            f"FAILED: {script.name} exited with code {result.returncode}",
            file=sys.stderr,
        )
    else:
        print(f"OK: {script.name}")
    return result.returncode


def print_db_summary() -> None:
    print()
    print("=== Pipeline summary ===")
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        total = conn.execute("SELECT COUNT(*) FROM circulars").fetchone()[0]
        missing_summary = conn.execute(
            """
            SELECT COUNT(*) FROM circulars
            WHERE summary IS NULL OR TRIM(summary) = ''
            """
        ).fetchone()[0]
        missing_category = conn.execute(
            """
            SELECT COUNT(*) FROM circulars
            WHERE category IS NULL OR TRIM(category) = ''
            """
        ).fetchone()[0]
        missing_matches = conn.execute(
            """
            SELECT COUNT(*) FROM circulars
            WHERE fintech_matches IS NULL OR TRIM(fintech_matches) = ''
            """
        ).fetchone()[0]
    finally:
        conn.close()

    print(f"Total circulars in DB:           {total}")
    print(f"Missing summary:                 {missing_summary}")
    print(f"Missing category:                {missing_category}")
    print(f"Missing fintech_matches:         {missing_matches}")
    print(f"Database: {DB_PATH}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the full regulatory tracker refresh pipeline."
    )
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Skip RBI/SEBI/IRDAI scrapers; only reprocess existing DB/CSV data.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help=(
            "Reserved for future LIMIT_ROWS passthrough. "
            "Current summarize/classify/match scripts use hardcoded LIMIT_ROWS; "
            "this flag is accepted but not applied yet."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("Regulatory Intelligence Tracker — full pipeline")
    print(f"Project root: {PROJECT_ROOT}")
    print(f"Python:       {PYTHON}")
    if args.skip_scrape:
        print("Mode:         --skip-scrape (scrapers disabled)")
    if args.limit is not None:
        print(
            f"Note:         --limit {args.limit} accepted but not applied "
            "(scripts use their own LIMIT_ROWS constants)"
        )

    step_num = 0
    total_steps = (0 if args.skip_scrape else 3) + 4

    if not args.skip_scrape:
        for label, script in [
            (f"1/{total_steps} Scraping RBI", PROJECT_ROOT / "scrapers" / "rbi_scraper.py"),
            (f"2/{total_steps} Scraping SEBI", PROJECT_ROOT / "scrapers" / "sebi_scraper.py"),
            (f"3/{total_steps} Scraping IRDAI", PROJECT_ROOT / "scrapers" / "irdai_scraper.py"),
        ]:
            code = run_step(label, script)
            if code != 0:
                print_db_summary()
                return code
        step_num = 3

    remaining = [
        (
            f"{step_num + 1}/{total_steps} Loading CSVs into SQLite (setup_db)",
            PROJECT_ROOT / "pipeline" / "setup_db.py",
        ),
        (
            f"{step_num + 2}/{total_steps} Summarizing circulars",
            PROJECT_ROOT / "pipeline" / "summarize.py",
        ),
        (
            f"{step_num + 3}/{total_steps} Classifying circulars",
            PROJECT_ROOT / "pipeline" / "classify.py",
        ),
        (
            f"{step_num + 4}/{total_steps} Matching fintechs",
            PROJECT_ROOT / "scripts" / "match_fintechs.py",
        ),
    ]

    for label, script in remaining:
        code = run_step(label, script)
        # summarize/classify may return 1 if some rows failed but others succeeded.
        # Treat non-zero as hard failure and stop, per requirements.
        if code != 0:
            print_db_summary()
            return code

    print()
    print("Pipeline finished successfully.")
    print_db_summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
