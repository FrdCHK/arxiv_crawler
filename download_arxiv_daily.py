import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import requests
import yaml


DEFAULT_DOWNLOAD_SETTINGS = {
    "arxiv": {
        "api_url": "http://export.arxiv.org/api/query",
        "category": "astro-ph*",
        "max_results": 1000,
        "user_agent": "arxiv crawler (research project; contact: your_email@example.com)",
    },
    "download": {
        "lookback_days": 4,
        "anchor_date_utc": "yesterday",
        "retry_interval_sec": 10,
        "max_retry_attempts": 0,
        "database": {
            "path": "data/arxiv.db",
        },
        "save_daily_json": True,
        "data_dir": "data",
    },
}

logger = logging.getLogger(__name__)


def setup_logging():
    log_dir = Path("log")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "download_arxiv_daily.log"
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)


def deep_update(base, updates):
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_download_settings(path="settings.yaml"):
    settings = json.loads(json.dumps(DEFAULT_DOWNLOAD_SETTINGS))
    settings_path = Path(path)
    if settings_path.exists():
        user_settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
        deep_update(settings, user_settings)
    return settings


def clean_text(raw):
    return " ".join(str(raw or "").replace("\n", " ").split())


def parse_anchor_date_utc(value):
    now_utc = datetime.now(timezone.utc)
    if str(value).lower() == "yesterday":
        return (now_utc - timedelta(days=1)).date()
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def build_submitted_date_range_window(start_date, end_date):
    start = f"{start_date.strftime('%Y%m%d')}0000"
    end = f"{end_date.strftime('%Y%m%d')}2359"
    return start, end


def fetch_arxiv_submitted_range(arxiv_cfg, start_date, end_date, retry_interval_sec=10, max_retry_attempts=0):
    start_str, end_str = build_submitted_date_range_window(start_date, end_date)
    query = (
        f'cat:{arxiv_cfg.get("category", "astro-ph*")} '
        f"AND submittedDate:[{start_str} TO {end_str}]"
    )
    params = {
        "search_query": query,
        "start": 0,
        "max_results": int(arxiv_cfg.get("max_results", DEFAULT_DOWNLOAD_SETTINGS["arxiv"]["max_results"])),
        "sortBy": "submittedDate",
        "sortOrder": "ascending",
    }
    headers = {"User-Agent": arxiv_cfg.get("user_agent", DEFAULT_DOWNLOAD_SETTINGS["arxiv"]["user_agent"])}
    api_url = arxiv_cfg.get("api_url", DEFAULT_DOWNLOAD_SETTINGS["arxiv"]["api_url"])

    attempt = 1
    while True:
        try:
            logger.info(
                "Requesting arXiv API for range %s..%s (attempt=%s)",
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
                attempt,
            )
            resp = requests.get(api_url, params=params, headers=headers, timeout=30)
            if resp.status_code != 200:
                raise requests.HTTPError(f"Unexpected status code: {resp.status_code}", response=resp)
            break
        except requests.RequestException as exc:
            can_retry = max_retry_attempts <= 0 or attempt < max_retry_attempts
            if not can_retry:
                logger.error("Request failed and retries exhausted: %s", exc)
                raise
            logger.warning(
                "Request failed for range %s..%s (attempt=%s): %s. Retrying in %.1fs",
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
                attempt,
                exc,
                retry_interval_sec,
            )
            time.sleep(max(float(retry_interval_sec), 0.0))
            attempt += 1

    root = ET.fromstring(resp.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    papers = []
    for entry in root.findall("atom:entry", ns):
        paper_url = entry.findtext("atom:id", default="", namespaces=ns).strip()
        paper_id = paper_url.split("/")[-1]
        title = clean_text(entry.findtext("atom:title", default="", namespaces=ns))
        abstract = clean_text(entry.findtext("atom:summary", default="", namespaces=ns))

        authors = [
            clean_text(author.findtext("atom:name", default="", namespaces=ns))
            for author in entry.findall("atom:author", ns)
            if author.findtext("atom:name", default="", namespaces=ns).strip()
        ]

        subjects = []
        for category in entry.findall("atom:category", ns):
            term = (category.attrib.get("term") or "").strip()
            if term and term not in subjects:
                subjects.append(term)

        published_text = entry.findtext("atom:published", default="", namespaces=ns).strip()
        updated_text = entry.findtext("atom:updated", default="", namespaces=ns).strip()

        papers.append(
            {
                "id": paper_id,
                "title": title,
                "abstract": abstract,
                "author_split": authors,
                "subject_split": subjects,
                "published": published_text,
                "updated": updated_text,
                "arxiv_url": f"https://arxiv.org/abs/{paper_id}",
            }
        )

    logger.info(
        "Download success for range %s..%s, query='%s', count=%s",
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
        query,
        len(papers),
    )
    return papers, query


def paper_day_utc(paper):
    published = str(paper.get("published", "")).strip()
    try:
        return datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").date()
    except ValueError:
        return None


def group_papers_by_day(papers, day_list):
    grouped = {day: [] for day in day_list}
    for paper in papers:
        day = paper_day_utc(paper)
        if day in grouped:
            grouped[day].append(paper)
    return grouped


def init_db(db_path):
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS papers (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            abstract TEXT NOT NULL,
            author_split_json TEXT NOT NULL,
            subject_split_json TEXT NOT NULL,
            published TEXT,
            updated TEXT,
            arxiv_url TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published)")
    conn.commit()
    logger.info("Database ready: %s", p)
    return conn


def upsert_papers(conn, papers):
    created_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = 0
    updated = 0

    for p in papers:
        existing = conn.execute("SELECT id FROM papers WHERE id = ?", (p["id"],)).fetchone()
        params = (
            p["title"],
            p["abstract"],
            json.dumps(p["author_split"], ensure_ascii=False),
            json.dumps(p["subject_split"], ensure_ascii=False),
            p.get("published", ""),
            p.get("updated", ""),
            p["arxiv_url"],
            created_ts,
            created_ts,
            p["id"],
        )
        if existing is None:
            conn.execute(
                """
                INSERT INTO papers (
                    title, abstract, author_split_json, subject_split_json,
                    published, updated, arxiv_url,
                    created_at_utc, updated_at_utc, id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
            inserted += 1
        else:
            conn.execute(
                """
                UPDATE papers
                SET
                    title = ?,
                    abstract = ?,
                    author_split_json = ?,
                    subject_split_json = ?,
                    published = ?,
                    updated = ?,
                    arxiv_url = ?,
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (
                    p["title"],
                    p["abstract"],
                    json.dumps(p["author_split"], ensure_ascii=False),
                    json.dumps(p["subject_split"], ensure_ascii=False),
                    p.get("published", ""),
                    p.get("updated", ""),
                    p["arxiv_url"],
                    created_ts,
                    p["id"],
                ),
            )
            updated += 1

    conn.commit()
    logger.info("Database upsert completed: inserted=%s updated=%s", inserted, updated)
    return inserted, updated


def save_daily_json(data_dir, target_date, papers, query):
    out_dir = Path(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    date_str = target_date.strftime("%Y-%m-%d")
    out_file = out_dir / f"{date_str}.json"

    payload = {
        "metadata": {
            "target_date_utc": date_str,
            "query": query,
            "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "paper_count": len(papers),
            "time_basis": "submittedDate (release/initial submission time)",
        },
        "papers": papers,
    }
    out_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Saved daily JSON: %s", out_file)
    return out_file


def main():
    setup_logging()
    logger.info("Starting daily arXiv download")
    settings = load_download_settings("settings.yaml")
    download_cfg = settings["download"]

    lookback_days = max(int(download_cfg.get("lookback_days", 4)), 1)
    anchor_date = parse_anchor_date_utc(download_cfg.get("anchor_date_utc", "yesterday"))
    retry_interval_sec = max(float(download_cfg.get("retry_interval_sec", 10)), 0.0)
    max_retry_attempts = int(download_cfg.get("max_retry_attempts", 0))
    db_path = download_cfg.get("database", {}).get("path", "data/arxiv.db")
    logger.info(
        "Loaded config: lookback_days=%s anchor_date=%s retry_interval_sec=%.1f max_retry_attempts=%s db=%s",
        lookback_days,
        anchor_date,
        retry_interval_sec,
        max_retry_attempts,
        db_path,
    )

    conn = init_db(db_path)
    try:
        total_downloaded = 0
        total_inserted = 0
        total_updated = 0

        day_list = [anchor_date - timedelta(days=i) for i in range(lookback_days)]
        range_start = min(day_list)
        range_end = max(day_list)
        logger.info("Single-request mode for range %s..%s", range_start, range_end)
        all_papers, query = fetch_arxiv_submitted_range(
            settings["arxiv"],
            range_start,
            range_end,
            retry_interval_sec=retry_interval_sec,
            max_retry_attempts=max_retry_attempts,
        )
        papers_by_day = group_papers_by_day(all_papers, day_list)

        for day in day_list:
            day_str = day.strftime("%Y-%m-%d")
            papers = papers_by_day.get(day, [])
            inserted, updated = upsert_papers(conn, papers)

            total_downloaded += len(papers)
            total_inserted += inserted
            total_updated += updated
            logger.info("[%s] downloaded=%s inserted=%s updated=%s", day_str, len(papers), inserted, updated)

            if bool(download_cfg.get("save_daily_json", True)):
                save_daily_json(download_cfg.get("data_dir", "data"), day, papers, query)

        logger.info(
            "Done, lookback_days=%s total_downloaded=%s new=%s existing_updated=%s db=%s",
            lookback_days,
            total_downloaded,
            total_inserted,
            total_updated,
            db_path,
        )
    finally:
        conn.close()
        logger.info("Database connection closed")


if __name__ == "__main__":
    main()
