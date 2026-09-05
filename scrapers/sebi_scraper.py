"""
Scrape SEBI Circulars and append new items to data/sebi_circulars.csv.
"""

from __future__ import annotations

import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

LISTING_URL = (
    "https://www.sebi.gov.in/sebiweb/home/HomeAction.do"
    "?doListing=yes&sid=1&ssid=7&smid=0"
)
AJAX_URL = "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp"
SITE_ORIGIN = "https://www.sebi.gov.in"
DATE_FORMAT = "%b %d, %Y"  # e.g. Aug 03, 2026
CSV_COLUMNS = ["title", "date", "pdf_link", "source"]
SOURCE = "SEBI"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CSV_PATH = DATA_DIR / "sebi_circulars.csv"
LAST_SCRAPE_PATH = DATA_DIR / "sebi_last_scrape.txt"

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
    for fmt in ("%Y-%m-%d", DATE_FORMAT, "%d-%m-%Y", "%d/%m/%Y"):
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


def extract_pdf_link(session: requests.Session, detail_url: str) -> str:
    """Fetch a circular detail page and pull the PDF URL from the iframe."""
    response = session.get(detail_url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    iframe = soup.find("iframe", src=True)
    if iframe:
        src = iframe["src"].strip()
        parsed = urlparse(urljoin(detail_url, src))
        file_vals = parse_qs(parsed.query).get("file")
        if file_vals and file_vals[0]:
            return file_vals[0].strip()
        if ".pdf" in src.lower():
            return urljoin(detail_url, src)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if ".pdf" in href.lower():
            return urljoin(detail_url, href)

    return ""


def parse_listing_html(
    html: str, cutoff: date, session: requests.Session
) -> tuple[list[dict[str, str]], bool]:
    """
    Parse one SEBI listing page.

    Returns (circulars newer than cutoff, hit_old_cutoff).
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="sample_1") or soup.find("table")
    if table is None:
        return [], False

    circulars: list[dict[str, str]] = []
    hit_old = False
    rows = table.find_all("tr")

    for row in rows:
        try:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            date_text = cells[0].get_text(" ", strip=True)
            circ_date = parse_circular_date(date_text)
            if circ_date is None:
                continue

            if circ_date < cutoff:
                hit_old = True
                break

            if circ_date <= cutoff:
                continue

            link = cells[1].find("a", href=True)
            if link is None:
                continue

            title = link.get_text(" ", strip=True)
            if not title:
                continue

            detail_url = urljoin(SITE_ORIGIN, link["href"].strip())
            try:
                pdf_link = extract_pdf_link(session, detail_url)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"Could not fetch PDF for {title!r}: {exc}",
                    file=sys.stderr,
                )
                pdf_link = ""

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


def fetch_listing_page(
    session: requests.Session, page_index: int
) -> str:
    """
    Fetch listing HTML for a zero-based page index via SEBI's AJAX endpoint.
    """
    data = {
        "nextValue": "1",
        "next": "n",
        "search": "",
        "fromDate": "",
        "toDate": "",
        "fromYear": "",
        "toYear": "",
        "deptId": "",
        "sid": "1",
        "ssid": "7",
        "smid": "0",
        "ssidhidden": "7",
        "intmid": "-1",
        "sText": "Legal",
        "ssText": "Circulars",
        "smText": "",
        "doDirect": str(page_index),
    }
    response = session.post(AJAX_URL, data=data, timeout=60)
    response.raise_for_status()
    return response.text


def scrape_new_circulars(cutoff: date) -> list[dict[str, str]]:
    session = requests.Session()
    session.headers.update(
        {
            **HEADERS,
            "Referer": LISTING_URL,
            "X-Requested-With": "XMLHttpRequest",
        }
    )

    # Establish session cookies from the main listing page.
    session.get(LISTING_URL, timeout=60)

    collected: list[dict[str, str]] = []
    max_pages = 50

    for page_index in range(max_pages):
        print(f"Fetching page {page_index + 1}...")
        html = fetch_listing_page(session, page_index)
        page_circulars, hit_old = parse_listing_html(html, cutoff, session)
        collected.extend(page_circulars)

        if hit_old:
            break

        # Empty page means we've run out of results.
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", id="sample_1") or soup.find("table")
        data_rows = [
            r
            for r in (table.find_all("tr") if table else [])
            if len(r.find_all("td")) >= 2
        ]
        if not data_rows:
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
