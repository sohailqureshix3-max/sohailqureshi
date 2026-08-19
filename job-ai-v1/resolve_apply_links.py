import json, re
from pathlib import Path
from urllib.parse import urlparse
from ddgs import DDGS

ROOT=Path(__file__).resolve().parent
TRACKER=ROOT/'applications.json'
SAFE=('greenhouse.io','lever.co','smartrecruiters.com','workable.com','workdayjobs.com','myworkdayjobs.com','amazon.jobs','careers.')

def host(u):
    try:return urlparse(u).netloc.lower()
    except:return ''

def safe(u):
    h=host(u)
    return any(x in h for x in SAFE) and 'linkedin.com' not in h

def search(company,title):
    qs=[f'"{company}" "{title}" apply',f'"{title}" "{company}" careers',f'"{company}" jobs "{title}"']
    for q in qs:
        try:
            with DDGS(timeout=20) as d:
                for r in d.text(q,max_results=8,safesearch='off') or []:
                    u=r.get('href','')
                    text=(r.get('title','')+' '+r.get('body','')).lower()
                    if safe(u) and any(k in text for k in ('apply','job','career','position')):
                        return u
        except Exception:
            pass
    return ''

data=json.loads(TRACKER.read_text(encoding='utf-8'))
for row in data.get('applications',[]):
    if int(row.get('fit_score',0) or 0)<75: continue
    if row.get('submission_url'): continue
    u=row.get('job_url','')
    if safe(u): row['submission_url']=u; row['submission_source']='direct'
    else:
        resolved=search(row.get('company',''),row.get('job_title',''))
        if resolved:
            row['submission_url']=resolved; row['submission_source']='resolved employer/ATS search'
        else:
            row['submission_url']=''; row['submission_source']='unresolved'
TRACKER.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding='utf-8')
print('Resolved ATS apply links where available')
