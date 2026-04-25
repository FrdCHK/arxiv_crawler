import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
import xml.etree.ElementTree as ET

import requests
import smtplib
import yaml
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


DEFAULT_SETTINGS = {
    "arxiv": {
        "api_url": "http://export.arxiv.org/api/query",
        "category": "astro-ph*",
        "max_results": 2000,
        "user_agent": "arxiv crawler (research project; contact: your_email@example.com)",
        "recent_days": 7,
    },
    "interest_file": "interst.txt",
    "llm": {
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "local-model",
        "timeout_sec": 120,
        "batch_size": 1,
        "temperature": 0.0,
        "max_tokens": 5000,
        "log_raw_response": False,
        "raw_response_log_file": "llm_raw_output.log",
    },
    "selection": {
        "threshold": 50,
    },
    "output": {
        "save_html": True,
        "html_file": "arxiv_selected.html",
        "send_email": False,
    },
}


def deep_update(base, updates):
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_settings(path="settings.yaml"):
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    settings_path = Path(path)
    if settings_path.exists():
        with settings_path.open("r", encoding="utf-8") as f:
            user_settings = yaml.safe_load(f) or {}
        deep_update(settings, user_settings)
    return settings


def load_interest(path):
    p = Path(path)
    if not p.exists() and p.name == "interst.txt":
        fallback = p.with_name("interest.txt")
        if fallback.exists():
            p = fallback
    if not p.exists():
        raise FileNotFoundError(f"interest file not found: {path}")
    return p.read_text(encoding="utf-8").strip()


def clean_text(raw):
    return " ".join(raw.replace("\n", " ").split())


def format_arxiv_api_date(dt):
    return dt.strftime("%Y%m%d%H%M")


def parse_arxiv_recent(arxiv_cfg):
    end_date = datetime.utcnow()
    recent_days = max(int(arxiv_cfg.get("recent_days", 7)), 1)
    start_date = end_date - timedelta(days=recent_days)

    query = (
        f'cat:{arxiv_cfg.get("category", "astro-ph*")} '
        f'AND submittedDate:[{format_arxiv_api_date(start_date)} TO {format_arxiv_api_date(end_date)}]'
    )
    params = {
        "search_query": query,
        "start": 0,
        "max_results": int(arxiv_cfg.get("max_results", 2000)),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    headers = {"User-Agent": arxiv_cfg.get("user_agent", DEFAULT_SETTINGS["arxiv"]["user_agent"])}
    api_url = arxiv_cfg.get("api_url", DEFAULT_SETTINGS["arxiv"]["api_url"])

    resp = requests.get(api_url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    rows = []
    for entry in root.findall("atom:entry", ns):
        paper_url = entry.findtext("atom:id", default="", namespaces=ns).strip()
        paper_id = paper_url.split("/")[-1]
        title = clean_text(entry.findtext("atom:title", default="", namespaces=ns))
        abstract = clean_text(entry.findtext("atom:summary", default="", namespaces=ns))
        author_split = [
            clean_text(author.findtext("atom:name", default="", namespaces=ns))
            for author in entry.findall("atom:author", ns)
            if author.findtext("atom:name", default="", namespaces=ns).strip()
        ]
        subject_split = []
        for category in entry.findall("atom:category", ns):
            term = (category.attrib.get("term") or "").strip()
            if term and term not in subject_split:
                subject_split.append(term)
        published_text = entry.findtext("atom:published", default="", namespaces=ns).strip()
        if published_text:
            date_dt = datetime.strptime(published_text, "%Y-%m-%dT%H:%M:%SZ")
        else:
            date_dt = end_date

        rows.append(
            {
                "date": date_dt.strftime("%a, %d %b %Y"),
                "datetime": date_dt,
                "id": paper_id,
                "title": title,
                "abstract": abstract,
                "authors": ", ".join(author_split),
                "author_split": author_split,
                "subjects": "; ".join(subject_split),
                "subject_split": subject_split,
            }
        )
    print(f"get paper success by API, query='{query}', count={len(rows)}")
    return rows


def filter_papers_by_recent_days(papers, recent_days):
    if not papers:
        return papers
    if recent_days is None:
        return papers
    recent_days = int(recent_days)
    if recent_days <= 0:
        return papers

    latest_dt = max(p["datetime"] for p in papers)
    cutoff = latest_dt - timedelta(days=recent_days - 1)
    filtered = [p for p in papers if p["datetime"] >= cutoff]
    print(
        f"time filter success, recent_days={recent_days}, "
        f"latest={latest_dt.strftime('%Y-%m-%d')}, kept={len(filtered)}/{len(papers)}"
    )
    return filtered


def extract_json(text):
    if not text:
        return []

    clean = text.strip()
    clean = re.sub(r"<think>.*?</think>", "", clean, flags=re.DOTALL | re.IGNORECASE).strip()
    clean = re.sub(r"^```(?:json)?", "", clean).strip()
    clean = re.sub(r"```$", "", clean).strip()

    # 1) Try raw content directly
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

    # 2) Try extracting JSON array block
    array_match = re.search(r"\[\s*{.*}\s*\]", clean, flags=re.DOTALL)
    if array_match:
        try:
            return json.loads(array_match.group(0))
        except json.JSONDecodeError:
            pass

    # 3) Try extracting any object and look for common list keys
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
            print(f"logged raw LLM output for batch starting at {start} -> {raw_log_file}")
        scored_list = extract_json(content)
        if not scored_list:
            print(f"warning: could not parse JSON for batch starting at {start}.")
            print("raw output:")
            print(content if content is not None else "")
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
        print(f"llm scored {min(start + batch_size, len(papers))}/{len(papers)} papers")
        time.sleep(0.1)

    merged = []
    for paper in papers:
        scored = dict(paper)
        match = score_map.get(paper["id"], {})
        scored["relevance_score"] = int(match.get("relevance_score", 0))
        scored["reason"] = str(match.get("reason", "")).strip()
        merged.append(scored)
    return merged


def build_html(selected_papers, threshold):
    msg = f"<h2>arXiv recent papers (AI-selected, threshold >= {threshold})</h2>"
    if not selected_papers:
        return msg + "<p>No papers passed the threshold.</p>"

    papers_gr = defaultdict(list)
    for item in selected_papers:
        # Group by calendar day rather than full timestamp.
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
    multi_part["Subject"] = Header("arXiv this week", "utf-8")

    smtp = smtplib.SMTP_SSL(host=sender["server"], port=sender["port"])
    smtp.login(sender["user"], sender["passwd"])
    smtp.sendmail(sender["user"], receiver, multi_part.as_string())
    smtp.quit()
    print("send email success")


def main():
    settings = load_settings("settings.yaml")
    interest_text = load_interest(settings["interest_file"])
    papers = parse_arxiv_recent(settings["arxiv"])
    papers = filter_papers_by_recent_days(papers, settings["arxiv"].get("recent_days", 7))

    scored_papers = score_papers_with_llm(papers, interest_text, settings)
    threshold = int(settings["selection"]["threshold"])
    selected_papers = [
        p for p in scored_papers if int(p.get("relevance_score", 0)) >= threshold
    ]
    selected_papers.sort(key=lambda p: (p["datetime"], p["relevance_score"]), reverse=True)
    print(f"selection success, selected={len(selected_papers)}")

    html_msg = build_html(selected_papers, threshold)

    output_cfg = settings["output"]
    if output_cfg.get("save_html", False):
        html_path = Path(output_cfg.get("html_file", "arxiv_selected.html"))
        html_path.write_text(html_msg, encoding="utf-8")
        print(f"saved html: {html_path}")

    if output_cfg.get("send_email", False):
        with open("account.json", "r", encoding="utf-8") as accf:
            acc = json.load(accf)
        try:
            send_email(acc["sender"], acc["receiver"], html_msg)
        except smtplib.SMTPException:
            print("error: email not sent!")

    print("finished!")


if __name__ == "__main__":
    main()
