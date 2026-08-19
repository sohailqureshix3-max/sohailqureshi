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

latest = json.loads(LATEST.read_text(encoding="utf-8")) if LATEST.exists() else {"jobs": []}
tracker = json.loads(TRACKER.read_text(encoding="utf-8")) if TRACKER.exists() else {"applications": []}
existing = {a.get("id"): a for a in tracker.get("applications", []) if a.get("id")}

protected_statuses = {
    "Applied", "Follow-up Due", "Recruiter Replied", "Interview", "Offer", "Rejected", "Closed"
}

for job in latest.get("jobs", []):
    jid = make_id(job)
    score = int(job.get("fit_score", 0) or 0)
    if jid in existing:
        row = existing[jid]
        for key in ["job_title", "company", "location", "fit_score", "job_url", "source"]:
            if job.get(key) not in (None, ""):
                row[key] = job.get(key)
        if row.get("status") not in protected_statuses:
            row["status"] = "Auto-Apply Queue" if score >= THRESHOLD else "New"
            if score >= THRESHOLD:
                row["notes"] = "Score 75+: automatically queued for application. External job-site submission is not marked Applied until a real submission is confirmed."
        continue

    existing[jid] = {
        "id": jid,
        "job_title": job.get("job_title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "fit_score": score,
        "job_url": job.get("job_url", ""),
        "source": job.get("source", ""),
        "status": "Auto-Apply Queue" if score >= THRESHOLD else "New",
        "applied_at": "",
        "application_method": "",
        "contact_email": "",
        "follow_up_due": "",
        "last_feedback_at": "",
        "feedback_status": "No feedback",
        "feedback_summary": "",
        "last_message_id": "",
        "notes": "Score 75+: automatically queued for application. External job-site submission is not marked Applied until a real submission is confirmed." if score >= THRESHOLD else "Below automatic-application threshold."
    }

rows = list(existing.values())
status_order = {
    "Offer": -1, "Interview": 0, "Recruiter Replied": 1, "Applied": 2,
    "Follow-up Due": 3, "Auto-Apply Queue": 4, "New": 5, "Rejected": 6, "Closed": 9
}
rows.sort(key=lambda x: (status_order.get(x.get("status", "New"), 8), -int(x.get("fit_score", 0) or 0)))

out = {
    "updated_at": datetime.now(timezone.utc).isoformat(),
    "auto_apply_threshold": THRESHOLD,
    "auto_apply_note": "Jobs scoring 75+ are automatically queued. They are marked Applied only when a real application submission is confirmed.",
    "applications": rows,
}
TRACKER.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"Tracker contains {len(rows)} opportunities; auto-apply queue threshold is {THRESHOLD}+")