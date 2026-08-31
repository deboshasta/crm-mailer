# -*- coding: utf-8 -*-
"""Daily VIP-customization safety net.

For every Closed-Won deal whose show is 0-7 days away and still has trivia and/or photos
REQUESTED-BUT-NOT-RECEIVED, alert Simon once a day so a VIP show never sneaks up on him
without the personalizations:
  * a full email to simon@thesimonshow.com  (client name / phone / email / what's missing / deep link)
  * a short text to 7324926071@vtext.com  (Verizon email-to-SMS gateway) whose SUBJECT is
    "URGENT VIP customizations missing for <client> / <show date>"  (the gateway strips HTML, so the
    subject carries the alert; the plain body is a backup).

"Requested but not received" uses the SAME predicate as the customization_request chase in
send_due.py (require_trivia is not False & no trivia_received_at; photo_goh/guest_limit > 0 & no
photos_received_at) so this net and the chase can never disagree about what is outstanding. If that
predicate changes there, change it here too.

Runs ONCE a day from vip-missing.yml (13:00 UTC ~ 9am ET). Dry-run by default; --send emails+texts.
Both sends are owner=True: never rerouted by safe-mode, never treated as a client email. The email
to Simon is auto-tagged "smCRM " centrally in mailer.send_email; the vtext line is a phone, so it is
deliberately NOT tagged.
"""
import sys, html, datetime
import tz
from db import connect
import send_due, mailer

TODAY = tz.today()
WINDOW = 7                                   # days before the show, inclusive of show day (0..7)
CRM_BASE = "https://crm.thesimonshow.com"
SMS_ADDR = "7324926071@vtext.com"
OWNER = "simon@thesimonshow.com"

def _numlim(x):
    try: return float(x)
    except (TypeError, ValueError): return 0.0

def main():
    send = "--send" in sys.argv
    c = connect(); cur = c.cursor()
    cols = ["id","stage","show_date","deal_name","primary_contact_id",
            "require_trivia","trivia_received_at","photo_goh_limit","photo_guest_limit","photos_received_at",
            "show_time","venue_address","occasion","company","guest_of_honor","audience_details","show_format",
            "amount","deposit_amount","balance_amount","event_type","customize_token","performer_id",
            "proposal_link","deposit_status"]
    cur.execute("select "+",".join(cols)+" from deals "
                "where stage='closed_won' and show_date is not null "
                "and show_date >= %s and show_date <= %s",
                (TODAY, TODAY + datetime.timedelta(days=WINDOW)))
    deals = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.execute("select id,first_name,last_name,full_name,email,phone_mobile,phone_other from contacts")
    CB = {r[0]: dict(zip(["id","first_name","last_name","full_name","email","phone_mobile","phone_other"], r))
          for r in cur.fetchall()}
    send_due.PERF = {}                        # merge_values reads PERF for PerformerName (unused here)
    c.close()

    alerts = []
    for d in deals:
        trivia_needed = (d.get("require_trivia") is not False) and not d.get("trivia_received_at")
        photos_needed = ((_numlim(d.get("photo_goh_limit")) > 0 or _numlim(d.get("photo_guest_limit")) > 0)
                         and not d.get("photos_received_at"))
        if not (trivia_needed or photos_needed):
            continue
        contact = CB.get(d.get("primary_contact_id")) or {}
        V = send_due.merge_values(d, contact)
        miss = []
        if trivia_needed: miss.append("trivia")
        if photos_needed: miss.append("photos")
        alerts.append({
            "id": d["id"],
            "name": V.get("ClientFullName") or contact.get("full_name") or d.get("deal_name") or "client",
            "phone": V.get("ClientPhone") or "(no phone on file)",
            "email": V.get("ClientEmail") or "(no email on file)",
            "showdate": V.get("ShowDate") or str(d.get("show_date")),
            "days": (d["show_date"] - TODAY).days,
            "missing": miss,
        })
    alerts.sort(key=lambda a: (a["days"], a["name"]))

    print(f"{TODAY}: {len(alerts)} VIP-customization alert(s) (closed_won shows 0-{WINDOW}d out, trivia/photos outstanding)")
    for a in alerts:
        print(f"  {a['name'][:26]:26s} in {a['days']}d  missing: {', '.join(a['missing'])}  {a['phone']}")
    if not alerts:
        print("nothing outstanding - no alerts sent.")
        return

    for a in alerts:
        miss_txt = " and ".join(a["missing"])                     # "trivia and photos" / "trivia" / "photos"
        dsc = "TODAY" if a["days"] == 0 else ("TOMORROW" if a["days"] == 1 else f"in {a['days']} days")
        subj = f"URGENT VIP customizations missing for {a['name']} / {a['showdate']}"
        url = f"{CRM_BASE}/?deal={a['id']}"
        body = (
            '<div style="font-family:Verdana,Arial,sans-serif;font-size:15px;color:#202124">'
            '<h2 style="margin:0 0 6px;color:#c0392b">&#9888;&#65039; VIP customizations still missing</h2>'
            f'<p style="margin:0 0 14px">The show is <b>{dsc}</b> ({html.escape(a["showdate"])}) and you still '
            f'do not have the <b>{html.escape(miss_txt)}</b> you requested.</p>'
            '<table style="border-collapse:collapse;font-size:15px;margin:0 0 14px">'
            f'<tr><td style="padding:2px 14px 2px 0;color:#5f6368">Client</td><td><b>{html.escape(a["name"])}</b></td></tr>'
            f'<tr><td style="padding:2px 14px 2px 0;color:#5f6368">Phone</td><td>{html.escape(a["phone"])}</td></tr>'
            f'<tr><td style="padding:2px 14px 2px 0;color:#5f6368">Email</td><td>{html.escape(a["email"])}</td></tr>'
            f'<tr><td style="padding:2px 14px 2px 0;color:#5f6368">Missing</td>'
            f'<td style="color:#c0392b"><b>{html.escape(miss_txt)}</b></td></tr>'
            '</table>'
            f'<a href="{html.escape(url)}" style="display:inline-block;background:#c0392b;color:#fff;'
            'text-decoration:none;font-weight:bold;padding:9px 18px;border-radius:8px">Open the deal</a>'
            '<p style="color:#9aa0a6;font-size:12px;margin-top:14px">You get this every day until the '
            'resources arrive or the show passes.</p></div>'
        )
        sms = (f"URGENT: missing {miss_txt} for {a['name']} ({dsc}, {a['showdate']}). "
               f"Ph {a['phone']} / {a['email']}")
        if send:
            r = mailer.send_email(OWNER, subj, body, owner=True)
            mailer.send_email(SMS_ADDR, subj, sms, owner=True)
            print(f"  sent -> {a['name']}  (email {r['routed_to']} + sms)")
        else:
            print(f"  (dry-run) would email+text: {subj}")
    if not send:
        print("(dry-run; pass --send to email+text)")

if __name__ == "__main__":
    main()
