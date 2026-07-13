# -*- coding: utf-8 -*-
"""Flag deals with a scheduled email due in the next N days that still has BLANK merge
fields - so Simon knows exactly what to fill in before it goes out. Emails the report to
Simon. Run alongside the nightly job."""
import sys, re, json, html, datetime
from db import connect
import send_due, mailer

WINDOW = 3
TODAY = datetime.date.today()
OPTIONAL = {"Company", "GuestOfHonor", "GuestOfHonorMagicLine", "ProposalLink", "LastShowYear", "EventDetails"}  # legitimately-often-blank (keep in sync with send_due.OPTIONAL_FIELDS)

def main():
    c = connect(); cur = c.cursor()
    cols = ["id","stage","show_date","show_time","venue_address","occasion","company","guest_of_honor",
            "proposal_link","audience_details","show_format","amount","deposit_amount","balance_amount",
            "event_type","deal_name","cue_state","stage_changed_at","created_at","primary_contact_id"]
    cur.execute("select "+",".join(cols)+" from deals")
    deals=[dict(zip(cols,r)) for r in cur.fetchall()]
    cur.execute("select id,first_name,full_name,email,phone_mobile,phone_other from contacts")
    CB={r[0]:dict(zip(["id","first_name","full_name","email","phone_mobile","phone_other"],r)) for r in cur.fetchall()}
    cur.execute("select key,subject,body_html from templates where active=true")
    TPL={r[0]:{"subject":r[1],"body":r[2]} for r in cur.fetchall()}
    c.close()

    fields_of = lambda t: set(re.findall(r"\{\{(\w+)\}\}", (t["subject"] or "")+" "+(t["body"] or "")))
    end = TODAY + datetime.timedelta(days=WINDOW)
    rows=[]  # (send_date, dealname, contactname, key, mode, [blanks])
    for d in deals:
        contact = CB.get(d.get("primary_contact_id")) or {}
        st = d.get("cue_state") or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        V = send_due.merge_values(d, contact)
        for (key,anchor,off,mode,stages) in send_due.CUE:
            t = TPL.get(key)
            if not t: continue
            base = send_due.anchor_date(d, anchor, stages)
            if not base: continue
            sd = base + datetime.timedelta(days=off)
            if not (TODAY <= sd <= end): continue
            e = st.get(key) or {}
            if e.get("sent") or e.get("cancelled"): continue
            blanks = sorted(f for f in fields_of(t) if not V.get(f) and f not in OPTIONAL)
            if blanks:
                nm = (contact.get("full_name") or d.get("deal_name") or "deal")
                rows.append((sd, nm, key, mode, blanks))
    rows.sort(key=lambda r: (r[0], r[1]))

    if rows:
        b=['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
        b.append(f'<h2 style="margin:0 0 4px">Emails due by {end:%b %d} with blank fields</h2>')
        b.append(f'<p style="color:#5f6368;margin:0 0 14px">{len(rows)} scheduled email(s) are missing data. Fill these before they send:</p>')
        b.append('<table style="border-collapse:collapse;font-size:13px">')
        b.append('<tr style="text-align:left;color:#5f6368"><th style="padding:4px 12px 4px 0">Send date</th>'
                 '<th style="padding:4px 12px 4px 0">Client</th><th style="padding:4px 12px 4px 0">Email</th>'
                 '<th style="padding:4px 0">Blank fields</th></tr>')
        for sd,nm,key,mode,blanks in rows:
            b.append(f'<tr><td style="padding:4px 12px 4px 0">{sd:%b %d}</td>'
                     f'<td style="padding:4px 12px 4px 0">{html.escape(nm)}</td>'
                     f'<td style="padding:4px 12px 4px 0">{html.escape(key)} <span style="color:#9aa0a6">({mode})</span></td>'
                     f'<td style="padding:4px 0;color:#c0392b">{html.escape(", ".join(blanks))}</td></tr>')
        b.append('</table></div>')
        body="".join(b)
    else:
        body='<div style="font-family:Verdana,Arial,sans-serif;font-size:14px">All clear - no scheduled emails in the next %d days have blank fields.</div>' % WINDOW

    print(f"{TODAY}: {len(rows)} upcoming email(s) with blank fields (window {WINDOW}d)")
    for sd,nm,key,mode,blanks in rows:
        print(f"  {sd}  {nm[:24]:24s} [{key}] blanks: {', '.join(blanks)}")
    if "--send" in sys.argv:
        r = mailer.send_email("simon@thesimonshow.com", f"[CRM] {len(rows)} upcoming email(s) need info", body)
        print("emailed report ->", r["routed_to"])
    return body

if __name__ == "__main__":
    main()
