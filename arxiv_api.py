import requests
import xml.etree.ElementTree as ET
import json
from datetime import datetime, timedelta

# ----------------------------
# 1. 时间范围：过去7天
# ----------------------------
end_date = datetime.utcnow()
start_date = end_date - timedelta(days=7)

def format_arxiv_date(dt):
    return dt.strftime("%Y%m%d%H%M")

start_str = format_arxiv_date(start_date)
end_str = format_arxiv_date(end_date)

# ----------------------------
# 2. API 参数
# ----------------------------
base_url = "http://export.arxiv.org/api/query"

query = f"cat:astro-ph* AND submittedDate:[{start_str} TO {end_str}]"

params = {
    "search_query": query,
    "start": 0,
    "max_results": 2000,   # astro-ph 7天一般够用，可调
    "sortBy": "submittedDate",
    "sortOrder": "descending"
}

headers = {
    "User-Agent": "astro-ph scraper (research project; contact: your_email@example.com)"
}

# ----------------------------
# 3. 请求 API
# ----------------------------
response = requests.get(base_url, params=params, headers=headers)
response.raise_for_status()

# ----------------------------
# 4. 解析 XML
# ----------------------------
root = ET.fromstring(response.text)

ns = {
    "atom": "http://www.w3.org/2005/Atom"
}

results = []

for entry in root.findall("atom:entry", ns):
    arxiv_id = entry.find("atom:id", ns).text.split("/")[-1]

    title = entry.find("atom:title", ns).text.strip()
    summary = entry.find("atom:summary", ns).text.strip()

    authors = [
        author.find("atom:name", ns).text
        for author in entry.findall("atom:author", ns)
    ]

    results.append({
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "summary": summary
    })

# ----------------------------
# 5. 保存 JSON
# ----------------------------
output_file = "astro_ph_last7days.json"

with open(output_file, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print(f"Saved {len(results)} papers to {output_file}")
