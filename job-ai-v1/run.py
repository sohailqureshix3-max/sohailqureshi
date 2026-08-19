import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent
PROFILE = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

candidate = PROFILE["candidate"]
search = PROFILE["search"]
weights = PROFILE["scoring_weights"]
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
ROLES = [
    "Fleet Operations Manager", "Fleet Manager", "Transport Operations Manager",
    "Head of Operations", "Operations Manager", "General Manager Operations",
    "Mobility Operations Manager", "Dispatch Manager",
]
MARKETS = [
    "United Arab Emirates", "Dubai", "Abu Dhabi",
    "Saudi Arabia", "Riyadh", "Jeddah", "Madinah", "Makkah",
]
ROLE_WORDS = [
    "fleet", "transport", "mobility", "operations", "dispatch", "chauffeur", "limousine",
]
NEGATIVE = [
    "intern", "junior", "software engineer", "mechanical engineer", "civil engineer",
    "warehouse associate", "sales executive", "delivery rider", "driver vacancy",
]


def clean_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme or "https", p.netloc.lower(), p.path.rstrip("/"), "", "", ""))
    except Exception:
        return url


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)).strip()


def pre_score(title: str, description: str, location: str) -> int:
    blob = f"{title} {description} {location}".lower()
    score = 0
    if any(r.lower() in blob for r in ROLES): score += 35
    if any(w in blob for w in ROLE_WORDS): score += 18
    if any(m.lower() in blob for m in MARKETS): score += 15
    if any(x in blob for x in ["manager", "head", "general manager"]): score += 10
    if any(x in blob for x in ["fleet", "vehicle", "driver", "transport"]): score += 12
    if any(x in blob for x in NEGATIVE): score -= 50
    return score


def linkedin_guest_search(role: str, location: str, pages: int = 2):
    out = []
    for page in range(pages):
        start = page * 25
        url = (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            f"?keywords={quote(role)}&location={quote(location)}&start={start}&sortBy=DD"
        )
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.select("li"):
                a = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
                if not a:
                    continue
                href = clean_url(a.get("href", ""))
                title_el = card.select_one("h3")
                company_el = card.select_one("h4") or card.select_one(".base-search-card__subtitle")
                loc_el = card.select_one(".job-search-card__location")
                time_el = card.select_one("time")
                title = strip_html(title_el.get_text(" ") if title_el else a.get_text(" "))
                company = strip_html(company_el.get_text(" ") if company_el else "")
                loc = strip_html(loc_el.get_text(" ") if loc_el else location)
                posted = time_el.get("datetime", "") if time_el else ""
                if href and title:
                    out.append({
                        "title": title, "company": company, "location": loc,
                        "url": href, "posted": posted, "source": "LinkedIn",
                        "description": "", "discovery": "linkedin_guest",
                    })
        except Exception:
            pass
        time.sleep(0.2)
    return out


def linkedin_detail(item: dict) -> dict:
    m = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)$", item.get("url", ""))
    if not m:
        return item
    try:
        r = requests.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}", headers=HEADERS, timeout=18)
        if r.status_code != 200:
            return item
        soup = BeautifulSoup(r.text, "html.parser")
        desc = soup.select_one(".show-more-less-html__markup")
        if desc:
            item["description"] = strip_html(desc.get_text(" "))[:5000]
        title = soup.select_one("h2.top-card-layout__title")
        company = soup.select_one("a.topcard__org-name-link") or soup.select_one(".topcard__flavor")
        location = soup.select_one(".topcard__flavor--bullet")
        if title: item["title"] = strip_html(title.get_text(" "))
        if company: item["company"] = strip_html(company.get_text(" "))
        if location: item["location"] = strip_html(location.get_text(" "))
    except Exception:
        pass
    return item


def ddg_fallback():
    out = []
    queries = [
        '"Fleet Manager" (Dubai OR UAE OR Riyadh OR Jeddah) (hiring OR vacancy)',
        '"Transport Operations Manager" (Dubai OR Abu Dhabi OR Riyadh OR Jeddah) jobs',
        '"Head of Operations" (transport OR fleet OR mobility) (UAE OR Saudi Arabia) jobs',
        'site:linkedin.com/jobs/view (fleet OR transport) manager (Dubai OR Riyadh OR Jeddah)',
        'site:naukrigulf.com transport operations manager UAE Saudi jid',
        'site:gulftalent.com fleet manager UAE Saudi "Apply Now"',
    ]
    try:
        with DDGS(timeout=20) as ddgs:
            for q in queries:
                for r in ddgs.text(q, max_results=10, safesearch="off") or []:
                    title, url, body = r.get("title", ""), clean_url(r.get("href", "")), r.get("body", "")
                    blob = f"{title} {body}".lower()
                    if url and any(w in blob for w in ROLE_WORDS) and not any(x in blob for x in NEGATIVE):
                        out.append({
                            "title": title, "company": "", "location": "",
                            "url": url, "posted": "", "source": "Web Search",
                            "description": body, "discovery": "ddg_fallback",
                        })
    except Exception:
        pass
    return out


# 1) Direct vacancy ingestion. LinkedIn guest job cards are primary because they expose specific job URLs.
raw = []
for role in ROLES[:7]:
    for market in ["United Arab Emirates", "Saudi Arabia", "Dubai", "Riyadh", "Jeddah"]:
        raw.extend(linkedin_guest_search(role, market, pages=1))

# 2) Fallback search only if direct ingestion is thin.
if len(raw) < 15:
    raw.extend(ddg_fallback())

# 3) Deduplicate and deterministic relevance filter.
seen = set()
unique = []
for item in raw:
    u = clean_url(item.get("url", ""))
    if not u or u in seen:
        continue
    seen.add(u)
    item["url"] = u
    item["pre_score"] = pre_score(item.get("title", ""), item.get("description", ""), item.get("location", ""))
    if item["pre_score"] >= 25:
        unique.append(item)

unique.sort(key=lambda x: (x.get("pre_score", 0), bool(x.get("posted"))), reverse=True)
unique = unique[:35]

# 4) Enrich strongest LinkedIn jobs with full description where public guest endpoint allows it.
for i in range(min(20, len(unique))):
    if "linkedin.com/jobs/view" in unique[i].get("url", ""):
        unique[i] = linkedin_detail(unique[i])
        unique[i]["pre_score"] = pre_score(unique[i].get("title", ""), unique[i].get("description", ""), unique[i].get("location", ""))

(OUTPUT_DIR / "search_debug.json").write_text(json.dumps({
    "version": "FINAL",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "raw_discovered": len(raw),
    "direct_candidates": len(unique),
    "results": unique,
}, indent=2, ensure_ascii=False), encoding="utf-8")

# 5) AI scoring and pitch generation. AI never discovers jobs; it only evaluates supplied URLs.
prompt = f"""
You are a conservative GCC executive recruitment analyst.
TODAY: {datetime.now(timezone.utc).date().isoformat()}

CANDIDATE PROFILE:
{json.dumps(candidate, indent=2)}

SCORING WEIGHTS:
{json.dumps(weights, indent=2)}

VERIFIED/DIRECT JOB CANDIDATES:
{json.dumps(unique, indent=2)}

Rules:
1. Use ONLY supplied jobs. Never invent a vacancy, employer, URL, requirement or achievement.
2. Prioritize senior Operations, Fleet, Transport, Dispatch and Mobility roles in UAE/KSA.
3. Score candidate fit 0-100 using the supplied weights. 80+ strong, 70-79 credible, 60-69 watchlist.
4. Reject obvious technical engineering, warehouse-only, junior, pure sales or unrelated HR/global-mobility roles.
5. A direct LinkedIn vacancy may be accepted even if its full description was not accessible, but lower live_confidence if evidence is thin.
6. Tailored outreach can use only verified candidate facts: 10+ years experience, 9+ years Dubai transport, 40+ drivers, 30+ vehicles, listed platforms, reconciliation, payroll, RTA compliance, SOPs, Excel/Power Query, ACCA Part-Qualified.
7. job_url must exactly equal a supplied URL.
8. Return up to 10 jobs scoring 60+. Never pad with junk.

Return JSON only:
{{"generated_at":"ISO","summary":{{"searched_markets":[],"credible_jobs_found":0,"high_fit_count":0,"watchlist_count":0,"notes":""}},"jobs":[{{"rank":1,"job_title":"","company":"","location":"","fit_score":0,"fit_tier":"strong|credible|watchlist","live_confidence":"high|medium|low","job_url":"","source":"","why_fit":[""],"gaps_risks":[""],"email_subject":"","email_pitch":"","linkedin_pitch":""}}]}}
"""

try:
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    text = (response.text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"): text = text[:-3]
    if text.lower().startswith("json\n"): text = text[5:]
    data = json.loads(text.strip())
except Exception as e:
    data = {"generated_at": datetime.now(timezone.utc).isoformat(), "summary": {"notes": f"AI scoring unavailable: {type(e).__name__}"}, "jobs": []}

valid = {x["url"]: x for x in unique}
clean_jobs = []
for j in data.get("jobs", []):
    try:
        score = max(0, min(100, int(j.get("fit_score", 0))))
    except Exception:
        score = 0
    url = clean_url(j.get("job_url", ""))
    if score < 60 or url not in valid:
        continue
    j["job_url"] = url
    j["fit_score"] = score
    j["fit_tier"] = "strong" if score >= 80 else ("credible" if score >= 70 else "watchlist")
    j["source"] = valid[url].get("source", j.get("source", ""))
    clean_jobs.append(j)

# Deterministic safety fallback: never lose strong direct candidates because Gemini formatting fails.
if not clean_jobs and unique:
    for item in unique[:8]:
        ps = item.get("pre_score", 0)
        score = min(79, max(60, 55 + ps // 3))
        if score < 60:
            continue
        clean_jobs.append({
            "rank": 0,
            "job_title": item.get("title", ""),
            "company": item.get("company", ""),
            "location": item.get("location", ""),
            "fit_score": score,
            "fit_tier": "credible" if score >= 70 else "watchlist",
            "live_confidence": "medium" if item.get("posted") else "low",
            "job_url": item.get("url", ""),
            "source": item.get("source", ""),
            "why_fit": ["Role title and market align with fleet/transport operations leadership."],
            "gaps_risks": ["AI scoring/pitch was unavailable; review the vacancy manually before applying."],
            "email_subject": "",
            "email_pitch": "",
            "linkedin_pitch": "",
        })

clean_jobs.sort(key=lambda x: x.get("fit_score", 0), reverse=True)
clean_jobs = clean_jobs[:10]
for i, j in enumerate(clean_jobs, 1): j["rank"] = i

summary = data.setdefault("summary", {})
summary["credible_jobs_found"] = sum(1 for j in clean_jobs if j["fit_score"] >= 70)
summary["high_fit_count"] = sum(1 for j in clean_jobs if j["fit_score"] >= 80)
summary["watchlist_count"] = sum(1 for j in clean_jobs if 60 <= j["fit_score"] < 70)
summary["discovery_candidates_reviewed"] = len(unique)
summary.setdefault("searched_markets", ["UAE", "Saudi Arabia"])
summary["engine"] = "LinkedIn direct vacancies + web fallback + Gemini scoring"
data["jobs"] = clean_jobs
data["generated_at"] = data.get("generated_at") or datetime.now(timezone.utc).isoformat()

(OUTPUT_DIR / "latest.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

md = [
    "# Sohail Qureshi — AI Job Match Report FINAL", "",
    f"Generated: {data['generated_at']}", "",
    f"**Direct candidates reviewed:** {len(unique)}  ",
    f"**Credible jobs:** {summary['credible_jobs_found']}  ",
    f"**Strong matches:** {summary['high_fit_count']}  ",
    f"**Watchlist:** {summary['watchlist_count']}  ", "",
]
if summary.get("notes"): md += [str(summary["notes"]), ""]
for j in clean_jobs:
    md += [
        f"## {j['rank']}. {j.get('job_title','')} — {j.get('company','')} ({j.get('fit_score',0)}/100)",
        f"**Tier:** {j.get('fit_tier','')}  ",
        f"**Location:** {j.get('location','')}  ",
        f"**Live confidence:** {j.get('live_confidence','')}  ",
        f"**Vacancy:** {j.get('job_url','')}", "", "**Why it fits**",
    ] + [f"- {x}" for x in j.get("why_fit", [])] + ["", "**Gaps / risks"] + [f"- {x}" for x in j.get("gaps_risks", [])]
    if j.get("email_subject") or j.get("email_pitch"):
        md += ["", f"**Email subject:** {j.get('email_subject','')}", "", j.get("email_pitch", ""), "", "**LinkedIn pitch**", j.get("linkedin_pitch", "")]
    md += ["", "---", ""]
(OUTPUT_DIR / "latest.md").write_text("\n".join(md), encoding="utf-8")
print(f"FINAL: discovered {len(unique)} direct candidates; published {len(clean_jobs)} scored roles")