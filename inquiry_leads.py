# -*- coding: utf-8 -*-
"""Daily 7am reminder: for every deal still in the 'inquiry' stage, email Simon
   "<Client> <ShowDate> is still in leads" with a link straight to the deal.

Dry-run by default; pass --send to actually send. Guarded to send at most ONCE
PER DAY per deal (a bookkeeping key in cue_state), so re-running is safe.
Reminders stop automatically as soon as the deal leaves the inquiry stage.
"""
import sys, json, html, datetime
from db import connect
import mailer
import tz

SEND = "--send" in sys.argv
FORCE = "--now" in sys.argv          # bypass the 7am-ET gate (for a manual/one-time run)
TODAY = tz.today().isoformat()       # Eastern day, so cloud (UTC) runs match Simon's local day
SEND_HOUR = 7                        # first sweep at/after 7am Eastern sends the daily reminder
# CRM app base for the deal deep-link (?deal=<id> opens that deal). Keep in sync with send_due.py.
CRM_BASE = "https://crm.thesimonshow.com"
GUARD_KEY = "_inquiry_lead"          # cue_state bookkeeping (not an email template key)

MO = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
def _d(v):
    if v is None: return None
    if isinstance(v,(datetime.date,datetime.datetime)): return v.date() if isinstance(v,datetime.datetime) else v
    return datetime.date.fromisoformat(str(v)[:10])
def short_date(v):
    d=_d(v)
    return f"{MO[d.month-1]} {d.day}, {d.year}" if d else "no date yet"

def body_html(who, when, occ, crm_url):
    b=['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    b.append(f'<p style="margin:0 0 10px"><a href="{html.escape(crm_url)}" '
             f'style="color:#1155cc;font-weight:bold;text-decoration:underline">&#128220; Open this lead in the CRM</a></p>')
    sub=" &middot; ".join(x for x in [html.escape(who), html.escape(when), (html.escape(occ) if occ else "")] if x)
    b.append(f'<p style="margin:0;color:#5f6368">{sub}</p></div>')
    return "".join(b)

def main():
    if SEND and not FORCE and tz.hour() < SEND_HOUR:
        print(f"{TODAY}  -  before {SEND_HOUR}am ET (now {tz.hour()}:00 ET), skipping inquiry reminders")
        return
    c=connect(); c.autocommit=True; cur=c.cursor()
    cur.execute("select d.id, d.show_date, d.occasion, d.cue_state, "
                "coalesce(ct.full_name, d.deal_name, 'Unnamed lead') "
                "from deals d left join contacts ct on ct.id=d.primary_contact_id "
                "where d.stage='inquiry'")
    rows=cur.fetchall()
    to="simon@thesimonshow.com"
    due=[]
    for (did, show_date, occasion, cue_state, who) in rows:
        st = cue_state or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        if (st.get(GUARD_KEY) or {}).get("pinged")==TODAY: continue   # already reminded today
        who=(who or "Unnamed lead").strip().title()
        when=short_date(show_date)
        subj=f"{who} {when} is still in leads"
        crm=f"{CRM_BASE}/?deal={did}"
        due.append((did, subj, body_html(who, when, occasion, crm), st))
    print(f"{TODAY}  -  {len(due)} inquiry reminder(s) {'to SEND' if SEND else '(dry-run)'}")
    for (did, subj, body, st) in due:
        print(f"  -> {subj[:80]}")
        if SEND:
            mailer.send_email(to, subj, body)
            st[GUARD_KEY]={"pinged":TODAY}
            cur.execute("update deals set cue_state=%s where id=%s",(json.dumps(st), did))
    if SEND and due: print("sent + marked.")
    c.close()

if __name__=="__main__":
    main()
