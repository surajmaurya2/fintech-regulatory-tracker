"""
Create the SQLite database and load circular CSVs from data/.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "regulatory_tracker.db"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS circulars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    date TEXT NOT NULL,
    pdf_link TEXT,
    source TEXT,
    summary TEXT,
    category TEXT,
    severity TEXT,
    change_type TEXT,
    fintech_matches TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE (title, date)
)
"""

INSERT_SQL = """
INSERT OR IGNORE INTO circulars (
    title, date, pdf_link, source,
    summary, category, severity, change_type, fintech_matches,
    created_at, updated_at
) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, datetime('now'), datetime('now'))
"""

# Formats used across RBI / SEBI / IRDAI CSVs (and ISO).
DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d.%m.%Y",
    "%d-%m-%y",
    "%d/%m/%y",
    "%d.%m.%y",
    "%b %d, %Y",  # Jul 31, 2026 / Aug 03, 2026
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d %Y",
    "%B %d %Y",
)


def normalize_date(value: str) -> str:
    """Convert a CSV date to YYYY-MM-DD when possible; otherwise keep original."""
    raw = (value or "").strip()
    if not raw:
        return raw

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue

    return raw


def infer_source(csv_path: Path, row: dict[str, str]) -> str:
    """Use CSV source column when present; otherwise derive from filename."""
    source = (row.get("source") or "").strip()
    if source:
        return source
    stem = csv_path.stem  # e.g. rbi_circulars
    if stem.endswith("_circulars"):
        return stem[: -len("_circulars")].upper()
    return stem.upper()


def load_csv_rows(csv_path: Path) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = (row.get("title") or "").strip()
            circ_date = normalize_date(row.get("date") or "")
            if not title or not circ_date:
                continue
            pdf_link = (row.get("pdf_link") or "").strip()
            source = infer_source(csv_path, row)
            rows.append((title, circ_date, pdf_link, source))
    return rows


def table_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(circulars)")}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create table if needed and add any missing columns on older databases."""
    conn.execute(CREATE_TABLE_SQL)

    cols = table_columns(conn)
    if "created_at" not in cols:
        conn.execute("ALTER TABLE circulars ADD COLUMN created_at TEXT")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE circulars ADD COLUMN updated_at TEXT")

    # Backfill timestamps on older rows so summarize.py-era data stays complete.
    conn.execute(
        """
        UPDATE circulars
        SET created_at = datetime('now')
        WHERE created_at IS NULL OR TRIM(created_at) = ''
        """
    )
    conn.execute(
        """
        UPDATE circulars
        SET updated_at = datetime('now')
        WHERE updated_at IS NULL OR TRIM(updated_at) = ''
        """
    )


def normalize_existing_dates(conn: sqlite3.Connection) -> None:
    """
    Rewrite existing date values to YYYY-MM-DD so UNIQUE(title, date)
    stays compatible with newly inserted normalized rows.
    """
    rows = conn.execute("SELECT id, date FROM circulars").fetchall()
    for row_id, current in rows:
        normalized = normalize_date(current or "")
        if normalized and normalized != current:
            try:
                conn.execute(
                    "UPDATE circulars SET date = ? WHERE id = ?",
                    (normalized, row_id),
                )
            except sqlite3.IntegrityError:
                # Another row already has (title, normalized_date); leave as-is.
                pass


def ensure_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_circulars_summary ON circulars(summary)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_circulars_source ON circulars(source)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_circulars_date ON circulars(date)"
    )


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_schema(conn)
        normalize_existing_dates(conn)
        ensure_indexes(conn)
        conn.commit()

        csv_files = sorted(DATA_DIR.glob("*_circulars.csv"))
        if not csv_files:
            print(f"No *_circulars.csv files found in {DATA_DIR}")
        else:
            print(f"Found {len(csv_files)} CSV file(s):")
            for path in csv_files:
                print(f"  - {path.name}")

        inserted = 0
        for csv_path in csv_files:
            rows = load_csv_rows(csv_path)
            before = conn.total_changes
            conn.executemany(INSERT_SQL, rows)
            conn.commit()
            inserted += conn.total_changes - before

        total = conn.execute("SELECT COUNT(*) FROM circulars").fetchone()[0]
        print(f"Inserted {inserted} new row(s) this run")
        print(f"Total rows in circulars: {total}")
        print(f"Database: {DB_PATH}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
