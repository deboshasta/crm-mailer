# -*- coding: utf-8 -*-
"""Minute-precise booking sends. Currently: magic_castle goes out 10 minutes after a
deal's deposit is marked paid (deals.deposit_paid_at). Run FREQUENTLY (e.g. every 5 min).
Dry-run by default; pass --send to actually send. Safe-mode still routes every send to
Simon until mail_safe_mode is off. Go-live cutoff (private.config.sequencer_start)
prevents ever back-firing on historical deals.
"""
import sys, json, datetime
from db import connect
import send_due, mailer, attachments

DELAY = datetime.timedelta(minutes=10)

def _ts(v):
    """Parse a timestamptz into an aware UTC datetime."""
    if not v:
        return None
    if isinstance(v, datetime.datetime):
        return v if v.tzinfo else v.replace(tzinfo=datetime.timezone.utc)
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None

def compute_due(deals, CB, TPL, sig, now, seq_start):
    """Pure selection logic (testable): which deals are due for a magic_castle send now."""
    due = []
    for d in deals:
        if d.get("deposit_status") != "paid":
            continue
        cd = send_due._d(d.get("created_at"))
        if cd and cd < seq_start:                      # go-live cutoff: never back-fire
            continue
        paid = _ts(d.get("deposit_paid_at"))
        if not paid or now < paid + DELAY:             # not yet 10 minutes since paid
            continue
        st = d.get("cue_state") or {}
        if isinstance(st, str):
            st = json.loads(st or "{}")
        e = st.get("magic_castle") or {}
        if e.get("sent") or e.get("cancelled"):
            continue
        contact = CB.get(d.get("primary_contact_id")) or {}
        if not contact.get("email"):
            continue
        t = TPL.get("magic_castle")
        if not t:
            continue
        V = send_due.merge_values(d, contact)
        subj = e["subject"] if e.get("subject") is not None else send_due.fill_subject(t["subject"], V)
        body = e["body"] if e.get("body") is not None else send_due.render_html(t["body"], V, sig)
        atts = attachments.attachments_for("magic_castle", d, contact)
        due.append((d, contact["email"], subj, body, atts, st))
    return due

def main():
    send = "--send" in sys.argv
    now = datetime.datetime.now(datetime.timezone.utc)
    c = connect(); c.autocommit = True; cur = c.cursor()
    cols = ["id","stage","deposit_status","deposit_paid_at","show_date","show_time","venue_address",
            "occasion","company","guest_of_honor","proposal_link","audience_details","show_format",
            "amount","deposit_amount","balance_amount","event_type","deal_name","cue_state",
            "created_at","primary_contact_id"]
    cur.execute("select " + ",".join(cols) + " from deals where deposit_status='paid' and deposit_paid_at is not null")
    deals = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.execute("select id,first_name,full_name,email,phone_mobile,phone_other from contacts")
    CB = {r[0]: dict(zip(["id","first_name","full_name","email","phone_mobile","phone_other"], r)) for r in cur.fetchall()}
    cur.execute("select key,subject,body_html from templates where active=true")
    TPL = {r[0]: {"subject": r[1], "body": r[2]} for r in cur.fetchall()}
    sig = TPL.get("_signature", {}).get("body", "")
    cur.execute("select value from private.config where key='sequencer_start'")
    _r = cur.fetchone(); seq_start = datetime.date.fromisoformat(_r[0]) if _r and _r[0] else datetime.date.today()

    due = compute_due(deals, CB, TPL, sig, now, seq_start)
    print(f"{now.isoformat()}  -  {len(due)} magic_castle send(s) due  (mode: {'SEND' if send else 'DRY-RUN'})")
    for (d, to, subj, body, atts, st) in due:
        print(f"  -> {to}  |  magic_castle  |  {subj[:60]}  |  att={[a[0] for a in atts]}")
        if send:
            mailer.send_email(to, subj, body, attachments=atts)
            st["magic_castle"] = {**(st.get("magic_castle") or {}), "sent": now.isoformat()}
            cur.execute("update deals set cue_state=%s where id=%s", (json.dumps(st), d["id"]))
    if send and due:
        print("marked sent + saved.")
    c.close()

if __name__ == "__main__":
    main()
