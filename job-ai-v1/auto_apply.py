import json, os, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parent
TRACKER=ROOT/'applications.json'
CV=(ROOT.parent/'Sohail_Qureshi_Saudi_Fleet_Operations_CV.pdf').resolve()
EMAIL=os.getenv('APPLICANT_EMAIL','').strip()
PHONE=os.getenv('APPLICANT_PHONE','').strip()
FIRST=os.getenv('APPLICANT_FIRST_NAME','Muhammad Sohail Ayub').strip()
LAST=os.getenv('APPLICANT_LAST_NAME','Qureshi').strip()
LOCATION=os.getenv('APPLICANT_LOCATION','Dubai, UAE').strip()
LINKEDIN=os.getenv('APPLICANT_LINKEDIN','https://www.linkedin.com/in/sohail-qureshi-6ab21a225/').strip()
PORTFOLIO=os.getenv('APPLICANT_PORTFOLIO','https://sohailqureshix3-max.github.io/sohailqureshi/').strip()
SAFE_DOMAINS=('greenhouse.io','lever.co','smartrecruiters.com','workable.com','workdayjobs.com','myworkdayjobs.com','amazon.jobs')
BLOCKED_DOMAINS=('linkedin.com','indeed.com','bayt.com','gulftalent.com','naukrigulf.com')
KNOWN={'first name':FIRST,'firstname':FIRST,'last name':LAST,'lastname':LAST,'email':EMAIL,'phone':PHONE,'mobile':PHONE,'location':LOCATION,'linkedin':LINKEDIN,'website':PORTFOLIO,'portfolio':PORTFOLIO}

def now(): return datetime.now(timezone.utc).isoformat()
def due(): return (datetime.now(timezone.utc)+timedelta(days=5)).date().isoformat()
def host(u):
    try:return urlparse(u).netloc.lower()
    except:return ''
def set_status(row,status,note,method=''):
    row['status']=status; row['notes']=note
    if method: row['application_method']=method

def fill_known(page):
    for label,val in KNOWN.items():
        if not val: continue
        try:
            el=page.get_by_label(re.compile(label,re.I)).first
            if el.is_visible(): el.fill(val)
        except: pass
    selectors={'input[name*="first" i]':FIRST,'input[name*="last" i]':LAST,'input[type="email"]':EMAIL,'input[type="tel"]':PHONE,'input[name*="linkedin" i]':LINKEDIN,'input[name*="website" i]':PORTFOLIO}
    for sel,val in selectors.items():
        if not val: continue
        try:
            for el in page.locator(sel).all():
                if el.is_visible() and not el.input_value(): el.fill(val)
        except: pass
    try:
        for el in page.locator('input[type="file"]').all():
            el.set_input_files(str(CV)); break
    except: pass

def unknown_required(page):
    unknown=[]
    try:
        for el in page.locator('input[required],textarea[required],select[required]').all():
            if not el.is_visible(): continue
            typ=(el.get_attribute('type') or '').lower()
            if typ in ('hidden','submit','button','file','checkbox','radio'): continue
            val=''
            try: val=el.input_value()
            except: pass
            if not val:
                name=(el.get_attribute('name') or el.get_attribute('aria-label') or el.get_attribute('placeholder') or 'required field')
                unknown.append(name[:80])
    except: pass
    return unknown

def try_apply(page,url,row):
    if not url:
        set_status(row,'Needs Manual Submission','No employer ATS submission link could be resolved.','Manual apply')
        return
    h=host(url)
    if any(d in h for d in BLOCKED_DOMAINS):
        set_status(row,'Needs Manual Submission','This job board blocks unattended authenticated submissions.','Manual job-board apply')
        return
    if not any(d in h for d in SAFE_DOMAINS):
        set_status(row,'Needs Manual Submission','No supported ATS adapter detected for the resolved application URL.','Manual external ATS')
        return
    if not EMAIL or not PHONE:
        set_status(row,'Needs Setup','Missing APPLICANT_EMAIL or APPLICANT_PHONE GitHub Actions secret.','ATS automation')
        return
    try:
        page.goto(url,wait_until='domcontentloaded',timeout=45000); page.wait_for_timeout(2000)
        for text in ('Apply for this job','Apply now','Apply','Submit application'):
            try:
                b=page.get_by_role('button',name=re.compile(f'^{re.escape(text)}$',re.I)).first
                if b.is_visible(): b.click(); page.wait_for_timeout(1200); break
            except: pass
        fill_known(page)
        missing=unknown_required(page)
        if missing:
            set_status(row,'Needs Manual Answers','Required fields need verified answers: '+', '.join(missing[:6]),'ATS automation')
            return
        submitted=False
        for pat in (r'submit application',r'submit',r'apply now'):
            try:
                btn=page.get_by_role('button',name=re.compile(pat,re.I)).last
                if btn.is_visible() and btn.is_enabled(): btn.click(); page.wait_for_timeout(3500); submitted=True; break
            except: pass
        body=(page.locator('body').inner_text(timeout=5000) or '').lower()
        confirmed=any(x in body for x in ('application submitted','thank you for applying','application received','thanks for applying','successfully submitted'))
        if submitted and confirmed:
            row['status']='Applied'; row['applied_at']=now(); row['application_method']='Automated ATS submission'; row['follow_up_due']=due(); row['notes']='Submission confirmation detected by browser automation.'
        else:
            set_status(row,'Needs Manual Confirmation','Form was reached but a trustworthy submission confirmation was not detected.','ATS automation')
    except Exception as e:
        set_status(row,'Automation Failed',f'Automation error: {str(e)[:180]}','ATS automation')

data=json.loads(TRACKER.read_text(encoding='utf-8')) if TRACKER.exists() else {'applications':[]}
queue=[r for r in data.get('applications',[]) if int(r.get('fit_score',0) or 0)>=75 and r.get('status') in ('Auto-Apply Queue','New')]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1200})
    for row in queue:
        try_apply(page,row.get('submission_url') or row.get('job_url',''),row)
    browser.close()
data['updated_at']=now(); TRACKER.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding='utf-8')
print(f'Auto-apply processed {len(queue)} queued roles')
