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

# Entire search strategy is now profile-driven. No UAE/KSA-only hard coding.
ROLES = list(dict.fromkeys(search.get("target_roles", [])))
PROFILE_LOCATIONS = list(dict.fromkeys(search.get("locations", [])))
MARKET_PRIORITY = list(dict.fromkeys(search.get("market_priority", [])))
MAX_JOBS = int(search.get("max_jobs", 15))

# Country-level searches cover the entire GCC while keeping the workflow fast.
GCC_COUNTRIES = [
    "Saudi Arabia", "United Arab Emirates", "Kuwait", "Qatar", "Bahrain", "Oman"
]
COUNTRY_MARKETS = [m for m in MARKET_PRIORITY if m in GCC_COUNTRIES]
for country in GCC_COUNTRIES:
    if country not in COUNTRY_MARKETS:
        COUNTRY_MARKETS.append(country)

ROLE_WORDS = [
    "operations", "fleet", "transport", "transportation", "mobility", "logistics",
    "dispatch", "last mile", "driver operations", "business unit", "country operations",
    "regional operations", "general manager", "head of operations", "director of operations",
]
NEGATIVE = [
    "intern", "internship", "junior", "software engineer", "mechanical engineer",
    "civil engineer", "warehouse associate", "sales executive", "delivery rider",
    "driver vacancy", "hr manager", "accountant", "medical", "nurse", "restaurant manager",
]
EXECUTIVE_TERMS = [
    "head of operations", "general manager", "regional operations", "country operations",
    "director of operations", "business unit manager", "senior operations manager",
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
    title_l = (title or "").lower()
    score = 0
    if any(r.lower() in title_l for r in ROLES):
        score += 32
    elif any(w in title_l for w in ROLE_WORDS):
        score += 22
    if any(w in blob for w in ROLE_WORDS):
        score += 14
    if any(m.lower() in blob for m in GCC_COUNTRIES + PROFILE_LOCATIONS):
        score += 12
    if any(x in title_l for x in EXECUTIVE_TERMS):
        score += 18  # career progression boost
    elif any(x in title_l for x in ["manager", "lead"]):
        score += 8
    if any(x in blob for x in ["fleet", "vehicle", "driver", "transport", "mobility", "logistics"]):
        score += 10
    if any(x in blob for x in ["budget", "p&l", "kpi", "cost control", "revenue", "compliance", "sop"]):
        score += 7
    if any(x in blob for x in NEGATIVE):
        score -= 60
    return score


def linkedin_guest_search(role: str, location: str):
    out = []
    # Last 30 days, newest first. One page keeps the GCC sweep inside the workflow timeout.
    url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={quote(role)}&location={quote(location)}&start=0&sortBy=DD&f_TPR=r2592000"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if r.status_code != 200:
            return out
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
                    "title": title,
                    "company": company,
                    "location": loc,
                    "url": href,
                    "posted": posted,
                    "source": "LinkedIn",
                    "description": "",
                    "discovery": "linkedin_guest_gcc",
                    "search_role": role,
                    "search_market": location,
                })
    except Exception:
        pass
    return out


def linkedin_detail(item: dict) -> dict:
    m = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)$", item.get("url", ""))
    if not m:
        return item
    try:
        r = requests.get(
            f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}",
            headers=HEADERS,
            timeout=12,
        )
        if r.status_code != 200:
            return item
        soup = BeautifulSoup(r.text, "html.parser")
        desc = soup.select_one(".show-more-less-html__markup")
        if desc:
            item["description"] = strip_html(desc.get_text(" "))[:6000]
        title = soup.select_one("h2.top-card-layout__title")
        company = soup.select_one("a.topcard__org-name-link") or soup.select_one(".topcard__flavor")
        location = soup.select_one(".topcard__flavor--bullet")
        if title:
            item["title"] = strip_html(title.get_text(" "))
        if company:
            item["company"] = strip_html(company.get_text(" "))
        if location:
            item["location"] = strip_html(location.get_text(" "))
    except Exception:
        pass
    return item


def ddg_fallback():
    out = []
    query_sets = [
        '("Head of Operations" OR "General Manager Operations" OR "Regional Operations Manager") (Saudi Arabia OR UAE OR Kuwait OR Qatar OR Bahrain OR Oman) jobs',
        '("Transport Operations Manager" OR "Mobility Operations Manager" OR "Fleet Operations Manager") (Saudi Arabia OR UAE OR Kuwait OR Qatar OR Bahrain OR Oman) jobs',
        '("Logistics Operations Manager" OR "Last Mile Operations Manager") (Riyadh OR Dubai OR Kuwait OR Doha OR Manama OR Muscat) jobs',
        'site:linkedin.com/jobs/view (operations OR fleet OR mobility OR transport) (Saudi Arabia OR UAE OR Kuwait OR Qatar OR Bahrain OR Oman)',
        'site:gulftalent.com ("Head of Operations" OR "Operations Manager" OR "Fleet Manager") GCC jobs',
        'site:naukrigulf.com (operations manager OR fleet manager OR transport manager) GCC jobs',
    ]
    try:
        with DDGS(timeout=15) as ddgs:
            for q in query_sets:
                for r in ddgs.text(q, max_results=12, safesearch="off") or []:
                    title = r.get("title", "")
                    url = clean_url(r.get("href", ""))
                    body = r.get("body", "")
                    blob = f"{title} {body}".lower()
                    if url and any(w in blob for w in ROLE_WORDS) and not any(x in blob for x in NEGATIVE):
                        out.append({
                            "title": title,
                            "company": "",
                            "location": "",
                            "url": url,
                            "posted": "",
                            "source": "Web Search",
                            "description": body,
                            "discovery": "ddg_gcc_fallback",
                        })
    except Exception:
        pass
    return out


# Search the complete GCC. Executive roles are intentionally first because they have the highest career upside.
raw = []
role_plan = ROLES[:12] if len(ROLES) > 12 else ROLES
for role in role_plan:
    for market in COUNTRY_MARKETS:
        raw.extend(linkedin_guest_search(role, market))
        time.sleep(0.08)

# Supplement with web search if public LinkedIn ingestion is thin or uneven.
if len(raw) < 40:
    raw.extend(ddg_fallback())

# Deduplicate and filter deterministically before sending anything to Gemini.
seen = set()
unique = []
for item in raw:
    u = clean_url(item.get("url", ""))
    if not u or u in seen:
        continue
    seen.add(u)
    item["url"] = u
    item["pre_score"] = pre_score(
        item.get("title", ""), item.get("description", ""), item.get("location", "")
    )
    if item["pre_score"] >= 25:
        unique.append(item)

unique.sort(
    key=lambda x: (x.get("pre_score", 0), bool(x.get("posted"))),
    reverse=True,
)
unique = unique[:50]

# Enrich the strongest direct vacancies with public job descriptions.
for i in range(min(25, len(unique))):
    if "linkedin.com/jobs/view" in unique[i].get("url", ""):
        unique[i] = linkedin_detail(unique[i])
        unique[i]["pre_score"] = pre_score(
            unique[i].get("title", ""),
            unique[i].get("description", ""),
            unique[i].get("location", ""),
        )

(OUTPUT_DIR / "search_debug.json").write_text(
    json.dumps(
        {
            "version": "FINAL-GCC-EXECUTIVE",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "searched_countries": COUNTRY_MARKETS,
            "searched_roles": role_plan,
            "raw_discovered": len(raw),
            "direct_candidates": len(unique),
            "results": unique,
        },
        indent=2,
        ensure_ascii=False,
    ),
    encoding="utf-8",
)

verified_facts = {
    "experience_years": candidate.get("experience_years"),
    "dubai_transport_years": candidate.get("dubai_transport_years"),
    "drivers_managed": candidate.get("drivers_managed"),
    "vehicles_managed": candidate.get("vehicles_managed"),
    "platforms": candidate.get("platforms", []),
    "strengths": candidate.get("strengths", []),
}

prompt = f"""
You are a conservative GCC executive recruitment analyst.
TODAY: {datetime.now(timezone.utc).date().isoformat()}

CANDIDATE PROFILE:
{json.dumps(candidate, indent=2)}

VERIFIED FACTS ALLOWED IN OUTREACH:
{json.dumps(verified_facts, indent=2)}

SEARCH STRATEGY:
{json.dumps(search, indent=2)}

SCORING WEIGHTS:
{json.dumps(weights, indent=2)}

DIRECT JOB CANDIDATES:
{json.dumps(unique, indent=2)}

Rules:
1. Use ONLY supplied jobs. Never invent a vacancy, employer, URL, qualification, requirement or candidate achievement.
2. Evaluate opportunities across the entire GCC: Saudi Arabia, UAE, Kuwait, Qatar, Bahrain and Oman.
3. Prioritize genuine career progression: Head, GM, Regional, Country, Director and Business Unit roles should receive progression credit when responsibilities are realistically achievable.
4. Score 0-100 using the supplied weights. 80+ strong, 70-79 credible, 60-69 watchlist.
5. Reject junior, unrelated engineering, pure sales, warehouse-associate, HR, medical and other irrelevant roles.
6. Do not over-score a senior title where the vacancy requires credentials or scale materially beyond the verified profile. Put those gaps in gaps_risks.
7. Tailored outreach may use ONLY the verified facts supplied above. Do not mention ACCA or any qualification not present in the profile.
8. job_url must exactly equal one supplied URL.
9. Return up to {MAX_JOBS} jobs scoring 60+. Never pad with weak opportunities.
10. Ranking should consider both fit_score and career upside; a strong Head/GM/Regional role may rank above a slightly higher-fit routine manager role.

Return JSON only:
{{"generated_at":"ISO","summary":{{"searched_markets":[],"credible_jobs_found":0,"high_fit_count":0,"watchlist_count":0,"executive_matches":0,"notes":""}},"jobs":[{{"rank":1,"job_title":"","company":"","location":"","fit_score":0,"career_progression_score":0,"fit_tier":"strong|credible|watchlist","live_confidence":"high|medium|low","job_url":"","source":"","why_fit":[""],"gaps_risks":[""],"email_subject":"","email_pitch":"","linkedin_pitch":""}}]}}
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
        if text.endswith("```"):
            text = text[:-3]
    if text.lower().startswith("json\n"):
        text = text[5:]
    data = json.loads(text.strip())
except Exception as e:
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {"notes": f"AI scoring unavailable: {type(e).__name__}"},
        "jobs": [],
    }

valid = {x["url"]: x for x in unique}
clean_jobs = []
for j in data.get("jobs", []):
    try:
        score = max(0, min(100, int(j.get("fit_score", 0))))
    except Exception:
        score = 0
    try:
        progression = max(0, min(100, int(j.get("career_progression_score", 0))))
    except Exception:
        progression = 0
    url = clean_url(j.get("job_url", ""))
    if score < 60 or url not in valid:
        continue
    j["job_url"] = url
    j["fit_score"] = score
    j["career_progression_score"] = progression
    j["fit_tier"] = "strong" if score >= 80 else ("credible" if score >= 70 else "watchlist")
    j["source"] = valid[url].get("source", j.get("source", ""))
    clean_jobs.append(j)

# Safety fallback: valid direct opportunities survive even if Gemini output formatting fails.
if not clean_jobs and unique:
    for item in unique[:MAX_JOBS]:
        ps = item.get("pre_score", 0)
        score = min(79, max(60, 55 + ps // 3))
        if score < 60:
            continue
        title_l = item.get("title", "").lower()
        progression = 80 if any(x in title_l for x in EXECUTIVE_TERMS) else 55
        clean_jobs.append(
            {
                "rank": 0,
                "job_title": item.get("title", ""),
                "company": item.get("company", ""),
                "location": item.get("location", ""),
                "fit_score": score,
                "career_progression_score": progression,
                "fit_tier": "credible" if score >= 70 else "watchlist",
                "live_confidence": "medium" if item.get("posted") else "low",
                "job_url": item.get("url", ""),
                "source": item.get("source", ""),
                "why_fit": ["Role and GCC market align with operations leadership experience."],
                "gaps_risks": ["AI scoring was unavailable; review the full vacancy before applying."],
                "email_subject": "",
                "email_pitch": "",
                "linkedin_pitch": "",
            }
        )

# Final ranking blends fit with career upside without allowing weak roles to leapfrog strong fits.
for j in clean_jobs:
    j["ranking_score"] = round(j.get("fit_score", 0) * 0.8 + j.get("career_progression_score", 0) * 0.2, 1)
clean_jobs.sort(key=lambda x: (x.get("ranking_score", 0), x.get("fit_score", 0)), reverse=True)
clean_jobs = clean_jobs[:MAX_JOBS]
for i, j in enumerate(clean_jobs, 1):
    j["rank"] = i

summary = data.setdefault("summary", {})
summary["credible_jobs_found"] = sum(1 for j in clean_jobs if j["fit_score"] >= 70)
summary["high_fit_count"] = sum(1 for j in clean_jobs if j["fit_score"] >= 80)
summary["watchlist_count"] = sum(1 for j in clean_jobs if 60 <= j["fit_score"] < 70)
summary["executive_matches"] = sum(1 for j in clean_jobs if j.get("career_progression_score", 0) >= 75)
summary["discovery_candidates_reviewed"] = len(unique)
summary["searched_markets"] = COUNTRY_MARKETS
summary["engine"] = "Full GCC direct vacancies + web fallback + Gemini fit/career scoring"
data["jobs"] = clean_jobs
data["generated_at"] = data.get("generated_at") or datetime.now(timezone.utc).isoformat()

(OUTPUT_DIR / "latest.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

md = [
    "# Sohail Qureshi — GCC Executive Job Match Report", "",
    f"Generated: {data['generated_at']}", "",
    f"**Markets:** {', '.join(COUNTRY_MARKETS)}  ",
    f"**Direct candidates reviewed:** {len(unique)}  ",
    f"**Credible jobs:** {summary['credible_jobs_found']}  ",
    f"**Strong matches:** {summary['high_fit_count']}  ",
    f"**Executive/progression matches:** {summary['executive_matches']}  ",
    f"**Watchlist:** {summary['watchlist_count']}  ", "",
]
if summary.get("notes"):
    md += [str(summary["notes"]), ""]
for j in clean_jobs:
    md += [
        f"## {j['rank']}. {j.get('job_title','')} — {j.get('company','')} ({j.get('fit_score',0)}/100)",
        f"**Career progression:** {j.get('career_progression_score',0)}/100  ",
        f"**Ranking score:** {j.get('ranking_score',0)}  ",
        f"**Tier:** {j.get('fit_tier','')}  ",
        f"**Location:** {j.get('location','')}  ",
        f"**Live confidence:** {j.get('live_confidence','')}  ",
        f"**Vacancy:** {j.get('job_url','')}  ", "",
        "**Why it fits**",
    ]
    md += [f"- {x}" for x in j.get("why_fit", [])]
    md += ["", "**Gaps / risks**"]
    md += [f"- {x}" for x in j.get("gaps_risks", [])]
    if j.get("email_pitch"):
        md += ["", "**Email pitch**", j.get("email_pitch", "")]
    if j.get("linkedin_pitch"):
        md += ["", "**LinkedIn pitch**", j.get("linkedin_pitch", "")]
    md += [""]

(OUTPUT_DIR / "latest.md").write_text("\n".join(md), encoding="utf-8")
print(
    f"GCC Job AI reviewed {len(unique)} candidates across {len(COUNTRY_MARKETS)} countries; "
    f"kept {len(clean_jobs)} opportunities."
)
