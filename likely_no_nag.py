# -*- coding: utf-8 -*-
"""Daily 8am nag: for every deal parked in 'likely_no' whose show is <=4 days away, email Simon
asking whether to move it to Closed Lost, with a link to the deal. Repeats daily until the deal
leaves the stage. Dry-run by default; pass --send to actually send. Guarded to once/day per deal.
  --deal <id>   force-send for ONE deal, ignoring the 4-day gate + guard (used for the one-time test).
"""
import sys, json, html, datetime
from db import connect
import mailer
import tz

SEND = "--send" in sys.argv
TODAY = tz.today()                          # Eastern day, so cloud (UTC) runs match Simon's local day
SEND_HOUR = 8                               # first sweep at/after 8am Eastern sends the daily nag
CRM_BASE = "https://crm.thesimonshow.com"    # keep in sync with send_due.py CRM_BASE
# One-click "move it to Closed Lost". Folded into approve-email because crm-send is AT Vercel's
# Hobby cap of 12 Serverless Functions - see the ?a=closelost block there. Auth is the deal's own
# customize_token, the same shared secret the vip_missing mark-done buttons use.
CLOSELOST_BASE = "https://crm-send-the-simon-show.vercel.app/api/approve-email?a=closelost"
GUARD_KEY = "_likely_no_nag"
FORCE_DEAL = None
if "--deal" in sys.argv:
    _i = sys.argv.index("--deal")
    if _i+1 < len(sys.argv): FORCE_DEAL = sys.argv[_i+1]

MO=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
def _d(v):
    if v is None: return None
    if isinstance(v,(datetime.date,datetime.datetime)): return v.date() if isinstance(v,datetime.datetime) else v
    return datetime.date.fromisoformat(str(v)[:10])
def short_date(v):
    d=_d(v); return f"{MO[d.month-1]} {d.day}, {d.year}" if d else "no date"

def _btn(url, text, bg):
    return (f'<a href="{html.escape(url)}" style="display:inline-block;background:{bg};color:#fff;'
            'text-decoration:none;font-weight:bold;padding:10px 18px;border-radius:8px;margin:0 8px 8px 0">'
            f'{html.escape(text)}</a>')

def body_html(who, when, days_txt, crm_url, token=None):
    b=['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    b.append(f'<p style="margin:0 0 10px"><a href="{html.escape(crm_url)}" '
             f'style="color:#1155cc;font-weight:bold;text-decoration:underline">&#128220; Open this deal in the CRM</a></p>')
    b.append(f'<p style="margin:0 0 6px"><b>{html.escape(who)}</b> is sitting in <b>Likely No</b> and the show is '
             f'{html.escape(days_txt)} ({html.escape(when)}).</p>')
    b.append('<p style="margin:0 0 14px;color:#5f6368">Move it to <b>Closed Lost</b>, or revive it if it&#39;s back on.</p>')
    # One click does it from here; the CRM link above is still there to revive it instead.
    if token:
        b.append(_btn(f"{CLOSELOST_BASE}&t={token}", "CLICK TO CLOSE LOST", "#d0342b"))
    b.append('</div>')
    return "".join(b)

def main():
    if SEND and not FORCE_DEAL and tz.hour() < SEND_HOUR:
        print(f"{TODAY}  -  before {SEND_HOUR}am ET (now {tz.hour()}:00 ET), skipping likely-no nags")
        return
    c=connect(); c.autocommit=True; cur=c.cursor()
    cur.execute("select d.id, d.show_date, d.cue_state, coalesce(ct.full_name, d.deal_name, 'This deal'), "
                "d.customize_token "
                "from deals d left join contacts ct on ct.id=d.primary_contact_id where d.stage='likely_no'")
    to="simon@thesimonshow.com"
    due=[]
    for (did, show_date, cue_state, who, cust_tok) in cur.fetchall():
        if FORCE_DEAL and str(did)!=FORCE_DEAL: continue
        sd=_d(show_date)
        st = cue_state or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        if not FORCE_DEAL:
            if not sd: continue                                    # need a show date to know the window
            if (sd - TODAY).days > 4: continue                     # only once the show is <=4 days out
            if (st.get(GUARD_KEY) or {}).get("pinged")==TODAY.isoformat(): continue   # once/day
        when=short_date(show_date)
        if sd:
            n=(sd-TODAY).days
            days_txt = "TODAY" if n==0 else ("tomorrow" if n==1 else (f"in {n} days" if n>0 else f"{-n} day(s) ago"))
        else:
            days_txt="(no show date set)"
        subj=f"Likely No - move {who} to Closed Lost? (show {when})"
        crm=f"{CRM_BASE}/?deal={did}"
        due.append((did, subj, body_html(who, when, days_txt, crm, cust_tok), st))
    print(f"{TODAY}  -  {len(due)} likely-no nag(s) {'to SEND' if SEND else '(dry-run)'}{' [FORCED]' if FORCE_DEAL else ''}")
    for (did, subj, body, st) in due:
        print(f"  -> {subj[:80]}")
        if SEND:
            mailer.send_email(to, subj, body)
            st[GUARD_KEY]={"pinged":TODAY.isoformat()}
            cur.execute("update deals set cue_state=%s where id=%s",(json.dumps(st), did))
    if SEND and due: print("sent + marked.")
    c.close()

if __name__=="__main__":
    main()
