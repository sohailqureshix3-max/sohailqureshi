import json
import os
from datetime import datetime, timezone
from pathlib import Path

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

prompt = f"""
You are an executive job-search analyst and outreach strategist.

TODAY: {datetime.now(timezone.utc).date().isoformat()}

CANDIDATE PROFILE:
{json.dumps(candidate, indent=2)}

TARGET SEARCH:
{json.dumps(search, indent=2)}

SCORING WEIGHTS (total 100):
{json.dumps(weights, indent=2)}

TASK:
1. Use Google Search grounding to find CURRENT, live-looking job opportunities published by employers or reputable job boards for the target roles and locations.
2. Favor roles in transport, fleet, mobility, limousine/chauffeur, airport ground transport, hospitality transport, logistics, and adjacent operations-heavy businesses.
3. Reject clearly junior, pure sales, pure warehouse, unrelated technical engineering, or obviously ineligible roles.
4. Score each opportunity from 0-100 using the supplied weights. Do not inflate scores.
5. Only return jobs scoring at least {search['minimum_score']} unless fewer than 5 credible matches exist; then include the strongest lower-scoring jobs and mark them clearly.
6. Maximum {search['max_jobs']} jobs.
7. For every job provide concise fit reasons, risks/gaps, and a tailored pitch.
8. Use candidate facts only. Never invent revenue, savings, percentages, qualifications, employer names, or achievements.
9. Tailor the pitch to the actual role/company problem. Avoid generic phrases such as 'I am writing to express my interest'.
10. Include a short email subject and a LinkedIn message of max 450 characters.
11. Include the job URL and source URL where available. Prefer direct employer application links.
12. Be conservative about whether a posting appears live. If uncertain, use medium or low live_confidence.

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
    model="gemini-2.5-flash",
    contents=prompt,
    config=types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0.2,
    ),
)

text = (response.text or "").strip()
if text.startswith("```"):
    text = text.split("\n", 1)[1]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

# Gemini may prefix a JSON code-language marker.
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

md = [
    "# Sohail Qureshi — AI Job Match Report",
    "",
    f"Generated: {data.get('generated_at', '')}",
    "",
]
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
    md += ["", "**Why it fits**"]
    md += [f"- {x}" for x in job.get("why_fit", [])]
    md += ["", "**Gaps / risks**"]
    md += [f"- {x}" for x in job.get("gaps_risks", [])]
    md += [
        "",
        f"**Email subject:** {job.get('email_subject','')}",
        "",
        "**Tailored email pitch**",
        job.get("email_pitch", ""),
        "",
        "**LinkedIn pitch**",
        job.get("linkedin_pitch", ""),
        "",
        "---",
        "",
    ]

(OUTPUT_DIR / "latest.md").write_text("\n".join(md), encoding="utf-8")
print(f"Generated {len(data['jobs'])} scored opportunities with Gemini")
