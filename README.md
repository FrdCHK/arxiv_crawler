# AI-driven arXiv crawler

Split into two parts:

1. `download_arxiv_daily.py`: daily rolling-window downloader + deduplicated SQLite ingest
2. `screen_arxiv.py`: multi-user LLM screening from SQLite

## Why this design

arXiv visibility can lag submission (for example, Friday submissions may appear Monday). A single-day download can miss papers. The downloader now fetches a rolling window (default 4 UTC days) every run, deduplicates by arXiv id, and stores to SQLite.

## Files

- `download_arxiv_daily.py`: downloader + DB ingest
- `screen_arxiv.py`: multi-user screening
- `settings.yaml`: downloader config
- `settings/`: per-user screening settings (`*.yaml`, `*.yml`, `*.json`)
- `data/arxiv.db`: SQLite database (auto-created)
- `data/*.json`: optional daily JSON snapshots (if enabled)
- `account.json`: email account config (only if `send_email: true`)

## Downloader settings

`settings.yaml` controls:

- arXiv API URL/category/max results/user-agent
- rolling window size (`download.lookback_days`, default `4`)
- anchor day (`download.anchor_date_utc`, usually `"yesterday"`)
- SQLite path (`download.database.path`)
- optional JSON snapshots (`download.save_daily_json`)

The downloader queries each day in the window with:

- `submittedDate:[YYYYMMDD0000 TO YYYYMMDD2359]`

Database behavior:

- primary key: `id` (arXiv id)
- new papers are inserted
- existing papers are updated (title/abstract/authors/subjects/updated time)

## Screening settings (per user)

Each file in `settings/` should include an inline `interest` field.

Example: `settings/user_example.yaml`.

Important fields:

- `user.name`: user label
- `data.database.path`: SQLite path
- `data.recent_days`: load papers published in past N UTC days (for example `7`)
- `interest`: user research interests
- `llm.*`: local LLM endpoint/model
- `selection.threshold`: keep papers with score >= threshold
- `output.*`: output HTML/email options

## Email config

Create `account.json`:

```json
{
  "sender": {
    "server": "smtp server",
    "port": 994,
    "user": "email address",
    "passwd": "password"
  },
  "receiver": "email address"
}
```

## Run

Update DB daily:

```bash
python download_arxiv_daily.py
```

Run screening for all users under `settings/`:

```bash
python screen_arxiv.py
```

A convenient way is to set up cron jobs for the two scripts, for example, run `download_arxiv_daily.py` daily and `screen_arxiv.py` weekly.

## Acknowledgement

Thank you to arXiv for use of its open access interoperability!
