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

ROLES = list(dict.fromkeys(search.get("target_roles", [])))
PROFILE_LOCATIONS = list(dict.fromkeys(search.get("locations", [])))
MARKET_PRIORITY = list(dict.fromkeys(search.get("market_priority", [])))
MAX_JOBS = int(search.get("max_jobs", 15))
GCC_COUNTRIES = ["Saudi Arabia", "United Arab Emirates", "Kuwait", "Qatar", "Bahrain", "Oman"]
COUNTRY_MARKETS = [x for x in MARKET_PRIORITY if x in GCC_COUNTRIES] + [x for x in GCC_COUNTRIES if x not in MARKET_PRIORITY]

DOMAIN_TERMS = [
    "fleet", "vehicle", "transport", "transportation", "mobility", "logistics", "last mile",
    "last-mile", "delivery operations", "driver operations", "dispatch", "ride-hailing", "ride hailing",
    "chauffeur", "limousine", "leasing", "rental", "freight", "distribution", "courier", "fulfillment",
    "airport transport", "ground transport", "e-commerce operations", "ecommerce operations",
]
GENERIC_EXEC = [
    "head of operations", "general manager", "regional operations", "country operations",
    "director of operations", "business unit manager", "senior operations manager",
]
NEGATIVE_TERMS = [
    "restaurant", "food & beverage", "food and beverage", "hotel operations", "rooms division",
    "front office", "data center", "datacenter", "oilfield", "drilling", "offshore marine",
    "medical", "hospital clinical", "nurse", "software engineering", "construction project",
    "project services general manager", "retail store", "sales manager", "human resources",
]
JUNIOR_TERMS = ["intern", "internship", "junior", "associate", "coordinator", "delivery rider", "driver vacancy"]


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


def contains_any(text: str, terms) -> bool:
    t = (text or "").lower()
    return any(x in t for x in terms)


def domain_aligned(title: str, description: str) -> bool:
    blob = f"{title} {description}".lower()
    return contains_any(blob, DOMAIN_TERMS)


def coarse_score(item: dict) -> int:
    title = item.get("title", "").lower()
    desc = item.get("description", "").lower()
    blob = f"{title} {desc} {item.get('location','')}".lower()
    score = 0
    if contains_any(title, JUNIOR_TERMS): score -= 60
    if contains_any(blob, NEGATIVE_TERMS): score -= 55
    if any(r.lower() in title for r in ROLES): score += 28
    if contains_any(title, DOMAIN_TERMS): score += 28
    if contains_any(title, GENERIC_EXEC): score += 16
    if any(m.lower() in blob for m in GCC_COUNTRIES + PROFILE_LOCATIONS): score += 10
    if contains_any(desc, DOMAIN_TERMS): score += 15
    return score


def final_pre_score(item: dict) -> int:
    title = item.get("title", "")
    desc = item.get("description", "")
    blob = f"{title} {desc} {item.get('location','')}".lower()
    if contains_any(blob, JUNIOR_TERMS) or contains_any(blob, NEGATIVE_TERMS):
        return -100
    aligned = domain_aligned(title, desc)
    specific_title = contains_any(title, DOMAIN_TERMS)
    if not aligned and not specific_title:
        return 0
    score = 20
    if specific_title: score += 25
    if contains_any(title, GENERIC_EXEC): score += 18
    if contains_any(desc, DOMAIN_TERMS): score += 18
    if any(x in blob for x in ["budget", "p&l", "profit and loss", "kpi", "cost control", "revenue", "compliance", "sop", "utilization", "utilisation"]): score += 8
    if any(x in blob for x in ["fleet", "driver", "vehicle"]): score += 8
    if any(m.lower() in blob for m in GCC_COUNTRIES + PROFILE_LOCATIONS): score += 8
    return score


def search_query_for_role(role: str) -> str:
    rl = role.lower()
    if contains_any(rl, GENERIC_EXEC):
        return f'{role} (transport OR fleet OR mobility OR logistics OR delivery)'
    return role


def linkedin_guest_search(role: str, location: str):
    out = []
    query = search_query_for_role(role)
    url = (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={quote(query)}&location={quote(location)}&start=0&sortBy=DD&f_TPR=r2592000"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        if r.status_code != 200:
            return out
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select("li"):
            a = card.select_one("a.base-card__full-link") or card.select_one("a[href*='/jobs/view/']")
            if not a: continue
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
                out.append({"title": title, "company": company, "location": loc, "url": href,
                            "posted": posted, "source": "LinkedIn", "description": "",
                            "discovery": "linkedin_guest_gcc", "search_role": role, "search_market": location})
    except Exception:
        pass
    return out


def linkedin_detail(item: dict) -> dict:
    m = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)$", item.get("url", ""))
    if not m: return item
    try:
        r = requests.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}", headers=HEADERS, timeout=12)
        if r.status_code != 200: return item
        soup = BeautifulSoup(r.text, "html.parser")
        desc = soup.select_one(".show-more-less-html__markup")
        if desc: item["description"] = strip_html(desc.get_text(" "))[:7000]
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
        '("Head of Operations" OR "General Manager Operations" OR "Regional Operations Manager") (fleet OR transport OR mobility OR logistics) (Saudi Arabia OR UAE OR Kuwait OR Qatar OR Bahrain OR Oman)',
        '("Transport Operations Manager" OR "Mobility Operations Manager" OR "Fleet Operations Manager") (Saudi Arabia OR UAE OR Kuwait OR Qatar OR Bahrain OR Oman)',
        '("Logistics Operations Manager" OR "Last Mile Operations Manager") (Riyadh OR Dubai OR Kuwait OR Doha OR Manama OR Muscat)',
        'site:linkedin.com/jobs/view (fleet OR mobility OR transport OR logistics) (manager OR head OR director) (Saudi Arabia OR UAE OR Kuwait OR Qatar OR Bahrain OR Oman)',
    ]
    try:
        with DDGS(timeout=15) as ddgs:
            for q in queries:
                for r in ddgs.text(q, max_results=12, safesearch="off") or []:
                    title, url, body = r.get("title", ""), clean_url(r.get("href", "")), r.get("body", "")
                    if not url: continue
                    item = {"title": title, "company": "", "location": "", "url": url, "posted": "",
                            "source": "Web Search", "description": body, "discovery": "ddg_gcc_fallback"}
                    if coarse_score(item) >= 15: out.append(item)
    except Exception:
        pass
    return out


raw = []
role_plan = ROLES[:14] if len(ROLES) > 14 else ROLES
for role in role_plan:
    for market in COUNTRY_MARKETS:
        raw.extend(linkedin_guest_search(role, market))
        time.sleep(0.06)
if len(raw) < 40:
    raw.extend(ddg_fallback())

seen, prelim = set(), []
for item in raw:
    u = clean_url(item.get("url", ""))
    if not u or u in seen: continue
    seen.add(u); item["url"] = u
    item["coarse_score"] = coarse_score(item)
    if item["coarse_score"] >= 15: prelim.append(item)
prelim.sort(key=lambda x: (x.get("coarse_score",0), bool(x.get("posted"))), reverse=True)
prelim = prelim[:60]

for i in range(min(40, len(prelim))):
    if "linkedin.com/jobs/view" in prelim[i].get("url", ""):
        prelim[i] = linkedin_detail(prelim[i])
    prelim[i]["pre_score"] = final_pre_score(prelim[i])

unique = [x for x in prelim if x.get("pre_score", final_pre_score(x)) >= 35]
unique.sort(key=lambda x: (x.get("pre_score",0), bool(x.get("posted"))), reverse=True)
unique = unique[:50]

(OUTPUT_DIR / "search_debug.json").write_text(json.dumps({
    "version": "FINAL-GCC-EXECUTIVE-HARDENED", "generated_at": datetime.now(timezone.utc).isoformat(),
    "searched_countries": COUNTRY_MARKETS, "searched_roles": role_plan, "raw_discovered": len(raw),
    "preliminary_candidates": len(prelim), "direct_candidates": len(unique), "results": unique,
}, indent=2, ensure_ascii=False), encoding="utf-8")

verified_facts = {
    "experience_years": candidate.get("experience_years"), "dubai_transport_years": candidate.get("dubai_transport_years"),
    "drivers_managed": candidate.get("drivers_managed"), "vehicles_managed": candidate.get("vehicles_managed"),
    "platforms": candidate.get("platforms", []), "strengths": candidate.get("strengths", []),
}
prompt = f"""
You are a conservative GCC executive recruitment analyst.
TODAY: {datetime.now(timezone.utc).date().isoformat()}
CANDIDATE PROFILE: {json.dumps(candidate)}
VERIFIED FACTS ALLOWED IN OUTREACH: {json.dumps(verified_facts)}
SEARCH STRATEGY: {json.dumps(search)}
SCORING WEIGHTS: {json.dumps(weights)}
DIRECT JOB CANDIDATES: {json.dumps(unique)}
Rules:
1. Use only supplied jobs and verified candidate facts. Never invent anything.
2. Keep only fleet, transport, mobility, logistics, last-mile, delivery, leasing/rental or closely adjacent operations roles.
3. Reject hospitality/hotel/restaurant, data-center, medical, engineering/construction, offshore/marine and unrelated generic operations even if the title is senior.
4. Score fit 0-100 and career progression 0-100. Do not over-score titles whose industry/scale/credentials are materially beyond the profile.
5. 80+ fit = strong; 70-79 credible; 60-69 watchlist. Return at most {MAX_JOBS}, never pad.
6. job_url must exactly equal a supplied URL.
7. Set auto_apply_eligible=true only when fit_score>=75, live_confidence is high/medium, industry alignment is clear, and no major mandatory credential gap is identified.
Return JSON only: {{"generated_at":"ISO","summary":{{"notes":""}},"jobs":[{{"job_title":"","company":"","location":"","fit_score":0,"career_progression_score":0,"live_confidence":"high|medium|low","job_url":"","source":"","industry_alignment":"clear|adjacent|weak","auto_apply_eligible":false,"why_fit":[""],"gaps_risks":[""],"email_subject":"","email_pitch":"","linkedin_pitch":""}}]}}
"""

data = None
last_error = ""
for attempt in range(3):
    try:
        response = client.models.generate_content(model="gemini-3.1-flash-lite", contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"))
        text = (response.text or "").strip()
        if text.startswith("```"):
            text = text.split("\n",1)[1]
            if text.endswith("```"): text = text[:-3]
        if text.lower().startswith("json\n"): text = text[5:]
        data = json.loads(text.strip()); break
    except Exception as e:
        last_error = f"{type(e).__name__}: {str(e)[:120]}"
        time.sleep(2 * (attempt + 1))
if data is None:
    data = {"generated_at": datetime.now(timezone.utc).isoformat(), "summary": {"notes": f"AI scoring unavailable after 3 attempts: {last_error}"}, "jobs": []}

valid = {x["url"]: x for x in unique}
clean_jobs = []
for j in data.get("jobs", []):
    try: score = max(0, min(100, int(j.get("fit_score",0))))
    except: score = 0
    try: progression = max(0, min(100, int(j.get("career_progression_score",0))))
    except: progression = 0
    url = clean_url(j.get("job_url", ""))
    if score < 60 or url not in valid: continue
    item = valid[url]
    if final_pre_score(item) < 35: continue
    j["job_url"] = url; j["fit_score"] = score; j["career_progression_score"] = progression
    j["fit_tier"] = "strong" if score >= 80 else ("credible" if score >= 70 else "watchlist")
    j["source"] = item.get("source", j.get("source", "")); j["scoring_mode"] = "ai"
    eligible = bool(j.get("auto_apply_eligible")) and score >= 75 and j.get("industry_alignment") in ("clear","adjacent") and j.get("live_confidence") != "low"
    j["auto_apply_eligible"] = eligible
    clean_jobs.append(j)

# Conservative deterministic fallback: show relevant jobs, but NEVER auto-apply without AI validation.
if not clean_jobs and unique:
    for item in unique[:MAX_JOBS]:
        ps = item.get("pre_score",0)
        score = min(79, max(60, 52 + ps // 4))
        title_l = item.get("title","").lower()
        progression = 80 if contains_any(title_l, GENERIC_EXEC) else 55
        clean_jobs.append({
            "job_title": item.get("title",""), "company": item.get("company",""), "location": item.get("location",""),
            "fit_score": score, "career_progression_score": progression,
            "fit_tier": "credible" if score >= 70 else "watchlist", "live_confidence": "medium" if item.get("posted") else "low",
            "job_url": item.get("url",""), "source": item.get("source",""), "industry_alignment": "clear",
            "auto_apply_eligible": False, "scoring_mode": "deterministic_fallback",
            "why_fit": ["Vacancy has verified GCC fleet/transport/mobility/logistics alignment."],
            "gaps_risks": ["AI validation was unavailable; manual review is required before application."],
            "email_subject":"", "email_pitch":"", "linkedin_pitch":"",
        })

for j in clean_jobs:
    j["ranking_score"] = round(j.get("fit_score",0)*0.8 + j.get("career_progression_score",0)*0.2,1)
clean_jobs.sort(key=lambda x:(x.get("ranking_score",0),x.get("fit_score",0)), reverse=True)
clean_jobs = clean_jobs[:MAX_JOBS]
for i,j in enumerate(clean_jobs,1): j["rank"] = i

summary = data.setdefault("summary",{})
summary["credible_jobs_found"] = sum(1 for j in clean_jobs if j["fit_score"]>=70)
summary["high_fit_count"] = sum(1 for j in clean_jobs if j["fit_score"]>=80)
summary["watchlist_count"] = sum(1 for j in clean_jobs if 60<=j["fit_score"]<70)
summary["executive_matches"] = sum(1 for j in clean_jobs if j.get("career_progression_score",0)>=75)
summary["auto_apply_eligible"] = sum(1 for j in clean_jobs if j.get("auto_apply_eligible"))
summary["discovery_candidates_reviewed"] = len(unique); summary["searched_markets"] = COUNTRY_MARKETS
summary["engine"] = "Hardened GCC executive search + industry gate + AI retry + safe auto-apply gating"
data["jobs"] = clean_jobs; data["generated_at"] = data.get("generated_at") or datetime.now(timezone.utc).isoformat()

(OUTPUT_DIR / "latest.json").write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding="utf-8")
md = ["# Sohail Qureshi — GCC Executive Job Match Report", "", f"Generated: {data['generated_at']}", "",
      f"**Markets:** {', '.join(COUNTRY_MARKETS)}  ", f"**Relevant candidates reviewed:** {len(unique)}  ",
      f"**Credible jobs:** {summary['credible_jobs_found']}  ", f"**Strong matches:** {summary['high_fit_count']}  ",
      f"**Executive/progression matches:** {summary['executive_matches']}  ", f"**Auto-apply eligible:** {summary['auto_apply_eligible']}  ", ""]
if summary.get("notes"): md += [str(summary["notes"]),""]
for j in clean_jobs:
    md += [f"## {j['rank']}. {j.get('job_title','')} — {j.get('company','')} ({j.get('fit_score',0)}/100)",
           f"**Career progression:** {j.get('career_progression_score',0)}/100  ", f"**Auto-apply eligible:** {j.get('auto_apply_eligible',False)}  ",
           f"**Scoring:** {j.get('scoring_mode','')}  ", f"**Location:** {j.get('location','')}  ", f"**Vacancy:** {j.get('job_url','')}  ", "",
           "**Why it fits**"] + [f"- {x}" for x in j.get("why_fit",[])] + ["", "**Gaps / risks**"] + [f"- {x}" for x in j.get("gaps_risks",[])] + [""]
(OUTPUT_DIR / "latest.md").write_text("\n".join(md),encoding="utf-8")
print(f"Hardened GCC Job AI reviewed {len(unique)} relevant candidates; kept {len(clean_jobs)}; auto-apply eligible {summary['auto_apply_eligible']}.")
