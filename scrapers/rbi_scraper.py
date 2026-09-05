"""
Scrape RBI Notifications and append new circulars to data/rbi_circulars.csv.
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

BASE_URL = "https://www.rbi.org.in/Scripts/NotificationUser.aspx"
DATE_FORMAT = "%b %d, %Y"  # e.g. Jul 31, 2026
CSV_COLUMNS = ["title", "date", "pdf_link", "source"]
SOURCE = "RBI"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CSV_PATH = DATA_DIR / "rbi_circulars.csv"
LAST_SCRAPE_PATH = DATA_DIR / "rbi_last_scrape.txt"

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


def build_form_data(soup: BeautifulSoup, year: int, month: int) -> dict[str, str]:
    data: dict[str, str] = {}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        typ = (inp.get("type") or "").lower()
        if typ in ("button", "image"):
            continue
        # Keep the hidden archive submit button; skip other submits.
        if typ == "submit" and name != "UsrFontCntr$btn":
            continue
        data[name] = inp.get("value") or ""

    data["hdnYear"] = str(year)
    data["hdnMonth"] = str(month)
    data["UsrFontCntr$btn"] = ""
    return data


def fetch_month(
    session: requests.Session, soup: BeautifulSoup, year: int, month: int
) -> BeautifulSoup:
    data = build_form_data(soup, year, month)
    response = session.post(BASE_URL, data=data, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def parse_page(
    soup: BeautifulSoup, cutoff: date
) -> tuple[list[dict[str, str]], bool, Optional[date]]:
    """
    Parse circulars from one notifications page.

    Returns (circulars, hit_old_cutoff, oldest_date_seen).
    hit_old_cutoff is True when a date older than cutoff was found (caller should stop).
    """
    table = soup.find("table", class_="tablebg")
    if table is None:
        return [], False, None

    circulars: list[dict[str, str]] = []
    current_date: Optional[date] = None
    current_date_str: Optional[str] = None
    oldest_seen: Optional[date] = None
    hit_old = False

    for row in table.find_all("tr"):
        try:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            # Date header row (single cell with e.g. "Jul 31, 2026")
            if len(cells) == 1:
                date_text = cells[0].get_text(" ", strip=True)
                parsed = parse_circular_date(date_text)
                if parsed is None:
                    continue
                current_date = parsed
                current_date_str = date_text
                oldest_seen = (
                    parsed if oldest_seen is None else min(oldest_seen, parsed)
                )
                if current_date < cutoff:
                    hit_old = True
                    break
                continue

            if current_date is None or current_date_str is None:
                continue

            # Strictly newer than cutoff
            if current_date <= cutoff:
                continue

            links = cells[0].find_all("a", href=True)
            if not links:
                continue

            title_link = links[0]
            title = title_link.get_text(" ", strip=True)
            if not title:
                continue

            pdf_link = ""
            for cell in cells:
                for a in cell.find_all("a", href=True):
                    href = a["href"].strip()
                    if ".pdf" in href.lower():
                        pdf_link = urljoin(BASE_URL, href)
                        break
                if pdf_link:
                    break

            circulars.append(
                {
                    "title": title,
                    "date": current_date_str,
                    "pdf_link": pdf_link,
                    "source": SOURCE,
                }
            )
        except Exception as exc:  # noqa: BLE001 — skip bad rows, keep going
            print(f"Skipping row due to parse error: {exc}", file=sys.stderr)
            continue

    return circulars, hit_old, oldest_seen


def previous_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def scrape_new_circulars(cutoff: date) -> list[dict[str, str]]:
    session = requests.Session()
    session.headers.update(HEADERS)

    response = session.get(BASE_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    collected: list[dict[str, str]] = []
    today = date.today()
    year, month = today.year, today.month

    # Walk archive months newest → oldest (ASP.NET year/month postback).
    soup = fetch_month(session, soup, year, month)

    for _ in range(24):
        print(f"Fetching {year}-{month:02d}...")
        page_circulars, hit_old, _oldest_seen = parse_page(soup, cutoff)
        collected.extend(page_circulars)

        if hit_old or (year, month) <= (cutoff.year, cutoff.month):
            break

        year, month = previous_month(year, month)
        soup = fetch_month(session, soup, year, month)

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
    # utf-8-sig only for a new file so Excel sees the BOM; append with plain utf-8
    # to avoid writing another BOM in the middle of the file.
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
