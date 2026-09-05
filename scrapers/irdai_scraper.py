"""
Scrape IRDAI Circulars and append new items to data/irdai_circulars.csv.
"""

from __future__ import annotations

import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

LISTING_URL = "https://irdai.gov.in/circulars"
SITE_ORIGIN = "https://irdai.gov.in"
PORTLET = "_com_irdai_document_media_IRDAIDocumentMediaPortlet"
DATE_FORMAT = "%d-%m-%Y"  # e.g. 04-08-2026
CSV_COLUMNS = ["title", "date", "pdf_link", "source"]
SOURCE = "IRDAI"
PAGE_SIZE = 20

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CSV_PATH = DATA_DIR / "irdai_circulars.csv"
LAST_SCRAPE_PATH = DATA_DIR / "irdai_last_scrape.txt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def get_cutoff_date() -> date:
    """Return cutoff date from last scrape file, or 5 days before today."""
    if not LAST_SCRAPE_PATH.exists():
        cutoff = date.today() - timedelta(days=5)
        print(f"First run - cutoff set to {cutoff.isoformat()} (5 days before today)")
        return cutoff

    raw = LAST_SCRAPE_PATH.read_text(encoding="utf-8").strip()
    for fmt in ("%Y-%m-%d", DATE_FORMAT, "%b %d, %Y", "%d/%m/%Y"):
        try:
            cutoff = datetime.strptime(raw, fmt).date()
            print(f"Cutoff from last scrape: {cutoff.isoformat()}")
            return cutoff
        except ValueError:
            continue

    raise ValueError(f"Could not parse date in {LAST_SCRAPE_PATH}: {raw!r}")


def parse_circular_date(text: str) -> Optional[date]:
    text = text.strip()
    try:
        return datetime.strptime(text, DATE_FORMAT).date()
    except ValueError:
        return None


def load_existing_keys() -> set[tuple[str, str]]:
    """Load (title, date) pairs already present in the CSV."""
    if not CSV_PATH.exists():
        return set()

    keys: set[tuple[str, str]] = set()
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = (row.get("title") or "").strip()
            circ_date = (row.get("date") or "").strip()
            if title and circ_date:
                keys.add((title, circ_date))
    return keys


def page_url(page_number: int) -> str:
    """Build a Liferay portlet listing URL for a 1-based page number."""
    if page_number <= 1:
        return LISTING_URL
    return (
        f"{LISTING_URL}"
        f"?p_p_id=com_irdai_document_media_IRDAIDocumentMediaPortlet"
        f"&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view"
        f"&{PORTLET}_delta={PAGE_SIZE}"
        f"&{PORTLET}_resetCur=false"
        f"&{PORTLET}_cur={page_number}"
    )


def parse_page(soup: BeautifulSoup, cutoff: date) -> tuple[list[dict[str, str]], bool]:
    """
    Parse one IRDAI circulars listing page.

    Returns (circulars newer than cutoff, hit_old_cutoff).
    """
    table = soup.find("table", class_="table")
    if table is None:
        return [], False

    circulars: list[dict[str, str]] = []
    hit_old = False

    for row in table.find_all("tr"):
        try:
            cells = row.find_all("td")
            if len(cells) < 6:
                continue

            date_text = cells[4].get_text(" ", strip=True)
            circ_date = parse_circular_date(date_text)
            if circ_date is None:
                continue

            if circ_date < cutoff:
                hit_old = True
                break

            if circ_date <= cutoff:
                continue

            title = cells[2].get_text(" ", strip=True)
            if not title:
                continue

            pdf_link = ""
            for a in cells[5].find_all("a", href=True):
                href = a["href"].strip()
                if ".pdf" in href.lower():
                    pdf_link = urljoin(SITE_ORIGIN, href)
                    break

            circulars.append(
                {
                    "title": title,
                    "date": date_text,
                    "pdf_link": pdf_link,
                    "source": SOURCE,
                }
            )
        except Exception as exc:  # noqa: BLE001 — skip bad rows, keep going
            print(f"Skipping row due to parse error: {exc}", file=sys.stderr)
            continue

    return circulars, hit_old


def scrape_new_circulars(cutoff: date) -> list[dict[str, str]]:
    session = requests.Session()
    session.headers.update(HEADERS)

    collected: list[dict[str, str]] = []
    max_pages = 50

    for page_number in range(1, max_pages + 1):
        url = page_url(page_number)
        print(f"Fetching page {page_number}...")
        response = session.get(url, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        page_circulars, hit_old = parse_page(soup, cutoff)
        collected.extend(page_circulars)

        if hit_old:
            break

        table = soup.find("table", class_="table")
        data_rows = [
            r
            for r in (table.find_all("tr") if table else [])
            if len(r.find_all("td")) >= 6
        ]
        if not data_rows:
            break

        # Stop if there is no Next link (last page).
        next_link = soup.find("a", string=lambda t: isinstance(t, str) and "Next" in t)
        if next_link is None or next_link.get("href", "").startswith("javascript"):
            break

    return collected


def append_circulars(
    circulars: list[dict[str, str]], existing: set[tuple[str, str]]
) -> list[dict[str, str]]:
    """Append non-duplicate circulars to CSV; return the ones actually added."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new_rows = [
        row for row in circulars if (row["title"], row["date"]) not in existing
    ]

    if not new_rows:
        return []

    write_header = not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0
    encoding = "utf-8-sig" if write_header else "utf-8"
    with CSV_PATH.open("a", newline="", encoding=encoding) as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerows(new_rows)

    return new_rows


def write_last_scrape_date() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LAST_SCRAPE_PATH.write_text(date.today().isoformat() + "\n", encoding="utf-8")


def main() -> int:
    try:
        cutoff = get_cutoff_date()
        existing = load_existing_keys()
        circulars = scrape_new_circulars(cutoff)
        added = append_circulars(circulars, existing)
        write_last_scrape_date()

        if added:
            dates = [
                d
                for d in (parse_circular_date(row["date"]) for row in added)
                if d is not None
            ]
            date_range = (
                f"{min(dates).isoformat()} to {max(dates).isoformat()}"
                if dates
                else "n/a"
            )
        else:
            date_range = "n/a (no new circulars)"

        print(f"Added {len(added)} new circular(s)")
        print(f"Date range covered: {date_range}")
        print(f"Updated {LAST_SCRAPE_PATH.name} to {date.today().isoformat()}")
        return 0
    except requests.RequestException as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Scraper failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
