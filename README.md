# Fintech Regulatory Tracker

A regulatory intelligence tracker for Indian fintech. It scrapes circulars from **RBI**, **SEBI**, and **IRDAI**, stores them locally, summarizes and classifies them with an LLM, maps which fintechs may be affected, and exposes a Streamlit search UI.

## Tech stack

- **Python** — scrapers, pipeline, and app
- **Cursor** — used to build and iterate on the project
- **OpenRouter API** — LLM calls for summarization, classification, and fintech matching
- **SQLite** — local store for circulars
- **Streamlit** — search and filter interface
- **BeautifulSoup** + **requests** — HTML scraping
- **pdfplumber** / **PyPDF2** — PDF text extraction

## Pipeline architecture

```
scrape → store → summarize (LLM) → classify (LLM) → match to fintechs (LLM) → search UI
```

1. **Scrape** — `scrapers/` pulls new circulars from RBI, SEBI, and IRDAI (incremental, with last-scrape dates).
2. **Store** — `pipeline/setup_db.py` loads CSVs into `data/regulatory_tracker.db`.
3. **Summarize** — `pipeline/summarize.py` downloads PDFs and writes factual summaries via OpenRouter.
4. **Classify** — `pipeline/classify.py` assigns category, severity, and change type.
5. **Match** — `scripts/match_fintechs.py` asks the model which companies in `data/fintechs.json` are materially affected.
6. **Search** — `app.py` is a read-only Streamlit dashboard over the database.

`run_pipeline.py` runs steps 1–5 in order.

## Setup

```bash
git clone https://github.com/surajmaurya2/fintech-regulatory-tracker.git
cd fintech-regulatory-tracker
```

Create a virtual environment and install dependencies:

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

Add your OpenRouter key (never commit `.env`):

```bash
copy .env.example .env   # Windows
# cp .env.example .env   # macOS / Linux
```

Open `.env` and set:

```
OPENROUTER_API_KEY=your_api_key_here
```

Run the pipeline, then the app:

```bash
# Full refresh (scrape + process)
python run_pipeline.py

# Or step by step:
python scrapers/rbi_scraper.py
python scrapers/sebi_scraper.py
python scrapers/irdai_scraper.py
python pipeline/setup_db.py
python pipeline/summarize.py
python pipeline/classify.py
python scripts/match_fintechs.py

python -m streamlit run app.py
```

On Windows, if `python` / `streamlit` are not on PATH, use `py` instead (`py run_pipeline.py`, `py -m streamlit run app.py`).

## Screenshots

[Add screenshot here]

## Disclaimer

Fintech-to-circular mapping is **LLM-inferred**. Treat it as a first-pass filter, not a verified legal or compliance determination. Always read the original circular (PDF) before acting.

## What it lacks / still figuring out

This is an early prototype. Gaps I am still working through:

- **No scheduler** — refreshes are manual (`run_pipeline.py` or the Streamlit sidebar button), not a daily cron/job.
- **No hosted deployment** — the app runs locally; there is no production URL, auth, or multi-user setup.
- **Matching quality** — LLM fintech mapping can miss companies or over-include them; it is not a legal mapping.
- **Small registry** — `data/fintechs.json` is a focused shortlist (~50 names), not the full Indian fintech market.
- **Batch limits** — summarize / classify / match still use a `LIMIT_ROWS` cap per run, so large backfills take several passes.
- **`--limit` on `run_pipeline.py`** is accepted but not yet passed through to child scripts.
- **Scraper brittleness** — RBI/SEBI/IRDAI HTML and pagination can change without notice.
- **No tests or CI** — no automated checks that scrapers, schema, or prompts still work.
- **No PDF archive** — PDFs are downloaded for summarization then discarded; only the link is stored.
- **Cost / rate limits** — OpenRouter usage is pay-as-you-go; 429s are retried, but long runs can still fail mid-batch.
- **UI polish** — table + expanders only; no saved views, alerts, or export.

## License

Personal / experimental project. Regulator websites remain the source of truth for all circulars.
