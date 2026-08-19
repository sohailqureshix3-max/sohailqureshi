import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LATEST = ROOT / "output" / "latest.json"
TRACKER = ROOT / "applications.json"
THRESHOLD = 75


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"https?://", "", value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")[:120]


def make_id(job: dict) -> str:
    url = job.get("job_url", "")
    m = re.search(r"(\d{7,})", url)
    if m:
        return f"{slugify(job.get('company','job'))}-{m.group(1)}"
    return slugify(f"{job.get('company','')} {job.get('job_title','')} {job.get('location','')}")

now = datetime.now(timezone.utc).isoformat()
latest = json.loads(LATEST.read_text(encoding="utf-8")) if LATEST.exists() else {"jobs": []}
tracker = json.loads(TRACKER.read_text(encoding="utf-8")) if TRACKER.exists() else {"applications": []}
existing = {a.get("id"): a for a in tracker.get("applications", []) if a.get("id")}
protected = {"Applied", "Follow-up Due", "Recruiter Replied", "Interview", "Offer", "Rejected", "Closed"}
current_ids = set()

for job in latest.get("jobs", []):
    jid = make_id(job); current_ids.add(jid)
    score = int(job.get("fit_score", 0) or 0)
    eligible = bool(job.get("auto_apply_eligible")) and score >= THRESHOLD and job.get("scoring_mode") == "ai"
    base = {
        "job_title": job.get("job_title", ""), "company": job.get("company", ""), "location": job.get("location", ""),
        "fit_score": score, "career_progression_score": int(job.get("career_progression_score",0) or 0),
        "job_url": job.get("job_url", ""), "source": job.get("source", ""),
        "scoring_mode": job.get("scoring_mode", ""), "auto_apply_eligible": eligible,
        "last_seen_at": now,
    }
    if jid in existing:
        row = existing[jid]; row.update({k:v for k,v in base.items() if v not in (None, "")})
        if row.get("status") not in protected:
            row["status"] = "Auto-Apply Queue" if eligible else "Review"
            row["notes"] = "AI-validated score 75+ and industry-aligned: queued for application." if eligible else "Current GCC shortlist item; manual review required before application."
    else:
        existing[jid] = {
            "id": jid, **base,
            "status": "Auto-Apply Queue" if eligible else "Review",
            "applied_at": "", "application_method": "", "contact_email": "", "follow_up_due": "",
            "last_feedback_at": "", "feedback_status": "No feedback", "feedback_summary": "", "last_message_id": "",
            "notes": "AI-validated score 75+ and industry-aligned: queued for application." if eligible else "Current GCC shortlist item; manual review required before application."
        }

for jid, row in existing.items():
    if jid not in current_ids and row.get("status") not in protected:
        row["status"] = "Archived"
        row["auto_apply_eligible"] = False
        row["notes"] = "Not present in the latest hardened GCC shortlist. Preserved for history only."

rows = list(existing.values())
status_order = {"Offer":-1,"Interview":0,"Recruiter Replied":1,"Applied":2,"Follow-up Due":3,"Auto-Apply Queue":4,"Review":5,"Archived":8,"Rejected":9,"Closed":10}
rows.sort(key=lambda x:(status_order.get(x.get("status","Review"),7), -int(x.get("fit_score",0) or 0)))

out = {
    "updated_at": now,
    "auto_apply_threshold": THRESHOLD,
    "auto_apply_note": "Only AI-validated, industry-aligned jobs scoring 75+ enter Auto-Apply Queue. Fallback-scored jobs never auto-apply.",
    "applications": rows,
}
TRACKER.write_text(json.dumps(out,indent=2,ensure_ascii=False),encoding="utf-8")
print(f"Tracker contains {len(rows)} records; safe auto-apply queue contains {sum(1 for r in rows if r.get('status')=='Auto-Apply Queue')} jobs")
