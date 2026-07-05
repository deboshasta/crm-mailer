# -*- coding: utf-8 -*-
"""Nag while a deal sits in 'Proposal & Follow-up' with the proposal NOT yet sent (checklist
incomplete). Emails Simon a link to finish + send it. Meant to run every 4 hours during business
hours (8am-8pm); sends for each such deal on every run. Dry-run by default; pass --send to send.
"""
import sys, json, html, datetime
from db import connect
import mailer
import tz

SEND = "--send" in sys.argv
TODAY = tz.today()                      # Eastern day, so cloud (UTC) runs match Simon's local day
CRM_BASE = "https://crm.thesimonshow.com"  # keep in sync with send_due.py CRM_BASE
GUARD_KEY = "_proposal_nag"             # per-deal cue_state bookkeeping: last nag timestamp
MIN_GAP_HOURS = 4                       # don't re-nag the same deal within this many hours
BIZ_START, BIZ_END = 8, 20             # only nag between 8am and 8pm Eastern

def body_html(who, crm_url):
    b=['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    b.append(f'<p style="margin:0 0 10px"><a href="{html.escape(crm_url)}" '
             f'style="color:#1155cc;font-weight:bold;text-decoration:underline">&#128220; Open this deal in the CRM</a></p>')
    b.append(f'<p style="margin:0"><b>{html.escape(who)}</b> is in Proposal &amp; Follow-up but the proposal '
             f'is still not sent. Finish the checklist (video / landing page / send) to start the follow-ups.</p></div>')
    return "".join(b)

def main():
    if SEND and not (BIZ_START <= tz.hour() < BIZ_END):
        print(f"{TODAY}  -  outside {BIZ_START}am-{BIZ_END-12}pm ET (now {tz.hour()}:00 ET), skipping proposal nags")
        return
    c=connect(); c.autocommit=True; cur=c.cursor()
    cur.execute("select d.id, d.cue_state, coalesce(ct.full_name, d.deal_name, 'This deal') "
                "from deals d left join contacts ct on ct.id=d.primary_contact_id "
                "where d.stage='proposal_sent' and d.proposal_sent_at is null")
    to="simon@thesimonshow.com"
    now=datetime.datetime.now(datetime.timezone.utc)
    due=[]
    for (did, cue_state, who) in cur.fetchall():
        st=cue_state or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        last=(st.get(GUARD_KEY) or {}).get("ts")
        if last:
            try:
                if (now - datetime.datetime.fromisoformat(last)).total_seconds() < MIN_GAP_HOURS*3600:
                    continue                       # already nagged within the last 4 hours
            except Exception: pass
        due.append((did, who, st))
    print(f"{TODAY}  -  {len(due)} 'proposal not sent' nag(s) {'to SEND' if SEND else '(dry-run)'}")
    for (did, who, st) in due:
        subj=f"Proposal not sent yet - {who}"
        print(f"  -> {subj[:80]}")
        if SEND:
            mailer.send_email(to, subj, body_html(who, f"{CRM_BASE}/?deal={did}"))
            st[GUARD_KEY]={"ts": now.isoformat()}
            cur.execute("update deals set cue_state=%s where id=%s",(json.dumps(st), did))
    c.close()

if __name__=="__main__":
    main()
