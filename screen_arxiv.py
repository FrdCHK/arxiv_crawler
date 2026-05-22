import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

import requests
import smtplib
import yaml
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


DEFAULT_SETTINGS = {
    "user": {
        "name": "default",
    },
    "data": {
        "database": {
            "path": "data/arxiv.db",
        },
        "recent_days": 7,
    },
    "interest": "",
    "llm": {
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "local-model",
        "timeout_sec": 240,
        "batch_size": 5,
        "temperature": 0.0,
        "max_tokens": 100000,
        "log_raw_response": False,
        "raw_response_log_file": "llm_raw_output.log",
    },
    "selection": {
        "threshold": 30,
    },
    "output": {
        "save_html": True,
        "output_dir": "output",
        "html_file": "arxiv_selected_{user}_{date}.html",
        "send_email": False,
        "email_address": "",
    },
}

logger = logging.getLogger(__name__)


def setup_logging():
    log_dir = Path("log")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "screen_arxiv.log"
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


def load_one_settings(path):
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    p = Path(path)
    if p.suffix.lower() == ".json":
        user_settings = json.loads(p.read_text(encoding="utf-8"))
    else:
        user_settings = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    deep_update(settings, user_settings)

    interest_text = str(settings.get("interest", "")).strip()
    if not interest_text:
        raise ValueError(f"missing required 'interest' in settings file: {p}")

    if not settings["user"].get("name"):
        settings["user"]["name"] = p.stem
    logger.info("Loaded settings: %s (user=%s)", p, settings["user"]["name"])
    return settings


def clean_text(raw):
    return " ".join(str(raw or "").replace("\n", " ").split())


def parse_iso_datetime(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def load_papers_from_db(db_path, recent_days):
    db_file = Path(db_path)
    if not db_file.exists():
        raise FileNotFoundError(f"database not found: {db_file}")

    now_utc_date = datetime.now(timezone.utc).date()
    recent_days = max(int(recent_days), 1)
    start_date = now_utc_date - timedelta(days=recent_days)
    start_time_str = f"{start_date.strftime('%Y-%m-%d')}T00:00:00Z"

    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                id, title, abstract, author_split_json, subject_split_json,
                published, updated, arxiv_url
            FROM papers
            WHERE published >= ?
            ORDER BY published DESC, id DESC
            """,
            (start_time_str,),
        ).fetchall()
    finally:
        conn.close()

    papers = []
    for row in rows:
        published = str(row["published"] or "").strip()
        published_dt = parse_iso_datetime(published) if published else datetime.utcnow()

        author_split = [clean_text(a) for a in json.loads(row["author_split_json"] or "[]")]
        subject_split = [clean_text(s) for s in json.loads(row["subject_split_json"] or "[]")]

        papers.append(
            {
                "date": published_dt.strftime("%a, %d %b %Y"),
                "datetime": published_dt,
                "id": clean_text(row["id"]),
                "title": clean_text(row["title"]),
                "abstract": clean_text(row["abstract"]),
                "authors": ", ".join(author_split),
                "author_split": author_split,
                "subjects": "; ".join(subject_split),
                "subject_split": subject_split,
            }
        )

    label = f"{start_date.strftime('%Y-%m-%d')}..{now_utc_date.strftime('%Y-%m-%d')} UTC (by published)"
    logger.info(
        "Loaded papers from db: db=%s recent_days=%s count=%s",
        db_file,
        recent_days,
        len(papers),
    )
    return papers, label


def extract_json(text):
    if not text:
        return []

    clean = text.strip()
    clean = re.sub(r"<think>.*?</think>", "", clean, flags=re.DOTALL | re.IGNORECASE).strip()
    clean = re.sub(r"^```(?:json)?", "", clean).strip()
    clean = re.sub(r"```$", "", clean).strip()

    try:
        parsed = json.loads(clean)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            if isinstance(parsed.get("results"), list):
                return parsed["results"]
            if isinstance(parsed.get("papers"), list):
                return parsed["papers"]
        return []
    except json.JSONDecodeError:
        pass

    array_match = re.search(r"\[\s*{.*}\s*\]", clean, flags=re.DOTALL)
    if array_match:
        try:
            return json.loads(array_match.group(0))
        except json.JSONDecodeError:
            pass

    obj_match = re.search(r"\{.*\}", clean, flags=re.DOTALL)
    if obj_match:
        try:
            parsed = json.loads(obj_match.group(0))
            if isinstance(parsed, dict):
                if isinstance(parsed.get("results"), list):
                    return parsed["results"]
                if isinstance(parsed.get("papers"), list):
                    return parsed["papers"]
        except json.JSONDecodeError:
            pass

    return []


def score_papers_with_llm(papers, interest_text, settings):
    llm_cfg = settings["llm"]
    batch_size = int(llm_cfg["batch_size"])
    score_map = {}
    url = f'{llm_cfg["base_url"].rstrip("/")}/chat/completions'
    log_raw = bool(llm_cfg.get("log_raw_response", False))
    raw_log_file = Path(llm_cfg.get("raw_response_log_file", "llm_raw_output.log"))

    system_prompt = (
        "/no_think "
        "Do not output reasoning, explanation, or chain-of-thought. "
        "You are a strict scientific paper relevance scorer. "
        "Given a user's research interest and arXiv title+abstract pairs, return ONLY JSON array. "
        "Each item MUST have keys: id (string), relevance_score (0-100 integer), reason (string). "
        "The reason must be 50-100 words and include both a brief summary and why this score is assigned. "
        "Score each paper independently. "
        "Score high if the paper is directly useful to the user's stated interests."
    )

    for start in range(0, len(papers), batch_size):
        batch = papers[start : start + batch_size]
        logger.info(
            "Scoring batch: start=%s size=%s total=%s model=%s",
            start,
            len(batch),
            len(papers),
            llm_cfg["model"],
        )
        paper_inputs = [
            {"id": row["id"], "title": row["title"], "abstract": row.get("abstract", "")}
            for row in batch
        ]
        user_prompt = (
            f"User interest:\n{interest_text}\n\n"
            "Paper title+abstract:\n"
            f"{json.dumps(paper_inputs, ensure_ascii=False, indent=2)}\n\n"
            "Return only JSON array with one object per input paper, same order. "
            'Output format: [{"id":"...", "relevance_score": 0, "reason":"..."}]. '
            "The reason must be 50-100 words. "
            "No extra keys. No prose."
        )
        payload = {
            "model": llm_cfg["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": float(llm_cfg["temperature"]),
            "max_tokens": int(llm_cfg["max_tokens"]),
        }
        resp = requests.post(url, json=payload, timeout=int(llm_cfg["timeout_sec"]))
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]

        if log_raw:
            with raw_log_file.open("a", encoding="utf-8") as f:
                f.write(f"=== batch start: {start}, size: {len(batch)} ===\n")
                f.write((content or "") + "\n\n")
            logger.info("Logged raw LLM output for batch start=%s -> %s", start, raw_log_file)

        scored_list = extract_json(content)
        if not scored_list:
            logger.warning("Could not parse JSON for batch start=%s", start)
            logger.info("Raw output: %s", content if content is not None else "")
            continue

        for item in scored_list:
            try:
                pid = str(item["id"]).strip()
                score = int(item["relevance_score"])
                reason = str(item.get("reason", "")).strip()
                if 0 <= score <= 100:
                    score_map[pid] = {"relevance_score": score, "reason": reason}
            except (KeyError, ValueError, TypeError):
                continue
        logger.info("LLM scored %s/%s papers", min(start + batch_size, len(papers)), len(papers))
        time.sleep(0.1)

    merged = []
    for paper in papers:
        scored = dict(paper)
        match = score_map.get(paper["id"], {})
        scored["relevance_score"] = int(match.get("relevance_score", 0))
        scored["reason"] = str(match.get("reason", "")).strip()
        merged.append(scored)
    return merged


def build_html(selected_papers, threshold, user_name, source_label):
    msg = (
        f"<h2>arXiv papers (AI-selected, threshold >= {threshold})</h2>"
        f"<p>User: <b>{user_name}</b> | Data window: <b>{source_label}</b></p>"
    )
    if not selected_papers:
        return msg + "<p>No papers passed the threshold.</p>"

    papers_gr = defaultdict(list)
    for item in selected_papers:
        papers_gr[item["datetime"].date()].append(item)

    for date in sorted(papers_gr.keys(), reverse=True):
        gr = papers_gr[date]
        msg += f"<h3>{date.strftime('%Y-%m-%d')}</h3>\n<ol>\n"
        for item in gr:
            msg += (
                f'<li><b>Title:</b> <a href="https://arxiv.org/abs/{item["id"]}">{item["title"]}</a><br/>'
                f'<b>Relevance:</b> {item["relevance_score"]}/100'
            )
            reason = str(item.get("reason", "")).strip()
            if reason:
                msg += "<br/><b>Reason:</b> " + reason
            msg += "<br/><b>Authors:</b> " + ", ".join(item["author_split"])
            msg += "<br/><b>Subjects:</b> " + ", ".join(item["subject_split"])
            msg += "</li>\n"
        msg += "</ol>"
    return msg


def send_email(sender, receiver, html_content):
    multi_part = MIMEMultipart("alternative")
    multi_part.attach(MIMEText(html_content, "html", "utf-8"))
    multi_part["From"] = sender["user"]
    multi_part["To"] = receiver
    multi_part["Subject"] = Header("arXiv weekly screening", "utf-8")

    smtp = smtplib.SMTP_SSL(host=sender["server"], port=sender["port"])
    smtp.login(sender["user"], sender["passwd"])
    smtp.sendmail(sender["user"], receiver, multi_part.as_string())
    smtp.quit()
    logger.info("Send email success to %s", receiver)


def resolve_output_html_path(output_cfg, user_name):
    output_dir = Path(output_cfg.get("output_dir", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_cfg.get("html_file", "arxiv_selected_{user}_{date}.html")
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fname = pattern.format(user=user_name, date=today_utc)
    return output_dir / fname


def run_for_settings_file(settings_path):
    settings = load_one_settings(settings_path)
    user_name = settings["user"]["name"]
    logger.info("Start processing user settings: %s", user_name)

    db_path = settings["data"]["database"]["path"]
    recent_days = settings["data"].get("recent_days", 7)
    papers, source_label = load_papers_from_db(db_path, recent_days)

    scored_papers = score_papers_with_llm(papers, settings["interest"], settings)
    threshold = int(settings["selection"]["threshold"])
    selected_papers = [
        p for p in scored_papers if int(p.get("relevance_score", 0)) >= threshold
    ]
    selected_papers.sort(key=lambda p: (p["datetime"], p["relevance_score"]), reverse=True)
    logger.info("[%s] selection completed, selected=%s", user_name, len(selected_papers))

    html_msg = build_html(selected_papers, threshold, user_name, source_label)

    output_cfg = settings["output"]
    if output_cfg.get("save_html", False):
        html_path = resolve_output_html_path(output_cfg, user_name)
        html_path.write_text(html_msg, encoding="utf-8")
        logger.info("[%s] saved html: %s", user_name, html_path)

    if output_cfg.get("send_email", False):
        receiver = str(output_cfg.get("email_address", "")).strip()
        if not receiver:
            logger.error("[%s] output.email_address is required when send_email=true", user_name)
            return
        with open("account.json", "r", encoding="utf-8") as accf:
            acc = json.load(accf)
        try:
            send_email(acc["sender"], receiver, html_msg)
        except smtplib.SMTPException:
            logger.exception("[%s] email not sent", user_name)


def iter_settings_files(settings_dir="user_settings"):
    sdir = Path(settings_dir)
    if not sdir.exists():
        raise FileNotFoundError(f"settings directory not found: {sdir}")

    files = sorted(
        [
            p
            for p in sdir.iterdir()
            if p.is_file() and p.suffix.lower() in {".yaml", ".yml", ".json"}
        ]
    )
    if not files:
        raise FileNotFoundError(f"no settings files found in: {sdir}")
    return files


def main():
    setup_logging()
    logger.info("Starting screen_arxiv")
    settings_files = iter_settings_files("user_settings")
    logger.info("Detected user settings files: %s", len(settings_files))

    for p in settings_files:
        logger.info("=== processing %s ===", p)
        run_for_settings_file(p)

    logger.info("Finished screening all users")


if __name__ == "__main__":
    main()
