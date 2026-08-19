import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
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

TARGET_DOMAINS = [
    "linkedin.com/jobs",
    "bayt.com",
    "gulftalent.com",
    "naukrigulf.com",
    "indeed.com",
    "glassdoor.com",
    "founditgulf.com",
    "careers.emiratesgroupcareers.com",
    "careers.etihad.com",
    "careers.accor.com",
    "careers.marriott.com",
    "careers.aramco.com",
]

ROLE_TERMS = [
    "operations manager", "head of operations", "general manager operations",
    "fleet manager", "fleet operations manager", "transport manager",
    "transport operations manager", "mobility manager", "mobility operations manager",
    "driver operations manager", "dispatch manager", "fleet general manager",
]

LOCATION_TERMS = [
    "Dubai", "Abu Dhabi", "UAE", "United Arab Emirates",
    "Riyadh", "Jeddah", "Madinah", "Medina", "Makkah", "Mecca", "Saudi Arabia", "KSA",
]

NEGATIVE_TERMS = [
    "warehouse associate", "sales executive", "software engineer", "mechanical engineer",
    "civil engineer", "intern", "junior", "driver job", "delivery rider", "definition",
    "salary guide", "course", "training", "what is", "meaning of",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
}


def clean_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", "", ""))
    except Exception:
        return url


def looks_job_like(title: str, body: str, url: str) -> bool:
    text = f"{title} {body} {url}".lower()
    if any(x in text for x in NEGATIVE_TERMS):
        return False
    role_hit = any(x in text for x in ROLE_TERMS)
    job_signal = any(x in text for x in ["job", "career", "vacancy", "hiring", "apply", "position", "opening"])
    location_hit = any(x.lower() in text for x in LOCATION_TERMS)
    return role_hit and (job_signal or location_hit)


def relevance_score(title: str, body: str, url: str) -> int:
    text = f"{title} {body} {url}".lower()
    score = 0
    score += 10 * sum(1 for x in ROLE_TERMS if x in text)
    score += 4 * sum(1 for x in LOCATION_TERMS if x.lower() in text)
    score += 3 * sum(1 for x in ["fleet", "transport", "mobility", "operations", "limousine", "chauffeur", "dispatch"] if x in text)
    if any(d in url.lower() for d in TARGET_DOMAINS):
        score += 12
    if any(x in text for x in ["apply now", "job id", "posted", "vacancy", "hiring"]):
        score += 8
    return score


def search_ddg(query: str, max_results: int = 10):
    out = []
    with DDGS(timeout=20) as ddgs:
        for r in ddgs.text(query, max_results=max_results, safesearch="off") or []:
            out.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "summary": r.get("body", ""),
                "query": query,
            })
    return out


def enrich_page(item: dict) -> dict:
    url = item.get("url", "")
    if not url or "linkedin.com" in url.lower():
        return item
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        if r.status_code >= 400 or "text/html" not in r.headers.get("content-type", ""):
            return item
        soup = BeautifulSoup(r.text[:600000], "html.parser")
        title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:220]
        desc = ""
        md = soup.find("meta", attrs={"name": re.compile("description", re.I)})
        if md and md.get("content"):
            desc = md.get("content", "")[:800]
        if not desc:
            og = soup.find("meta", attrs={"property": "og:description"})
            if og and og.get("content"):
                desc = og.get("content", "")[:800]
        text = " ".join(soup.stripped_strings)
        text = re.sub(r"\s+", " ", text)[:3500]
        item["page_title"] = title
        item["page_description"] = desc
        item["page_text"] = text
        item["resolved_url"] = clean_url(r.url)
    except Exception as e:
        item["enrich_warning"] = str(e)[:160]
    return item


# Search strategy: role/location combinations + targeted job-board queries.
queries = []
priority_roles = [
    "Head of Operations", "General Manager Operations", "Fleet Operations Manager",
    "Transport Operations Manager", "Mobility Operations Manager", "Fleet Manager",
]
priority_locations = ["Dubai", "Abu Dhabi", "Riyadh", "Jeddah", "Madinah", "Makkah"]

for role in priority_roles:
    for loc in priority_locations:
        queries.append(f'"{role}" "{loc}" jobs')

for role in priority_roles[:5]:
    queries += [
        f'site:linkedin.com/jobs/view "{role}" (Dubai OR UAE OR Riyadh OR Jeddah OR Saudi)',
        f'site:bayt.com "{role}" (UAE OR Saudi Arabia)',
        f'site:gulftalent.com "{role}" (UAE OR Saudi Arabia)',
        f'site:naukrigulf.com "{role}" (UAE OR Saudi Arabia)',
    ]

# Keep runtime reasonable on scheduled GitHub Actions.
queries = queries[:46]

seen = set()
raw_results = []
search_errors = []
for q in queries:
    try:
        for item in search_ddg(q, max_results=8):
            key = clean_url(item.get("url", "")) or item.get("title", "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            if looks_job_like(item.get("title", ""), item.get("summary", ""), item.get("url", "")):
                item["pre_score"] = relevance_score(item.get("title", ""), item.get("summary", ""), item.get("url", ""))
                raw_results.append(item)
        time.sleep(0.15)
    except Exception as e:
        search_errors.append({"query": q, "error": str(e)[:220]})

raw_results.sort(key=lambda x: x.get("pre_score", 0), reverse=True)
raw_results = raw_results[:45]

# Enrich the strongest results, while keeping search snippets as the fallback.
for i in range(min(24, len(raw_results))):
    raw_results[i] = enrich_page(raw_results[i])

# Final deterministic cleanup before AI scoring.
cleaned_results = []
seen_final = set()
for item in raw_results:
    final_url = item.get("resolved_url") or item.get("url", "")
    key = clean_url(final_url)
    if not key or key in seen_final:
        continue
    combined_title = item.get("page_title") or item.get("title", "")
    combined_body = " ".join([
        item.get("summary", ""), item.get("page_description", ""), item.get("page_text", "")[:1800]
    ])
    if not looks_job_like(combined_title, combined_body, final_url):
        continue
    seen_final.add(key)
    cleaned_results.append({
        "title": item.get("title", ""),
        "url": final_url,
        "summary": item.get("summary", ""),
        "page_title": item.get("page_title", ""),
        "page_description": item.get("page_description", ""),
        "page_excerpt": item.get("page_text", "")[:1800],
        "query": item.get("query", ""),
        "pre_score": item.get("pre_score", 0),
    })

cleaned_results = cleaned_results[:32]

(OUTPUT_DIR / "search_debug.json").write_text(json.dumps({
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "queries_attempted": len(queries),
    "raw_job_like_results": len(raw_results),
    "cleaned_results": len(cleaned_results),
    "search_errors": search_errors,
    "results": cleaned_results,
}, indent=2, ensure_ascii=False), encoding="utf-8")

prompt = f"""
You are an executive job-search analyst and outreach strategist.

TODAY: {datetime.now(timezone.utc).date().isoformat()}

CANDIDATE PROFILE:
{json.dumps(candidate, indent=2)}

TARGET SEARCH:
{json.dumps(search, indent=2)}

SCORING WEIGHTS (total 100):
{json.dumps(weights, indent=2)}

DISCOVERED JOB CANDIDATES:
{json.dumps(cleaned_results, indent=2)}

TASK:
1. Use ONLY the supplied discovered candidates. Never invent a vacancy, employer, location, URL, date, requirement or hiring status.
2. Identify specific job vacancies relevant to senior Operations, Fleet, Transport, Dispatch or Mobility leadership in UAE or Saudi Arabia.
3. Exclude category/search pages, generic career pages, articles, salary guides, expired-looking pages and clearly junior roles.
4. Prefer specific employer career pages and specific job-board vacancy pages.
5. Score each credible opportunity 0-100 using the supplied weights. Do not inflate scores.
6. A score of 80+ should mean a genuinely strong match. 70-79 means credible but with meaningful gaps. Below 70 should normally be excluded.
7. Maximum {search['max_jobs']} jobs. If fewer than 5 credible jobs exist, return fewer than 5 rather than padding the list.
8. For every job provide concise fit reasons and real gaps/risks.
9. Produce a tailored email pitch that references the role/company and only verified candidate facts.
10. Never invent revenue, savings, percentages, degrees, certifications, team sizes or achievements beyond the candidate profile.
11. Include a concise email subject and LinkedIn message (max 450 characters).
12. job_url and source_url must be exact URLs from the supplied candidates.
13. live_confidence = high only when the supplied evidence strongly looks like a current, specific vacancy; otherwise medium or low.
14. If there are no credible vacancies, return an empty jobs array and explain why in summary.notes.

Return ONLY valid JSON with exactly this structure:
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

# Enforce the configured minimum rather than letting the model pad weak results.
data["jobs"] = [j for j in data.get("jobs", []) if j.get("fit_score", 0) >= search["minimum_score"]]
data["jobs"] = sorted(data["jobs"], key=lambda j: j.get("fit_score", 0), reverse=True)[:search["max_jobs"]]
for i, job in enumerate(data["jobs"], 1):
    job["rank"] = i

summary = data.setdefault("summary", {})
summary["credible_jobs_found"] = len(data["jobs"])
summary["high_fit_count"] = sum(1 for j in data["jobs"] if j.get("fit_score", 0) >= 80)
summary.setdefault("searched_markets", ["UAE", "Saudi Arabia"])
summary["discovery_candidates_reviewed"] = len(cleaned_results)

data["generated_at"] = data.get("generated_at") or datetime.now(timezone.utc).isoformat()
(OUTPUT_DIR / "latest.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

md = [
    "# Sohail Qureshi — AI Job Match Report",
    "",
    f"Generated: {data.get('generated_at', '')}",
    "",
    f"**Discovery candidates reviewed:** {len(cleaned_results)}  ",
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
    md += [
        "", f"**Email subject:** {job.get('email_subject','')}",
        "", "**Tailored email pitch", job.get("email_pitch", ""),
        "", "**LinkedIn pitch", job.get("linkedin_pitch", ""),
        "", "---", "",
    ]

(OUTPUT_DIR / "latest.md").write_text("\n".join(md), encoding="utf-8")
print(f"V1.1: reviewed {len(cleaned_results)} candidates and generated {len(data['jobs'])} credible matches")
