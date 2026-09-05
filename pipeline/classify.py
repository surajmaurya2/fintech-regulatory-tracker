"""
Classify circulars via OpenRouter (google/gemini-2.5-flash).

Fills category, severity, and change_type for rows that already have a summary.
"""

from __future__ import annotations

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
ENV_PATH = PROJECT_ROOT / ".env"
ERRORS_LOG = Path(__file__).resolve().parent / "errors.log"

LIMIT_ROWS = 50
MODEL = "google/gemini-2.5-flash"
BASE_URL = "https://openrouter.ai/api/v1"
DELAY_SECONDS = 1
MAX_RETRIES_429 = 6
BACKOFF_BASE_SECONDS = 1  # 1s, 2s, 4s, 8s, 16s, 32s

CATEGORIES = [
    "KYC / AML",
    "Lending / Digital Lending",
    "Payments / PPI / Wallets",
    "Insurance",
    "Capital Markets / Securities",
    "Audit & Internal Controls",
    "Fraud & Financial Crime",
    "Governance / Compliance Function",
    "Data Protection / IT / Cyber",
    "Cross-border / FEMA",
    "Other",
]

SEVERITIES = ["Low", "Medium", "High"]

CHANGE_TYPES = [
    "New Reporting Requirement",
    "Licensing Change",
    "Penalty or Enforcement",
    "Operational Deadline",
    "Policy Clarification",
    "Repeal or Consolidation",
    "Other",
]

PROMPT_TEMPLATE = """You are an expert in Indian financial regulation (RBI, SEBI, IRDAI).

Classify the circular using ONLY the title and summary provided.

Return the result in EXACTLY this format and nothing else:
CATEGORY: [value] | SEVERITY: [value] | CHANGE_TYPE: [value]

Allowed values:

CATEGORY (choose the most specific one):
- KYC / AML
- Lending / Digital Lending
- Payments / PPI / Wallets
- Insurance
- Capital Markets / Securities
- Audit & Internal Controls
- Fraud & Financial Crime
- Governance / Compliance Function
- Data Protection / IT / Cyber
- Cross-border / FEMA
- Other

CATEGORY disambiguation rules:
- Internal Audit Function, Concurrent Audit, Statutory Audit, Supervisory Returns → "Audit & Internal Controls".
- Fraud Risk Management, counterfeit currency (FICN), cash-handling security → "Fraud & Financial Crime".
- Broad compliance-function structure, board-level governance frameworks, general supervisory consolidations that are not specifically about audit or fraud → "Governance / Compliance Function".
- Digital payment / card / wallet / UPI security controls → "Payments / PPI / Wallets" when the primary subject is payment products or payment security; use "Data Protection / IT / Cyber" only when the focus is broader IT/cyber governance not tied to a specific payment product.
- Pure consolidations or repeals still take the category of the underlying subject matter — do not default to "Other".
- Use "Other" ONLY when the circular is clearly outside all listed categories.

SEVERITY:
- High = Significant operational change, new restrictions, major compliance burden, or new systems required
- Medium = Process changes, new reporting, or moderate compliance effort
- Low = Clarifications, minor updates, consolidations, or informational

CHANGE_TYPE — decide based on WHAT THE CIRCULAR DOES, not its title or document format:
- New Reporting Requirement: creates a new obligation, standard, framework, or binding requirement that entities must now follow, regardless of whether the document is titled "Directions," "Master Circular," "Framework," "Guidelines," or anything else. Ask: does this create a new duty, control, function, or structure that did not exist as a binding requirement before? If yes, use this, even if it doesn't literally require a "report."
- Licensing Change: changes who can operate, register, or hold a license, or changes licensing conditions.
- Penalty or Enforcement: imposes or describes a penalty, enforcement action, or punitive consequence for non-compliance.
- Operational Deadline: sets or extends a date/timeline for compliance with an EXISTING requirement, without changing what is required.
- Policy Clarification: explains, interprets, or restates an existing rule WITHOUT creating any new obligation, control, or standard. Use ONLY when nothing new is being required of entities.
- Repeal or Consolidation: withdraws, merges, or supersedes previous circulars.
- Other: none of the above clearly applies.

Critical test before choosing "Policy Clarification": if the circular establishes a NEW function, framework, control, audit requirement, reporting format, or compliance structure that entities did not have to follow before this circular, it is NOT a clarification.

Example: "RBI establishes Compliance Function requirements for Commercial Banks" → New Reporting Requirement (a new function/structure is being mandated).
Example: "RBI clarifies that its earlier KYC circular also applies to co-lending arrangements" → Policy Clarification (no new obligation, just scope clarification of an existing rule).

Rules:
- Prefer the most specific category using the disambiguation rules above.
- Be consistent with severity definitions.
- Do not invent information.

Title: {title}

Summary: {summary}
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


def fetch_rows_needing_classification(
    conn: sqlite3.Connection, limit: int
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        """
        SELECT id, title, summary
        FROM circulars
        WHERE (category IS NULL OR TRIM(category) = '')
          AND summary IS NOT NULL
          AND TRIM(summary) != ''
        ORDER BY id
        LIMIT ?
        """,
        (limit,),
    )
    return list(cursor.fetchall())


def build_prompt(title: str, summary: str) -> str:
    return PROMPT_TEMPLATE.format(title=title, summary=summary)


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


def _extract_field(text: str, label: str) -> str:
    """Pull the value after LABEL: up to the next | or end of string."""
    pattern = rf"{re.escape(label)}\s*:\s*(.+?)(?:\s*\||$)"
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip().strip("[]").strip()


def parse_classification(raw: str) -> tuple[str, str, str]:
    """
    Parse CATEGORY / SEVERITY / CHANGE_TYPE from the model reply.
    Invalid category/change_type default to "Other";
    invalid severity defaults to "Medium".
    """
    cleaned = (raw or "").strip()
    # Keep only the first line if the model adds extra text.
    first_line = cleaned.splitlines()[0].strip() if cleaned else ""

    category = _extract_field(first_line, "CATEGORY")
    severity = _extract_field(first_line, "SEVERITY")
    change_type = _extract_field(first_line, "CHANGE_TYPE")

    if category not in CATEGORIES:
        category = "Other"
    if severity not in SEVERITIES:
        severity = "Medium"
    if change_type not in CHANGE_TYPES:
        change_type = "Other"

    return category, severity, change_type


def classify_circular(client: OpenAI, title: str, summary: str) -> tuple[str, str, str]:
    response = create_chat_completion(
        client,
        messages=[{"role": "user", "content": build_prompt(title, summary)}],
        temperature=0.0,
    )
    content = response.choices[0].message.content
    if not content or not content.strip():
        raise ValueError("Empty response from model")
    return parse_classification(content)


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
        rows = fetch_rows_needing_classification(conn, LIMIT_ROWS)
        total = len(rows)
        if total == 0:
            print("No rows with empty category (and non-empty summary) to process.")
            return 0

        print(
            f"Processing up to {total} of {LIMIT_ROWS} max row(s) this run "
            f"(LIMIT_ROWS={LIMIT_ROWS})..."
        )
        print(f"Model: {MODEL}")

        for index, row in enumerate(rows, start=1):
            row_id = row["id"]
            title = row["title"]
            summary = row["summary"]
            try:
                if (summary or "").startswith("Non-English circular."):
                    category, severity, change_type = "Other", "Low", "Other"
                    try:
                        from langdetect import LangDetectException, detect

                        detected_language = detect(title)
                    except Exception:  # noqa: BLE001
                        detected_language = "unknown"
                    log_message(
                        title,
                        "SKIPPED: Non-English circular detected, "
                        f"language={detected_language}",
                    )
                else:
                    category, severity, change_type = classify_circular(
                        client, title, summary
                    )
                conn.execute(
                    """
                    UPDATE circulars
                    SET category = ?, severity = ?, change_type = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (category, severity, change_type, row_id),
                )
                conn.commit()
                processed += 1
                print(f"  -> {category} | {severity} | {change_type}")
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
