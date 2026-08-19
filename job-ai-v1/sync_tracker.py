import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LATEST = ROOT / "output" / "latest.json"
TRACKER = ROOT / "applications.json"


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

latest = json.loads(LATEST.read_text(encoding="utf-8")) if LATEST.exists() else {"jobs": []}
tracker = json.loads(TRACKER.read_text(encoding="utf-8")) if TRACKER.exists() else {"applications": []}
existing = {a.get("id"): a for a in tracker.get("applications", []) if a.get("id")}

for job in latest.get("jobs", []):
    jid = make_id(job)
    if jid in existing:
        # Refresh public vacancy metadata but preserve application/feedback state.
        row = existing[jid]
        for key in ["job_title", "company", "location", "fit_score", "job_url", "source"]:
            if job.get(key) not in (None, ""):
                row[key] = job.get(key)
        continue
    existing[jid] = {
        "id": jid,
        "job_title": job.get("job_title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "fit_score": job.get("fit_score", 0),
        "job_url": job.get("job_url", ""),
        "source": job.get("source", ""),
        "status": "New",
        "applied_at": "",
        "application_method": "",
        "contact_email": "",
        "follow_up_due": "",
        "last_feedback_at": "",
        "feedback_status": "No feedback",
        "feedback_summary": "",
        "last_message_id": "",
        "notes": "Added automatically by Job AI FINAL. No application has been sent automatically."
    }

rows = list(existing.values())
status_order = {"Interview": 0, "Recruiter Replied": 1, "Applied": 2, "Follow-up Due": 3, "Approved to Apply": 4, "New": 5, "Rejected": 6, "Offer": -1, "Closed": 9}
rows.sort(key=lambda x: (status_order.get(x.get("status", "New"), 8), -int(x.get("fit_score", 0) or 0)))

out = {
    "updated_at": datetime.now(timezone.utc).isoformat(),
    "applications": rows,
}
TRACKER.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Tracker contains {len(rows)} opportunities")
