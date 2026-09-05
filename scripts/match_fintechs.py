"""
Match regulatory circulars to Indian fintechs via OpenRouter (google/gemini-2.5-flash).

Uses the focused shortlist in data/fintechs.json and asks the model which
companies are realistically / materially affected by each circular.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import APIStatusError, OpenAI, RateLimitError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "regulatory_tracker.db"
FINTECHS_PATH = DATA_DIR / "fintechs.json"
ENV_PATH = PROJECT_ROOT / ".env"
ERRORS_LOG = PROJECT_ROOT / "pipeline" / "errors.log"

LIMIT_ROWS = 50
MODEL = "google/gemini-2.5-flash"
BASE_URL = "https://openrouter.ai/api/v1"
DELAY_SECONDS = 1
MAX_RETRIES_429 = 6
BACKOFF_BASE_SECONDS = 1  # 1s, 2s, 4s, 8s, 16s, 32s

PROMPT_TEMPLATE = """You are a regulatory compliance expert for Indian fintech.

Given the circular below and the list of fintech companies, return ONLY the companies that are realistically and materially affected by this circular.

Rules (be strict):
- If the circular applies only to Commercial Banks and explicitly excludes Payments Banks, Small Finance Banks, or Local Area Banks, do NOT include pure NBFCs, pure wealth/investment apps, pure insurers, pure lending apps, or pure KYC vendors unless they clearly perform banking functions.
- Prefer precision over recall. An empty list is better than a noisy list.
- A company is affected only if the circular creates new obligations, restrictions, reporting duties, or material compliance risk for that company.
- Do not include a company just because the topic is vaguely related (e.g. cyber, KYC, governance).

Circular title: {title}
Circular summary: {summary}

Fintech list:
{fintech_list}

Return ONLY a valid JSON array of company names, nothing else.
Example: ["PhonePe", "Razorpay"]
If none are affected, return: []
"""


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


def load_fintechs() -> list[dict]:
    if not FINTECHS_PATH.exists():
        raise FileNotFoundError(f"Fintech registry not found: {FINTECHS_PATH}")
    data = json.loads(FINTECHS_PATH.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "companies" in data:
        return data["companies"]
    if isinstance(data, list):
        return data  # backward compatible with older array format
    raise ValueError("fintechs.json must be a companies object or a JSON array")


def format_fintech_list(fintechs: list[dict]) -> str:
    lines: list[str] = []
    for item in fintechs:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        segments = item.get("segments") or []
        regulators = item.get("regulators") or []
        segment_text = ", ".join(segments) if segments else "Unknown"
        regulator_text = ", ".join(regulators) if regulators else "Unknown"
        lines.append(
            f"- {name} ({segment_text}) | regulators: {regulator_text}"
        )
    return "\n".join(lines)


def known_fintech_names(fintechs: list[dict]) -> set[str]:
    return {
        (item.get("name") or "").strip()
        for item in fintechs
        if (item.get("name") or "").strip()
    }


def fetch_rows_needing_matches(
    conn: sqlite3.Connection, limit: int
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """
        SELECT id, title, summary
        FROM circulars
        WHERE (fintech_matches IS NULL OR TRIM(fintech_matches) = '')
          AND summary IS NOT NULL
          AND TRIM(summary) != ''
        ORDER BY id
        LIMIT ?
        """,
        (limit,),
    )
    return list(cursor.fetchall())


def build_prompt(title: str, summary: str, fintech_list: str) -> str:
    return PROMPT_TEMPLATE.format(
        title=title,
        summary=summary,
        fintech_list=fintech_list,
    )


def parse_matches(raw: str, allowed_names: set[str]) -> list[str]:
    """Parse a JSON array of company names; fall back to regex extraction."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("Empty response from model")

    parsed: object | None = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*?\]", text)
        if not match:
            raise ValueError(f"Could not parse JSON array from response: {text[:200]!r}")
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, list):
        raise ValueError(f"Model response is not a JSON array: {parsed!r}")

    matched: list[str] = []
    for item in parsed:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name in allowed_names:
            matched.append(name)

    return sorted(set(matched), key=lambda n: n.lower())


def match_fintechs_for_row(
    client: OpenAI,
    title: str,
    summary: str,
    fintech_list: str,
    allowed_names: set[str],
) -> list[str]:
    response = create_chat_completion(
        client,
        messages=[
            {
                "role": "user",
                "content": build_prompt(title, summary, fintech_list),
            }
        ],
        temperature=0.0,
    )
    content = response.choices[0].message.content
    if not content or not content.strip():
        raise ValueError("Empty response from model")
    return parse_matches(content, allowed_names)


def main() -> int:
    try:
        client = get_client()
        fintechs = load_fintechs()
    except Exception as exc:  # noqa: BLE001
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1

    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        return 1

    fintech_list = format_fintech_list(fintechs)
    allowed_names = known_fintech_names(fintechs)

    conn = sqlite3.connect(DB_PATH)
    processed = 0
    failed = 0

    try:
        rows = fetch_rows_needing_matches(conn, LIMIT_ROWS)
        total = len(rows)
        if total == 0:
            print(
                "No rows with empty fintech_matches "
                "(and non-empty summary) to process."
            )
            return 0

        print(
            f"Processing up to {total} of {LIMIT_ROWS} max row(s) this run "
            f"(LIMIT_ROWS={LIMIT_ROWS})..."
        )
        print(f"Model: {MODEL}")
        print(f"Fintech registry: {len(allowed_names)} companies from {FINTECHS_PATH.name}")

        for index, row in enumerate(rows, start=1):
            row_id = row["id"]
            title = row["title"]
            summary = row["summary"]
            try:
                matched = match_fintechs_for_row(
                    client,
                    title,
                    summary,
                    fintech_list,
                    allowed_names,
                )
                payload = json.dumps(matched, ensure_ascii=False)
                conn.execute(
                    """
                    UPDATE circulars
                    SET fintech_matches = ?, updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (payload, row_id),
                )
                conn.commit()
                processed += 1
                print(f"  -> {len(matched)} match(es): {payload}")
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
