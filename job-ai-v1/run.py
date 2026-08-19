import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

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

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
ROLE_TERMS = [
    "head of operations", "general manager operations", "operations manager",
    "fleet operations manager", "fleet manager", "fleet general manager",
    "transport operations manager", "transport manager", "mobility operations manager",
    "mobility manager", "driver operations manager", "dispatch manager",
]
LOCATIONS = ["dubai", "abu dhabi", "uae", "united arab emirates", "riyadh", "jeddah", "madinah", "medina", "makkah", "mecca", "saudi arabia", "ksa"]
NEGATIVE = ["salary", "definition", "course", "training", "intern", "junior", "driver job", "warehouse associate", "software engineer", "mechanical engineer"]


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url)
        query = ""
        # Keep Indeed job identifier only; drop tracking parameters elsewhere.
        if "indeed." in p.netloc.lower():
            q = dict(parse_qsl(p.query))
            if q.get("jk"):
                query = urlencode({"jk": q["jk"]})
        return urlunparse((p.scheme or "https", p.netloc.lower(), p.path.rstrip("/"), "", query, ""))
    except Exception:
        return url


def specific_job_url(url: str) -> bool:
    u = url.lower()
    patterns = [
        r"linkedin\.com/jobs/view/[^/?]+", r"linkedin\.com/jobs/view/\d+",
        r"gulftalent\.com/.+/jobs/.+-\d+$", r"naukrigulf\.com/.+-jid-",
        r"indeed\.[^/]+/viewjob", r"bayt\.com/.+/jobs/.+-\d+/",
        r"careers\.[^/]+/.+", r"jobs\.[^/]+/.+", r"workdayjobs\.com/.+",
        r"myworkdayjobs\.com/.+", r"smartrecruiters\.com/.+", r"greenhouse\.io/.+",
    ]
    return any(re.search(p, u) for p in patterns)


def search_web(query: str, max_results: int = 10):
    out = []
    with DDGS(timeout=20) as ddgs:
        for r in ddgs.text(query, max_results=max_results, safesearch="off") or []:
            out.append({"title": r.get("title", ""), "url": r.get("href", ""), "summary": r.get("body", ""), "query": query})
    return out


def extract_jobposting(soup: BeautifulSoup):
    found = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text(strip=True)
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        stack = obj if isinstance(obj, list) else [obj]
        for x in stack:
            if isinstance(x, dict) and x.get("@graph") and isinstance(x["@graph"], list):
                stack.extend(x["@graph"])
            if isinstance(x, dict) and str(x.get("@type", "")).lower() == "jobposting":
                found.append(x)
    return found[0] if found else {}


def enrich(item: dict) -> dict:
    url = item.get("url", "")
    if not url:
        return item
    # LinkedIn often blocks automated requests; search snippet is still useful.
    if "linkedin.com" in url.lower():
        item["resolved_url"] = normalize_url(url)
        return item
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        item["http_status"] = r.status_code
        item["resolved_url"] = normalize_url(r.url)
        if r.status_code >= 400 or "text/html" not in r.headers.get("content-type", ""):
            return item
        soup = BeautifulSoup(r.text[:700000], "html.parser")
        item["page_title"] = (soup.title.get_text(" ", strip=True) if soup.title else "")[:240]
        desc = ""
        for attrs in ({"name": "description"}, {"property": "og:description"}):
            m = soup.find("meta", attrs=attrs)
            if m and m.get("content"):
                desc = m.get("content", "")[:900]
                break
        item["page_description"] = desc
        job = extract_jobposting(soup)
        if job:
            hiring = job.get("hiringOrganization") or {}
            loc = job.get("jobLocation") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
            item["structured_job"] = {
                "title": job.get("title", ""),
                "company": hiring.get("name", "") if isinstance(hiring, dict) else "",
                "datePosted": job.get("datePosted", ""),
                "validThrough": job.get("validThrough", ""),
                "location": ", ".join([str(addr.get("addressLocality", "")), str(addr.get("addressCountry", ""))]).strip(", "),
                "description": BeautifulSoup(str(job.get("description", "")), "html.parser").get_text(" ", strip=True)[:2500],
            }
        else:
            text = " ".join(soup.stripped_strings)
            item["page_excerpt"] = re.sub(r"\s+", " ", text)[:2600]
    except Exception as e:
        item["enrich_warning"] = str(e)[:180]
    return item


def text_blob(item):
    sj = item.get("structured_job") or {}
    return " ".join([
        item.get("title", ""), item.get("summary", ""), item.get("page_title", ""),
        item.get("page_description", ""), item.get("page_excerpt", ""), sj.get("title", ""),
        sj.get("company", ""), sj.get("location", ""), sj.get("description", ""), item.get("url", "")
    ]).lower()


def evidence_score(item):
    t = text_blob(item)
    score = 0
    if specific_job_url(item.get("resolved_url") or item.get("url", "")):
        score += 24
    if item.get("structured_job"):
        score += 30
    if any(role in t for role in ROLE_TERMS):
        score += 20
    if any(loc in t for loc in LOCATIONS):
        score += 10
    if any(x in t for x in ["hiring", "vacancy", "apply", "job summary", "responsibilities", "dateposted"]):
        score += 8
    if any(x in t for x in NEGATIVE):
        score -= 35
    return score


# V1.2: search explicitly for direct job-detail URLs instead of broad category pages.
queries = []
role_groups = [
    "Fleet Operations Manager", "Fleet Manager", "Transport Operations Manager",
    "Head of Operations", "Operations Manager", "General Manager Operations",
]
market_groups = ["Dubai UAE", "Abu Dhabi UAE", "Riyadh Saudi Arabia", "Jeddah Saudi Arabia", "Madinah Saudi Arabia", "Makkah Saudi Arabia"]
for role in role_groups:
    for market in market_groups:
        queries.append(f'"{role}" "{market}" (hiring OR vacancy OR apply)')
for role in role_groups:
    queries.extend([
        f'site:linkedin.com/jobs/view "{role}" (UAE OR Saudi Arabia)',
        f'site:gulftalent.com "{role}" (UAE OR Saudi Arabia) "Apply Now"',
        f'site:naukrigulf.com "{role}" (UAE OR Saudi Arabia) jid',
    ])
queries = queries[:54]

seen, results, errors = set(), [], []
for q in queries:
    try:
        for item in search_web(q, 8):
            item["url"] = normalize_url(item.get("url", ""))
            key = item["url"] or item.get("title", "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            blob = f"{item.get('title','')} {item.get('summary','')} {item.get('url','')}".lower()
            if not any(role in blob for role in ROLE_TERMS):
                continue
            if any(x in blob for x in NEGATIVE):
                continue
            # Category pages are retained only briefly; direct detail URLs get priority.
            item["direct_url"] = specific_job_url(item["url"])
            results.append(item)
        time.sleep(0.12)
    except Exception as e:
        errors.append({"query": q, "error": str(e)[:200]})

# Direct job URLs first, then strongest specific-looking snippets.
results.sort(key=lambda x: (x.get("direct_url", False), "hiring" in x.get("title", "").lower(), "apply" in x.get("summary", "").lower()), reverse=True)
results = results[:40]
for i in range(min(28, len(results))):
    results[i] = enrich(results[i])
for item in results:
    item["evidence_score"] = evidence_score(item)

candidates = []
seen2 = set()
for item in sorted(results, key=lambda x: x.get("evidence_score", 0), reverse=True):
    u = item.get("resolved_url") or item.get("url", "")
    k = normalize_url(u)
    if not k or k in seen2:
        continue
    if item.get("evidence_score", 0) < 35:
        continue
    # Require direct URL OR structured JobPosting OR strong vacancy evidence.
    if not (specific_job_url(k) or item.get("structured_job") or item.get("evidence_score", 0) >= 55):
        continue
    seen2.add(k)
    candidates.append({
        "title": item.get("title", ""), "url": k, "summary": item.get("summary", ""),
        "query": item.get("query", ""), "evidence_score": item.get("evidence_score", 0),
        "page_title": item.get("page_title", ""), "page_description": item.get("page_description", ""),
        "page_excerpt": item.get("page_excerpt", ""), "structured_job": item.get("structured_job", {}),
    })
candidates = candidates[:22]

(OUTPUT_DIR / "search_debug.json").write_text(json.dumps({
    "version": "1.2", "generated_at": datetime.now(timezone.utc).isoformat(),
    "queries_attempted": len(queries), "raw_results": len(results), "direct_candidates": len(candidates),
    "errors": errors, "results": candidates,
}, indent=2, ensure_ascii=False), encoding="utf-8")

prompt = f"""
You are a conservative executive recruitment analyst.
TODAY: {datetime.now(timezone.utc).date().isoformat()}
CANDIDATE: {json.dumps(candidate, indent=2)}
SCORING WEIGHTS: {json.dumps(weights, indent=2)}
DIRECT VACANCY CANDIDATES: {json.dumps(candidates, indent=2)}

Rules:
- Use ONLY supplied candidates. Never invent employers, vacancies, URLs, dates, requirements or achievements.
- A candidate can be accepted when it is a specific vacancy URL and its title/location clearly fit, even if the full page is blocked.
- Exclude obvious category/search pages, stale/expired-looking roles, unrelated industries and junior roles.
- Score candidate fit, not page quality. Use the supplied scoring weights. 80+ = strong, 70-79 = credible, 60-69 = watchlist.
- Return up to 10 jobs scoring 60+, but mark fit_tier as strong/credible/watchlist. Do not pad with junk.
- Tailored outreach must use verified candidate facts only: 40+ drivers, 30+ vehicles, 10+ years, 9+ Dubai transport years, listed systems/strengths.
- job_url/source_url must exactly match a supplied candidate URL.

Return JSON only:
{{"generated_at":"ISO","summary":{{"searched_markets":[],"credible_jobs_found":0,"high_fit_count":0,"watchlist_count":0,"notes":""}},"jobs":[{{"rank":1,"job_title":"","company":"","location":"","fit_score":0,"fit_tier":"strong|credible|watchlist","live_confidence":"high|medium|low","job_url":"","source":"","source_url":"","why_fit":[""],"gaps_risks":[""],"score_breakdown":{{"operations_leadership":0,"fleet_transport":0,"team_scale":0,"commercial_finance_controls":0,"compliance":0,"systems_data":0,"gcc_relevance":0}},"email_subject":"","email_pitch":"","linkedin_pitch":""}}]}}
"""

response = client.models.generate_content(model="gemini-3.1-flash-lite", contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json"))
text = (response.text or "").strip()
if text.startswith("```"):
    text = text.split("\n", 1)[1]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
if text.lower().startswith("json\n"):
    text = text[5:].strip()
data = json.loads(text)

valid_urls = {c["url"] for c in candidates}
clean_jobs = []
for j in data.get("jobs", []):
    try:
        j["fit_score"] = max(0, min(100, int(j.get("fit_score", 0))))
    except Exception:
        j["fit_score"] = 0
    if j["fit_score"] < 60 or j.get("job_url") not in valid_urls:
        continue
    j["fit_tier"] = "strong" if j["fit_score"] >= 80 else ("credible" if j["fit_score"] >= 70 else "watchlist")
    clean_jobs.append(j)
clean_jobs.sort(key=lambda x: x["fit_score"], reverse=True)
clean_jobs = clean_jobs[:10]
for i, j in enumerate(clean_jobs, 1): j["rank"] = i
data["jobs"] = clean_jobs
summary = data.setdefault("summary", {})
summary["credible_jobs_found"] = sum(1 for j in clean_jobs if j["fit_score"] >= 70)
summary["high_fit_count"] = sum(1 for j in clean_jobs if j["fit_score"] >= 80)
summary["watchlist_count"] = sum(1 for j in clean_jobs if 60 <= j["fit_score"] < 70)
summary["discovery_candidates_reviewed"] = len(candidates)
summary.setdefault("searched_markets", ["UAE", "Saudi Arabia"])
data["generated_at"] = data.get("generated_at") or datetime.now(timezone.utc).isoformat()

(OUTPUT_DIR / "latest.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
md = ["# Sohail Qureshi — AI Job Match Report V1.2", "", f"Generated: {data['generated_at']}", "", f"**Direct candidates reviewed:** {len(candidates)}  ", f"**Credible jobs:** {summary['credible_jobs_found']}  ", f"**Strong matches:** {summary['high_fit_count']}  ", f"**Watchlist:** {summary['watchlist_count']}  ", ""]
if summary.get("notes"): md += [summary["notes"], ""]
for j in clean_jobs:
    md += [f"## {j['rank']}. {j.get('job_title','')} — {j.get('company','')} ({j['fit_score']}/100 · {j['fit_tier']})", f"**Location:** {j.get('location','')}  ", f"**Live confidence:** {j.get('live_confidence','')}  ", f"**Apply:** {j.get('job_url','')}  ", "", "**Why it fits"]
    md += [f"- {x}" for x in j.get("why_fit", [])]
    md += ["", "**Gaps / risks"] + [f"- {x}" for x in j.get("gaps_risks", [])]
    md += ["", f"**Email subject:** {j.get('email_subject','')}", "", j.get("email_pitch", ""), "", "**LinkedIn pitch**", j.get("linkedin_pitch", ""), "", "---", ""]
(OUTPUT_DIR / "latest.md").write_text("\n".join(md), encoding="utf-8")
print(f"V1.2 reviewed {len(candidates)} direct vacancy candidates and produced {len(clean_jobs)} ranked jobs")
