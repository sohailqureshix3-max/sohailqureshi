import json
import os
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent
PROFILE = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
candidate = PROFILE["candidate"]
search = PROFILE["search"]
weights = PROFILE["scoring_weights"]


def bing_rss(query: str, limit: int = 10):
    url = "https://www.bing.com/search?format=rss&q=" + urllib.parse.quote(query)
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    feed = feedparser.parse(r.text)
    out = []
    for entry in feed.entries[:limit]:
        out.append({
            "title": getattr(entry, "title", ""),
            "url": getattr(entry, "link", ""),
            "summary": getattr(entry, "summary", ""),
        })
    return out

queries = []
for role in search["target_roles"][:6]:
    queries.append(f'"{role}" (Dubai OR UAE OR Riyadh OR Jeddah OR Saudi) jobs')
queries += [
    'fleet operations manager Dubai jobs transport',
    'head of operations mobility UAE jobs',
    'transport operations manager Saudi Arabia jobs',
    'fleet manager limousine chauffeur Dubai jobs',
]

seen = set()
raw_results = []
for q in queries:
    try:
        for item in bing_rss(q, limit=8):
            key = item.get("url") or item.get("title")
            if key and key not in seen:
                seen.add(key)
                item["query"] = q
                raw_results.append(item)
    except Exception as e:
        print(f"Search warning for {q}: {e}")

raw_results = raw_results[:50]

prompt = f"""
You are an executive job-search analyst and outreach strategist.

TODAY: {datetime.now(timezone.utc).date().isoformat()}

CANDIDATE PROFILE:
{json.dumps(candidate, indent=2)}

TARGET SEARCH:
{json.dumps(search, indent=2)}

SCORING WEIGHTS (total 100):
{json.dumps(weights, indent=2)}

WEB SEARCH RESULTS:
{json.dumps(raw_results, indent=2)}

TASK:
1. Use ONLY the supplied web search results. Do not invent jobs, companies, URLs, dates, requirements or hiring status.
2. Identify CURRENT-looking job opportunities relevant to Operations, Fleet, Transport or Mobility leadership in UAE or Saudi Arabia.
3. Favor employer career pages and reputable job boards. Reject obviously irrelevant results, articles, category pages and junior roles.
4. Score each credible opportunity 0-100 using the supplied weights. Do not inflate scores.
5. Only return jobs scoring at least {search['minimum_score']} unless fewer than 5 credible matches exist; then include the strongest lower-scoring roles and mark the risk clearly.
6. Maximum {search['max_jobs']} jobs.
7. For every job provide concise fit reasons, gaps/risks and a tailored pitch.
8. Use candidate facts only. Never invent revenue, savings, percentages, qualifications or achievements.
9. Include a concise email subject and LinkedIn message (max 450 characters).
10. job_url and source_url must come from the supplied search results only.
11. If a result does not clearly represent a specific open role, exclude it.
12. Be conservative about live status: use high only when the result strongly appears to be a specific active vacancy.

Return ONLY valid JSON with this top-level structure:
{{
  "generated_at": "ISO timestamp",
  "summary": {{"searched_markets": [], "credible_jobs_found": 0, "high_fit_count": 0, "notes": ""}},
  "jobs": [{{
    "rank": 1,
    "job_title": "",
    "company": "",
    "location": "",
    "fit_score": 0,
    "live_confidence": "high|medium|low",
    "job_url": "",
    "source": "",
    "source_url": "",
    "why_fit": [""],
    "gaps_risks": [""],
    "score_breakdown": {{
      "operations_leadership": 0,
      "fleet_transport": 0,
      "team_scale": 0,
      "commercial_finance_controls": 0,
      "compliance": 0,
      "systems_data": 0,
      "gcc_relevance": 0
    }},
    "email_subject": "",
    "email_pitch": "",
    "linkedin_pitch": ""
  }}]
}}
"""

response = client.models.generate_content(
    model="gemini-3.1-flash-lite",
    contents=prompt,
    config=types.GenerateContentConfig(response_mime_type="application/json"),
)

text = (response.text or "").strip()
if text.startswith("```"):
    text = text.split("\n", 1)[1]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
if text.lower().startswith("json\n"):
    text = text[5:].strip()

data = json.loads(text)
for job in data.get("jobs", []):
    try:
        job["fit_score"] = max(0, min(100, int(job.get("fit_score", 0))))
    except Exception:
        job["fit_score"] = 0

data["jobs"] = sorted(data.get("jobs", []), key=lambda j: j.get("fit_score", 0), reverse=True)
for i, job in enumerate(data["jobs"], 1):
    job["rank"] = i

(OUTPUT_DIR / "latest.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

md = ["# Sohail Qureshi — AI Job Match Report", "", f"Generated: {data.get('generated_at', '')}", ""]
summary = data.get("summary", {})
md += [
    f"**Credible jobs found:** {summary.get('credible_jobs_found', len(data['jobs']))}  ",
    f"**High-fit jobs:** {summary.get('high_fit_count', 0)}  ",
    f"**Markets:** {', '.join(summary.get('searched_markets', []))}",
    "",
]
if summary.get("notes"):
    md += [summary["notes"], ""]

for job in data["jobs"]:
    md += [
        f"## {job['rank']}. {job.get('job_title','')} — {job.get('company','')} ({job.get('fit_score',0)}/100)",
        f"**Location:** {job.get('location','')}  ",
        f"**Live confidence:** {job.get('live_confidence','')}  ",
    ]
    if job.get("job_url"):
        md.append(f"**Apply:** {job['job_url']}  ")
    if job.get("source_url"):
        md.append(f"**Source:** {job.get('source','')} — {job['source_url']}")
    md += ["", "**Why it fits"] + [f"- {x}" for x in job.get("why_fit", [])]
    md += ["", "**Gaps / risks"] + [f"- {x}" for x in job.get("gaps_risks", [])]
    md += ["", f"**Email subject:** {job.get('email_subject','')}", "", "**Tailored email pitch", job.get("email_pitch", ""), "", "**LinkedIn pitch", job.get("linkedin_pitch", ""), "", "---", ""]

(OUTPUT_DIR / "latest.md").write_text("\n".join(md), encoding="utf-8")
print(f"Discovered {len(raw_results)} web results and generated {len(data['jobs'])} scored opportunities")
