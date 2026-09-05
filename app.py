"""
Regulatory Intelligence Tracker — Streamlit read-only dashboard.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "data" / "regulatory_tracker.db"
PIPELINE_SCRIPT = PROJECT_ROOT / "run_pipeline.py"

SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2, "": 3}


def parse_fintech_matches(raw) -> list[str]:
    if raw is None:
        return []
    text = str(raw).strip()
    if not text or text == "[]":
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


@st.cache_data(show_spinner=False)
def load_circulars(db_mtime: float) -> pd.DataFrame:
    """Load circulars once; db_mtime busts the cache when the DB file changes."""
    del db_mtime  # used only as cache key
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            """
            SELECT
                id, title, date, source, category, severity, change_type,
                summary, fintech_matches, pdf_link
            FROM circulars
            """,
            conn,
        )
    finally:
        conn.close()

    df["fintechs"] = df["fintech_matches"].apply(parse_fintech_matches)
    df["match_count"] = df["fintechs"].apply(len)
    df["date_parsed"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    for col in ("title", "summary", "source", "category", "severity", "change_type", "pdf_link"):
        df[col] = df[col].fillna("").astype(str)
    return df


def apply_filters(
    df: pd.DataFrame,
    *,
    search: str,
    company: str,
    category: str,
    severity: str,
    change_type: str,
    date_window: str,
    today: date,
) -> pd.DataFrame:
    out = df

    if search.strip():
        q = search.strip().lower()

        def row_matches(row) -> bool:
            if q in (row["title"] or "").lower():
                return True
            if q in (row["summary"] or "").lower():
                return True
            return any(q in name.lower() for name in row["fintechs"])

        out = out[out.apply(row_matches, axis=1)]

    if company != "All":
        out = out[out["fintechs"].apply(lambda names: company in names)]

    if category != "All":
        out = out[out["category"] == category]

    if severity != "All":
        out = out[out["severity"] == severity]

    if change_type != "All":
        out = out[out["change_type"] == change_type]

    if date_window != "All":
        days = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}[date_window]
        cutoff = today - timedelta(days=days)
        out = out[out["date_parsed"].notna() & (out["date_parsed"] >= cutoff)]

    return out.copy()


def sort_frame(df: pd.DataFrame, sort_by: str) -> pd.DataFrame:
    if sort_by == "Date (oldest first)":
        return df.sort_values(["date_parsed", "id"], ascending=[True, True], na_position="last")
    if sort_by == "Severity (High → Low)":
        ranked = df["severity"].map(lambda s: SEVERITY_ORDER.get(s, 3))
        return df.assign(_sev=ranked).sort_values(
            ["_sev", "date_parsed", "id"], ascending=[True, False, True], na_position="last"
        ).drop(columns=["_sev"])
    if sort_by == "# Fintechs affected (most first)":
        return df.sort_values(
            ["match_count", "date_parsed", "id"],
            ascending=[False, False, True],
            na_position="last",
        )
    # Default: Date (newest first)
    return df.sort_values(["date_parsed", "id"], ascending=[False, True], na_position="last")


def truncate(text: str, limit: int = 220) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def main() -> None:
    st.set_page_config(
        page_title="Regulatory Intelligence Tracker",
        page_icon="📋",
        layout="wide",
    )

    st.markdown(
        """
        <style>
          .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
          div[data-testid="stMetricValue"] { font-size: 1.6rem; }
          .empty-state {
            border: 1px solid #e4e7ec;
            border-radius: 8px;
            padding: 1.25rem 1.5rem;
            color: #475467;
            background: #f9fafb;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.header("Data")

        if st.session_state.pop("pipeline_message", None):
            msg_type, msg_text = st.session_state.pop(
                "pipeline_message_payload", ("success", "Pipeline finished successfully.")
            )
            if msg_type == "success":
                st.success(msg_text)
            else:
                st.error(msg_text)

        if st.button("Refresh data (scrape + process)", use_container_width=True):
            with st.spinner("Running full pipeline..."):
                try:
                    result = subprocess.run(
                        [sys.executable, str(PIPELINE_SCRIPT)],
                        cwd=str(PROJECT_ROOT),
                        capture_output=True,
                        text=True,
                        timeout=None,
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Pipeline failed to start: {exc}")
                else:
                    if result.returncode == 0:
                        st.session_state["pipeline_message"] = True
                        st.session_state["pipeline_message_payload"] = (
                            "success",
                            "Pipeline finished successfully.",
                        )
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        detail = (result.stderr or result.stdout or "").strip()
                        if len(detail) > 500:
                            detail = detail[-500:]
                        st.error(
                            f"Pipeline failed (exit {result.returncode})."
                            + (f"\n\n{detail}" if detail else "")
                        )

        if st.button("Clear cache only", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    try:
        db_mtime = DB_PATH.stat().st_mtime if DB_PATH.exists() else 0.0
        df = load_circulars(db_mtime)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not load database: {exc}")
        st.stop()

    today = date.today()
    total = len(df)
    high_count = int((df["severity"] == "High").sum())
    last_7 = int(
        (
            df["date_parsed"].notna()
            & (df["date_parsed"] >= today - timedelta(days=7))
        ).sum()
    )
    last_30 = int(
        (
            df["date_parsed"].notna()
            & (df["date_parsed"] >= today - timedelta(days=30))
        ).sum()
    )

    st.title("Regulatory Intelligence Tracker")
    st.caption(f"{total} circulars tracked across RBI · SEBI · IRDAI")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total circulars", total)
    m2.metric("High severity", high_count)
    m3.metric("Last 7 days", last_7)
    m4.metric("Last 30 days", last_30)

    st.divider()

    companies = sorted(
        {name for names in df["fintechs"] for name in names},
        key=str.lower,
    )
    categories = sorted(
        {c for c in df["category"].tolist() if c},
        key=str.lower,
    )
    change_types = sorted(
        {c for c in df["change_type"].tolist() if c},
        key=str.lower,
    )

    c1, c2, c3, c4, c5, c6, c7 = st.columns([1.6, 1.1, 1.2, 0.9, 1.2, 1.0, 1.3])
    with c1:
        search = st.text_input("Search", placeholder="Title, summary, or company")
    with c2:
        company = st.selectbox("Company", ["All", *companies])
    with c3:
        category = st.selectbox("Category", ["All", *categories])
    with c4:
        severity = st.selectbox("Severity", ["All", "High", "Medium", "Low"])
    with c5:
        change_type = st.selectbox("Change Type", ["All", *change_types])
    with c6:
        date_window = st.selectbox(
            "Date window",
            ["All", "Last 7 days", "Last 30 days", "Last 90 days"],
        )
    with c7:
        sort_by = st.selectbox(
            "Sort by",
            [
                "Date (newest first)",
                "Date (oldest first)",
                "Severity (High → Low)",
                "# Fintechs affected (most first)",
            ],
        )

    filtered = apply_filters(
        df,
        search=search,
        company=company,
        category=category,
        severity=severity,
        change_type=change_type,
        date_window=date_window,
        today=today,
    )
    filtered = sort_frame(filtered, sort_by)

    st.markdown(f"**{len(filtered)} circulars match**")

    if filtered.empty:
        st.markdown(
            '<div class="empty-state">No circulars match the current filters. '
            "Try clearing search or widening the date window.</div>",
            unsafe_allow_html=True,
        )
        return

    display = pd.DataFrame(
        {
            "Title": filtered["title"],
            "Date": filtered["date"],
            "Source": filtered["source"],
            "Category": filtered["category"].replace("", "—"),
            "Severity": filtered["severity"].replace("", "—"),
            "Change Type": filtered["change_type"].replace("", "—"),
            "Summary": filtered["summary"].apply(truncate),
            "Fintechs Affected": filtered["fintechs"].apply(
                lambda names: ", ".join(names) if names else "—"
            ),
            "PDF": filtered["pdf_link"].apply(lambda u: u if u.strip() else None),
        }
    )

    severity_colors = {
        "High": "color: #b42318; font-weight: 600",
        "Medium": "color: #b54708; font-weight: 600",
        "Low": "color: #667085; font-weight: 600",
    }

    def style_severity(col: pd.Series) -> list[str]:
        return [severity_colors.get(str(v), "") for v in col]

    styled = display.style.apply(style_severity, subset=["Severity"])

    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Title": st.column_config.TextColumn("Title", width="large"),
            "Date": st.column_config.TextColumn("Date", width="small"),
            "Source": st.column_config.TextColumn("Source", width="small"),
            "Category": st.column_config.TextColumn("Category", width="medium"),
            "Severity": st.column_config.TextColumn("Severity", width="small"),
            "Change Type": st.column_config.TextColumn("Change Type", width="medium"),
            "Summary": st.column_config.TextColumn("Summary", width="large"),
            "Fintechs Affected": st.column_config.TextColumn(
                "Fintechs Affected", width="medium"
            ),
            "PDF": st.column_config.LinkColumn(
                "PDF", display_text="Open PDF", width="small"
            ),
        },
    )

    st.markdown("#### Full summaries")
    for _, row in filtered.iterrows():
        label = f"{row['date']} · {row['source'] or '—'} · {row['title'][:100]}"
        with st.expander(label):
            st.markdown(f"**Category:** {row['category'] or '—'}  \n"
                        f"**Severity:** {row['severity'] or '—'}  \n"
                        f"**Change type:** {row['change_type'] or '—'}")
            fintechs = ", ".join(row["fintechs"]) if row["fintechs"] else "—"
            st.markdown(f"**Fintechs affected:** {fintechs}")
            st.write(row["summary"] or "—")
            if row["pdf_link"].strip():
                st.markdown(f"[Open PDF]({row['pdf_link']})")


if __name__ == "__main__":
    main()
