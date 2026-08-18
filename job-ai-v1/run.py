import json
import os
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI

ROOT = Path(__file__).resolve().parent
PROFILE = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

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
1. Use web search to find CURRENT, live-looking job opportunities published by employers or reputable job boards for the target roles and locations.
2. Favor roles in transport, fleet, mobility, limousine/chauffeur, airport ground transport, hospitality transport, logistics, and adjacent operations-heavy businesses.
3. Reject roles that are clearly junior, pure sales, pure warehouse, unrelated technical engineering, or require qualifications that make the candidate obviously ineligible.
4. For each opportunity, score fit from 0-100 using the supplied weights. Do not inflate scores.
5. Only return jobs scoring at least {search['minimum_score']} unless fewer than 5 credible matches exist; in that case include the strongest lower-scoring jobs and mark them clearly.
6. Maximum {search['max_jobs']} jobs.
7. For every job provide a concise reason for fit, risks/gaps, and a tailored pitch.
8. Pitch must be grounded in the candidate facts only. Never invent revenue, savings, percentages, qualifications, employer names, or achievements.
9. Tailor the pitch to the actual role/company problem. Avoid generic phrases such as 'I am writing to express my interest'.
10. Include a short email subject and a short LinkedIn message (max 450 characters).
11. Include the job URL and source URL if available. Prefer direct employer application links when possible.
12. Be conservative about whether a posting appears live. If uncertain, set live_confidence to 'medium' or 'low'.

Return ONLY valid JSON with exactly this top-level structure:
{{
  "generated_at": "ISO timestamp",
  "summary": {{
    "searched_markets": [],
    "credible_jobs_found": 0,
    "high_fit_count": 0,
    "notes": ""
  }},
  "jobs": [
    {{
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
    }}
  ]
}}
"""

response = client.responses.create(
    model="gpt-5.6",
    tools=[{"type": "web_search"}],
    input=prompt,
)

text = response.output_text.strip()
if text.startswith("```"):
    text = text.split("\n", 1)[1]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

data = json.loads(text)

# Defensive sorting and score normalization.
for job in data.get("jobs", []):
    try:
        job["fit_score"] = max(0, min(100, int(job.get("fit_score", 0))))
    except Exception:
        job["fit_score"] = 0

data["jobs"] = sorted(data.get("jobs", []), key=lambda j: j.get("fit_score", 0), reverse=True)
for i, job in enumerate(data["jobs"], 1):
    job["rank"] = i

(OUTPUT_DIR / "latest.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

md = []
md.append("# Sohail Qureshi — AI Job Match Report")
md.append("")
md.append(f"Generated: {data.get('generated_at', '')}")
md.append("")
summary = data.get("summary", {})
md.append(f"**Credible jobs found:** {summary.get('credible_jobs_found', len(data['jobs']))}  ")
md.append(f"**High-fit jobs:** {summary.get('high_fit_count', 0)}  ")
md.append(f"**Markets:** {', '.join(summary.get('searched_markets', []))}")
md.append("")
if summary.get("notes"):
    md.append(summary["notes"])
    md.append("")

for job in data["jobs"]:
    md.append(f"## {job['rank']}. {job.get('job_title','')} — {job.get('company','')} ({job.get('fit_score',0)}/100)")
    md.append(f"**Location:** {job.get('location','')}  ")
    md.append(f"**Live confidence:** {job.get('live_confidence','')}  ")
    if job.get("job_url"):
        md.append(f"**Apply:** {job['job_url']}  ")
    if job.get("source_url"):
        md.append(f"**Source:** {job.get('source','')} — {job['source_url']}")
    md.append("")
    md.append("**Why it fits**")
    for x in job.get("why_fit", []):
        md.append(f"- {x}")
    md.append("")
    md.append("**Gaps / risks**")
    for x in job.get("gaps_risks", []):
        md.append(f"- {x}")
    md.append("")
    md.append(f"**Email subject:** {job.get('email_subject','')}")
    md.append("")
    md.append("**Tailored email pitch**")
    md.append(job.get("email_pitch", ""))
    md.append("")
    md.append("**LinkedIn pitch**")
    md.append(job.get("linkedin_pitch", ""))
    md.append("")
    md.append("---")
    md.append("")

(OUTPUT_DIR / "latest.md").write_text("\n".join(md), encoding="utf-8")
print(f"Generated {len(data['jobs'])} scored opportunities")
