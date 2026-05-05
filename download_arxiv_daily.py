import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import requests
import yaml


DEFAULT_DOWNLOAD_SETTINGS = {
    "arxiv": {
        "api_url": "http://export.arxiv.org/api/query",
        "category": "astro-ph*",
        "max_results": 2000,
        "user_agent": "arxiv crawler (research project; contact: your_email@example.com)",
    },
    "download": {
        "lookback_days": 4,
        "anchor_date_utc": "yesterday",
        "database": {
            "path": "data/arxiv.db",
        },
        "save_daily_json": True,
        "data_dir": "data",
    },
}


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


def build_submitted_date_range(target_date):
    start = f"{target_date.strftime('%Y%m%d')}0000"
    end = f"{target_date.strftime('%Y%m%d')}2359"
    return start, end


def fetch_arxiv_submitted_day(arxiv_cfg, target_date):
    start_str, end_str = build_submitted_date_range(target_date)
    query = (
        f'cat:{arxiv_cfg.get("category", "astro-ph*")} '
        f"AND submittedDate:[{start_str} TO {end_str}]"
    )
    params = {
        "search_query": query,
        "start": 0,
        "max_results": int(arxiv_cfg.get("max_results", 2000)),
        "sortBy": "submittedDate",
        "sortOrder": "ascending",
    }
    headers = {"User-Agent": arxiv_cfg.get("user_agent", DEFAULT_DOWNLOAD_SETTINGS["arxiv"]["user_agent"])}
    api_url = arxiv_cfg.get("api_url", DEFAULT_DOWNLOAD_SETTINGS["arxiv"]["api_url"])

    resp = requests.get(api_url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()

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

    print(f"download success, query='{query}', count={len(papers)}")
    return papers, query


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
    return out_file


def main():
    settings = load_download_settings("settings.yaml")
    download_cfg = settings["download"]

    lookback_days = max(int(download_cfg.get("lookback_days", 4)), 1)
    anchor_date = parse_anchor_date_utc(download_cfg.get("anchor_date_utc", "yesterday"))
    db_path = download_cfg.get("database", {}).get("path", "data/arxiv.db")

    conn = init_db(db_path)
    try:
        total_downloaded = 0
        total_inserted = 0
        total_updated = 0

        for i in range(lookback_days):
            day = anchor_date - timedelta(days=i)
            day_str = day.strftime("%Y-%m-%d")
            papers, query = fetch_arxiv_submitted_day(settings["arxiv"], day)
            inserted, updated = upsert_papers(conn, papers)

            total_downloaded += len(papers)
            total_inserted += inserted
            total_updated += updated
            print(f"[{day_str}] downloaded={len(papers)}, inserted={inserted}, updated={updated}")

            if bool(download_cfg.get("save_daily_json", True)):
                out_file = save_daily_json(download_cfg.get("data_dir", "data"), day, papers, query)
                print(f"[{day_str}] saved json: {out_file}")

        print(
            "done, "
            f"lookback_days={lookback_days}, total_downloaded={total_downloaded}, "
            f"new={total_inserted}, existing_updated={total_updated}, db={db_path}"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
