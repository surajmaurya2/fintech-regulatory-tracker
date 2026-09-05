"""
Fill empty circular summaries via OpenRouter (google/gemini-2.5-flash).

Downloads each circular PDF, extracts text, and sends cleaned content
to the model. Falls back to title-only (and optional OCR) when needed.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from langdetect import LangDetectException, detect
from openai import APIStatusError, OpenAI, RateLimitError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "regulatory_tracker.db"
ENV_PATH = PROJECT_ROOT / ".env"
ERRORS_LOG = Path(__file__).resolve().parent / "errors.log"

LIMIT_ROWS = 50
MODEL = "google/gemini-2.5-flash"
BASE_URL = "https://openrouter.ai/api/v1"
DELAY_SECONDS = 1
MAX_RETRIES_429 = 6
BACKOFF_BASE_SECONDS = 1  # 1s, 2s, 4s, 8s, 16s, 32s

# Soft caps so we never send an oversized prompt.
SOFT_TRUNCATE_THRESHOLD = 40_000
HEAD_CHARS = 35_000
TAIL_CHARS = 5_000
HARD_MAX_CHARS = 45_000
MIN_USEFUL_CHARS = 80  # Below this, treat extraction as failed / scanned

DOWNLOAD_TIMEOUT = 60
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

PROMPT_TEMPLATE = """You are a regulatory compliance expert specializing in Indian banking and fintech regulations (RBI, SEBI, IRDAI).

Based ONLY on the text provided below, write a detailed but concise factual summary.

Structure the summary as 5–8 sentences that cover ALL of the following (skip a point only if the text truly has no information on it):

1. What the circular / directions are about (main subject).
2. Exact applicability: which entities it applies to, and any explicit exclusions (e.g. excludes Payments Banks, SFBs, Local Area Banks).
3. The most important new obligations, controls, or requirements.
4. Any specific deadlines, timelines, thresholds, or quantitative requirements.
5. Reporting, board approval, or governance obligations (if mentioned).
6. Whether it repeals, consolidates, or supersedes earlier instructions.
7. Effective date or whether it applies immediately.

Strict rules:
- Do NOT use words like "likely", "probably", "appears to", "may", "seems", or "suggests".
- Do NOT invent clause numbers, dates, requirements, or details that are not present in the text.
- Prefer precise entity names and exclusions over vague phrases like "all regulated entities" when the text is more specific.
- Write in plain, professional English.
- Be direct and factual.

Text:
{text}

Summary:"""


def get_client() -> OpenAI:
    load_dotenv(ENV_PATH)
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key or api_key.strip() in ("", "your_api_key_here"):
        raise RuntimeError(
            "OPENROUTER_API_KEY is missing or still a placeholder. "
            f"Set it in {ENV_PATH}"
        )
    return OpenAI(api_key=api_key.strip(), base_url=BASE_URL)


def log_message(title: str, message: str) -> None:
    """Append a structured note/failure for this circular to errors.log."""
    ERRORS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ERRORS_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{message}\n")
        f.write(f"  Title: {title}\n")


def log_error(title: str, error: Exception) -> None:
    log_message(title, f"FAILED: {type(error).__name__}: {error}")


def fetch_rows_needing_summary(
    conn: sqlite3.Connection, limit: int
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """
        SELECT id, title, pdf_link
        FROM circulars
        WHERE summary IS NULL OR TRIM(summary) = ''
        ORDER BY id
        LIMIT ?
        """,
        (limit,),
    )
    return list(cursor.fetchall())


def download_pdf(pdf_link: str, dest: Path) -> None:
    """Download a PDF to a temp path. Raises on HTTP/timeout/empty body."""
    if not pdf_link or not pdf_link.strip():
        raise ValueError("Empty pdf_link")

    response = requests.get(
        pdf_link.strip(),
        headers=HEADERS,
        timeout=DOWNLOAD_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()
    if not response.content:
        raise ValueError("PDF download returned empty body")

    dest.write_bytes(response.content)


def extract_text_pdfplumber(pdf_path: Path) -> str:
    import pdfplumber

    chunks: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                chunks.append(page_text)
    return "\n".join(chunks)


def extract_text_pypdf2(pdf_path: Path) -> str:
    from PyPDF2 import PdfReader

    reader = PdfReader(str(pdf_path))
    chunks: list[str] = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        if page_text.strip():
            chunks.append(page_text)
    return "\n".join(chunks)


def extract_text_ocr(pdf_path: Path) -> str:
    """Optional OCR path for scanned PDFs (requires pdf2image + pytesseract)."""
    from pdf2image import convert_from_path
    import pytesseract

    images = convert_from_path(str(pdf_path))
    chunks: list[str] = []
    for image in images:
        page_text = pytesseract.image_to_string(image) or ""
        if page_text.strip():
            chunks.append(page_text)
    return "\n".join(chunks)


def clean_extracted_text(text: str) -> str:
    """Normalize whitespace and drop repeated headers/footers/page numbers."""
    if not text:
        return ""

    # Normalize line endings and non-breaking spaces.
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    lines = [line for line in lines if line]

    # Drop exact duplicate consecutive lines (common repeated headers/footers).
    deduped: list[str] = []
    for line in lines:
        if deduped and deduped[-1] == line:
            continue
        deduped.append(line)

    # Drop lines that are only page numbers / "Page N of M".
    page_pat = re.compile(
        r"^(?:page\s*)?\d+(?:\s*(?:of|/)\s*\d+)?$",
        re.IGNORECASE,
    )
    cleaned_lines = [line for line in deduped if not page_pat.match(line)]

    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def truncate_for_model(text: str) -> str:
    """
    Soft safety limit: if text is extremely long, keep head + tail.
    Never return more than HARD_MAX_CHARS.
    """
    if len(text) <= SOFT_TRUNCATE_THRESHOLD:
        return text[:HARD_MAX_CHARS]

    # Prefer first 35k + last 5k when over the soft threshold.
    head = text[:HEAD_CHARS]
    tail = text[-TAIL_CHARS:]
    combined = (
        head
        + "\n\n[... middle section truncated for length ...]\n\n"
        + tail
    )
    if len(combined) > HARD_MAX_CHARS:
        combined = combined[:HARD_MAX_CHARS]
    return combined


def extract_pdf_text(pdf_path: Path, title: str) -> tuple[str, str]:
    """
    Extract and clean PDF text.

    Returns (cleaned_text, method) where method is 'pdfplumber', 'pypdf2',
    'ocr', or '' if nothing usable was extracted.
    """
    cleaned = ""
    method = ""

    # Prefer pdfplumber; fall back to PyPDF2 on error or near-empty output.
    try:
        cleaned = clean_extracted_text(extract_text_pdfplumber(pdf_path))
        method = "pdfplumber"
    except Exception as exc:  # noqa: BLE001
        log_message(
            title,
            f"NOTE: pdfplumber failed ({type(exc).__name__}: {exc}); trying PyPDF2",
        )

    if len(cleaned) < MIN_USEFUL_CHARS:
        try:
            pypdf_text = clean_extracted_text(extract_text_pypdf2(pdf_path))
            if len(pypdf_text) > len(cleaned):
                cleaned = pypdf_text
                method = "pypdf2"
        except Exception as exc:  # noqa: BLE001
            log_message(
                title,
                f"NOTE: PyPDF2 failed ({type(exc).__name__}: {exc})",
            )

    if len(cleaned) >= MIN_USEFUL_CHARS:
        return cleaned, method

    # Likely a scanned PDF — try OCR if the optional deps are installed.
    log_message(
        title,
        "NOTE: PDF text extraction returned almost no readable text; trying OCR",
    )
    try:
        ocr_cleaned = clean_extracted_text(extract_text_ocr(pdf_path))
        if len(ocr_cleaned) >= MIN_USEFUL_CHARS:
            return ocr_cleaned, "ocr"
        log_message(
            title,
            "FALLBACK: Used title only because OCR returned almost no readable text",
        )
    except ImportError as exc:
        log_message(
            title,
            "FALLBACK: Used title only because OCR deps are not installed "
            f"(pdf2image/pytesseract): {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        log_message(
            title,
            "FALLBACK: Used title only because OCR failed "
            f"({type(exc).__name__}: {exc})",
        )

    log_message(
        title,
        "FALLBACK: Used title only because PDF text extraction failed",
    )
    return "", ""


def get_circular_text(title: str, pdf_link: str | None) -> tuple[str, bool]:
    """
    Download PDF and extract cleaned text.

    Returns (text_or_empty, used_title_fallback).
    Temporary PDF files are always deleted.
    """
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            download_pdf(pdf_link or "", tmp_path)
        except Exception as exc:  # noqa: BLE001
            log_message(
                title,
                "FALLBACK: Used title only because PDF download failed "
                f"({type(exc).__name__}: {exc})",
            )
            return "", True

        text, method = extract_pdf_text(tmp_path, title)
        if text:
            print(f"  Extracted text via {method} ({len(text)} chars)")
            return truncate_for_model(text), False

        # extract_pdf_text already logged the specific FALLBACK reason.
        return "", True
    finally:
        # Always clean up the temp PDF, even on failure.
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def build_prompt(title: str, text: str, title_only: bool) -> str:
    # On PDF failure, send the title as the only available text.
    if title_only or not text.strip():
        body = title
    else:
        body = f"Title: {title}\n\n{text}"
    return PROMPT_TEMPLATE.format(text=body)


def is_rate_limit_error(exc: Exception) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, APIStatusError) and getattr(exc, "status_code", None) == 429:
        return True
    message = str(exc).lower()
    return "429" in message or "rate limit" in message


def create_chat_completion(
    client: OpenAI,
    *,
    messages: list[dict[str, str]],
    temperature: float,
):
    """Call OpenRouter with exponential backoff on HTTP 429 responses."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES_429):
        try:
            return client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001
            if not is_rate_limit_error(exc):
                raise
            last_exc = exc
            if attempt >= MAX_RETRIES_429 - 1:
                break
            wait = min(BACKOFF_BASE_SECONDS * (2**attempt), 60)
            print(
                f"Rate limited (429). Waiting {wait}s before retry "
                f"{attempt + 1}/{MAX_RETRIES_429 - 1}...",
                file=sys.stderr,
            )
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def summarize_circular(client: OpenAI, title: str, pdf_link: str | None) -> str:
    # Skip PDF download / LLM for non-English titles (common for some IRDAI rows).
    try:
        detected_language = detect(title)
    except LangDetectException:
        detected_language = "en"

    if detected_language != "en":
        log_message(
            title,
            f"SKIPPED: Non-English circular detected, language={detected_language}",
        )
        return f"Non-English circular. Title: {title}"

    text, title_only = get_circular_text(title, pdf_link)
    prompt = build_prompt(title, text, title_only)

    # Final hard cap on total prompt size (title + instructions + body).
    if len(prompt) > HARD_MAX_CHARS + 2000:
        prompt = prompt[: HARD_MAX_CHARS + 2000]

    response = create_chat_completion(
        client,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    content = response.choices[0].message.content
    if not content or not content.strip():
        raise ValueError("Empty response from model")
    return content.strip()


def main() -> int:
    try:
        client = get_client()
    except Exception as exc:  # noqa: BLE001
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1

    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    processed = 0
    failed = 0

    try:
        rows = fetch_rows_needing_summary(conn, LIMIT_ROWS)
        total = len(rows)
        if total == 0:
            print("No rows with empty summary to process.")
            return 0

        print(f"Processing up to {total} of {LIMIT_ROWS} max row(s) this run (LIMIT_ROWS={LIMIT_ROWS})...")
        print(f"Model: {MODEL}")

        for index, row in enumerate(rows, start=1):
            row_id = row["id"]
            title = row["title"]
            pdf_link = row["pdf_link"]
            try:
                summary = summarize_circular(client, title, pdf_link)
                conn.execute(
                    "UPDATE circulars SET summary = ?, updated_at = datetime('now') WHERE id = ?",
                    (summary, row_id),
                )
                conn.commit()
                processed += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log_error(title, exc)
                print(
                    f"Failed on: {title[:80]}\n"
                    f"  Error type: {type(exc).__name__}\n"
                    f"  Error: {exc}",
                    file=sys.stderr,
                )

            print(f"Processed {index}/{total}")
            if index < total:
                time.sleep(DELAY_SECONDS)

        print(f"Done. Total processed: {processed}. Total failed: {failed}.")
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
