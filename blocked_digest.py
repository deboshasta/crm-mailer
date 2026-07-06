# -*- coding: utf-8 -*-
"""Daily nag: email Simon once a day for every AUTO email that send_due.py has PAUSED because a
prerequisite merge field is blank. Each row has a button that deep-links into the deal so he can
fill it in (?deal=<id>&fixemail=<key>). Once the field is filled the email auto-sends (send_due.py)
and drops off this list; if he taps the button after it already sent, the app says "already fixed".

Meant to run ONCE a day from its own workflow (blocked-digest.yml). Dry-run by default; --send emails.
"""
import sys, json, html, datetime
from db import connect
import send_due, mailer

TODAY = datetime.date.today()
CRM_BASE = "https://crm.thesimonshow.com"

def main():
    c = connect(); cur = c.cursor()
    cols = ["id","stage","show_date","show_time","venue_address","occasion","company","guest_of_honor",
            "proposal_link","audience_details","show_format","amount","deposit_amount","balance_amount",
            "event_type","is_repeat","customize_token","performer_id","proposal_sent_at",
            "deal_name","cue_state","stage_changed_at","created_at","primary_contact_id"]
    cur.execute("select "+",".join(cols)+" from deals")
    deals = [dict(zip(cols,r)) for r in cur.fetchall()]
    cur.execute("select id,first_name,full_name,email,phone_mobile,phone_other from contacts")
    CB = {r[0]:dict(zip(["id","first_name","full_name","email","phone_mobile","phone_other"],r)) for r in cur.fetchall()}
    cur.execute("select id, first_name, full_name from performers")
    send_due.PERF = {r[0]:{"first_name":r[1],"full_name":r[2]} for r in cur.fetchall()}
    cur.execute("select key,subject,body_html from templates where active=true")
    TPL = {r[0]:{"subject":r[1],"body":r[2]} for r in cur.fetchall()}
    c.close()

    rows = []   # (client_name, key, [blanks], deal_id)
    for d in deals:
        st = d.get("cue_state") or {}
        if isinstance(st,str): st = json.loads(st or "{}")
        contact = CB.get(d.get("primary_contact_id")) or {}
        V = send_due.merge_values(d, contact)
        for key, e in st.items():
            if not isinstance(e, dict) or not e.get("blocked"): continue
            if e.get("sent") or e.get("cancelled"): continue
            t = TPL.get(key)
            if not t: continue
            blanks = send_due.missing_fields(t, e, V)
            if not blanks: continue        # complete now (just pending its send) -> nothing to nag about
            nm = contact.get("full_name") or d.get("deal_name") or "deal"
            rows.append((nm, key, blanks, d["id"]))
    rows.sort(key=lambda r: (r[0], r[1]))

    print(f"{TODAY}: {len(rows)} paused email(s) waiting on missing fields")
    for nm, key, blanks, did in rows:
        print(f"  {nm[:26]:26s} [{key}] missing: {', '.join(blanks)}")

    if not rows:
        print("nothing paused - no nag sent.")
        return

    b = ['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    b.append('<h2 style="margin:0 0 4px">Emails paused - missing info</h2>')
    b.append(f'<p style="color:#5f6368;margin:0 0 16px">{len(rows)} scheduled email(s) are on hold because a '
             'required field is blank. Tap a button to fill it in - the email sends automatically once it is complete.</p>')
    for nm, key, blanks, did in rows:
        url = f"{CRM_BASE}/?deal={did}&fixemail={key}"
        b.append('<div style="border:1px solid #e6e6e6;border-radius:10px;padding:12px 14px;margin:0 0 10px">')
        b.append(f'<div style="font-weight:bold;margin-bottom:2px">{html.escape(nm)}</div>')
        b.append(f'<div style="color:#5f6368;font-size:12px;margin-bottom:9px">{html.escape(key)} &middot; '
                 f'missing: <span style="color:#c0392b">{html.escape(", ".join(blanks))}</span></div>')
        b.append(f'<a href="{html.escape(url)}" style="display:inline-block;background:#1155cc;color:#fff;'
                 'text-decoration:none;font-weight:bold;padding:8px 16px;border-radius:8px">Add the missing info</a>')
        b.append('</div>')
    b.append('<p style="color:#9aa0a6;font-size:12px;margin-top:6px">You get this once a day until each one is '
             'filled in (it then sends on its own) or cancelled from the deal.</p></div>')
    body = "".join(b)

    if "--send" in sys.argv:
        r = mailer.send_email("simon@thesimonshow.com", f"{len(rows)} email(s) paused - missing info", body)
        print("emailed nag ->", r["routed_to"])
    else:
        print("(dry-run; pass --send to email)")

if __name__ == "__main__":
    main()
