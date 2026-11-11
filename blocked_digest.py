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
import pie

TODAY = datetime.date.today()
CRM_BASE = "https://crm.thesimonshow.com"

def main():
    c = connect(); cur = c.cursor()
    cols = ["id","stage","show_date","show_time","venue_address","occasion","company","guest_of_honor",
            "proposal_link","audience_details","show_format","amount","deposit_amount","balance_amount",
            "event_type","is_repeat","customize_token","performer_id","proposal_sent_at",
            "deal_name","cue_state","stage_changed_at","created_at","primary_contact_id"]
    # EGRESS: cue_state is the fat part of a deal row (~1.5 MB of `body` text across the table) and
    # NOTHING in this script reads an entry's body - send_due.missing_fields() only asks whether the
    # key EXISTS (send_due.py:407, "a hand-written email is assumed complete"). So fetch every entry
    # whole but with each STRING body replaced by "". Same keys, same truthiness, a fraction of the
    # bytes. Part of the 2026-11-10 egress work (6.22 GB against the Free plan's 5 GB).
    # ⚠ jsonb_typeof(...)='string', not `? 'body'`: an explicit "body": null must STAY null, or
    #   `e.get("body") is not None` flips to True and the entry silently reads as complete.
    # ⚠ If you ever need the real body text here, read it from public.sent_emails by
    #   (deal_id, cue_key). Do NOT undo this and refetch the column whole.
    CUE_SLIM = ("(select jsonb_object_agg(k, case when jsonb_typeof(v->'body')='string' "
                "then jsonb_set(v,'{body}','\"\"'::jsonb) else v end) "
                "from jsonb_each(cue_state) x(k,v)) as cue_state")
    _sel = ",".join(CUE_SLIM if _c == "cue_state" else _c for _c in cols)
    # EGRESS: and only the deals that can possibly produce a row below. The loop keeps an entry
    # only when it is blocked and neither sent nor cancelled, so the same test in SQL turns a
    # 579-row fetch into a handful. Deliberately permissive in the SAFE direction: `v ? 'blocked'`
    # matches send_due's own _sweep_where, and an extra row costs one skipped loop iteration while
    # a missing one would silently drop a paused email out of the nag.
    _blocked_where = """
        cue_state is not null
        and exists (select 1 from jsonb_each(cue_state) _x(k,v)
                     where jsonb_typeof(v) = 'object'
                       and v ? 'blocked'
                       and coalesce(v->>'sent','') = ''
                       and coalesce(v->>'cancelled','') in ('','false'))
    """
    cur.execute("select "+_sel+" from deals where "+_blocked_where)
    deals = [dict(zip(cols,r)) for r in cur.fetchall()]
    cur.execute("select id,first_name,last_name,full_name,email,phone_mobile,phone_other from contacts")
    CB = {r[0]:dict(zip(["id","first_name","last_name","full_name","email","phone_mobile","phone_other"],r)) for r in cur.fetchall()}
    cur.execute("select id, first_name, full_name from performers")
    send_due.PERF = {r[0]:{"first_name":r[1],"full_name":r[2]} for r in cur.fetchall()}
    cur.execute("select key,subject,body_html from templates where active=true")
    TPL = {r[0]:{"subject":r[1],"body":r[2]} for r in cur.fetchall()}
    # c stays OPEN: pie.publish/pie.unseen need it. Closed at every exit below.

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

    # ── PIE CONTROL (step 4) ──────────────────────────────────────────────────────────────────
    # Published, not derived: `blanks` comes from send_due.missing_fields(template, entry, merge
    # values), which is template-aware and has no SQL equivalent.
    # ⚠ One item per PAUSED EMAIL, not per deal - a deal can have two paused for different
    #   reasons, and they are fixed separately.
    _pie = [{"id": pie.item_id("blocked", did, key), "title": nm, "deal_id": did,
             "sub": f"{key} paused - missing {', '.join(blanks)}",
             "url": f"{CRM_BASE}/?deal={did}&fixemail={key}"} for (nm, key, blanks, did) in rows]
    pie.publish(c, "blocked", _pie)
    # ⚠⚠ THIS IS A DIGEST, so the gate FILTERS THE ROWS rather than deciding one send.
    #    Anything already seen on the phone drops out of the email, and if every one of them has
    #    been seen the email does not go at all - which is the behaviour that actually quietens
    #    the inbox. A digest that still lists what he has already dealt with buys nothing.
    _keep = pie.unseen(c, "blocked", [i["id"] for i in _pie])
    rows = [r for r in rows if pie.item_id("blocked", r[3], r[1]) in _keep]

    print(f"{TODAY}: {len(rows)} paused email(s) waiting on missing fields")
    for nm, key, blanks, did in rows:
        print(f"  {nm[:26]:26s} [{key}] missing: {', '.join(blanks)}")

    if not rows:
        print("nothing paused - no nag sent (or all of them are on the phone already).")
        c.close()
        return
    if "--send" in sys.argv and _pie:
        pie.doorbell(c, "CRM: emails paused", "%d email%s paused for missing info - open Pie Control"
                     % (len(_pie), "" if len(_pie) == 1 else "s"))

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
    c.close()

if __name__ == "__main__":
    main()
