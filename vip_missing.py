# -*- coding: utf-8 -*-
"""Daily VIP-customization reminder — "have YOU done the trivia / picture customizations?"

For every Closed-Won show, on a set of reminder days before it, email Simon if trivia and/or pictures
were REQUESTED from the client but he has not yet marked them DONE (prepared by him). One combined
email per show, with a button to mark each outstanding item done (trivia / pictures / both). The body
also shows, for each requested item, whether the CLIENT has sent their materials yet (received), so
Simon knows if he's even able to do it.

Two distinct states per item (do not conflate):
  * requested  — Simon asked the client for it (require_trivia; photo_goh/guest_limit > 0)
  * received   — the CLIENT submitted it            (trivia_received_at / photos_received_at)
  * done       — SIMON prepared the customization    (trivia_done_at / photos_done_at)  <- what this chases
The mark-done buttons hit vercel-send/api/approve-email.js (?a=markdone; auth = the deal's
customize_token) — folded there rather than its own function because Vercel Hobby caps 12 per deploy.

Reminder days before the show: 6 weeks (42d), 4 weeks (28d), 10 days, 7 days, then EVERY day from 6
days out down to and including show day (0). Nothing after the show. An item drops off the moment it
is marked done. On the final week (0-7 days) an extra URGENT text also goes to Simon's phone
(7324926071@vtext.com) — a phone can't render buttons, so its SUBJECT carries the alert.

"Requested" reuses send_due.py's customization_request predicate (require_trivia is not False; a photo
limit > 0). If that predicate changes there, change it here too.

Runs ONCE a day from vip-missing.yml. Dry-run by default; --send emails+texts. Owner=True on every
send (no safe-mode reroute). The email to Simon is auto-tagged centrally; the vtext line is not.
"""
import sys, html, datetime
import tz
from db import connect
import send_due, mailer
import pie

TODAY = tz.today()
# reminder days before the show: 6w, 4w, 10d, 7d, then daily 6..0. (set for O(1) lookup)
REMINDER_DAYS = {42, 28, 10} | set(range(0, 8))   # {0,1,2,3,4,5,6,7,10,28,42}
MAX_LOOKAHEAD = 42                                  # furthest reminder = 6 weeks out
# Simon 2026-10-25: "give me a reminder to call / text people if they are within 5 days and i
# haven't gotten the customizations ... make sure to include phone number", and, on how it should
# sit beside this email: "the call reminder replaces it inside 5 days."
# So within CALL_MAX_DAYS there is NO email - just one raised bubble carrying the phone number.
# Days 6 and 7 keep the email exactly as before. Two things saying the same thing on the same day
# is how you stop reading both, and at this range the useful act is a phone call, not another note
# in an inbox.
# ⚠ This REPLACES URGENT_SMS_MAX_DAYS (7), the window the retired Verizon SMS gateway used. The
#   escalation bubble no longer fires at 6-7 days: there the email is the signal.
CALL_MAX_DAYS = 5
CRM_BASE = "https://crm.thesimonshow.com"
MARKDONE_BASE = "https://crm-send-the-simon-show.vercel.app/api/approve-email?a=markdone"  # folded into approve-email (Hobby 12-fn cap)
# (the Verizon SMS gateway is gone - replaced by a pie_alert bubble, 2026-10-25)
OWNER = "simon@thesimonshow.com"

def _numlim(x):
    try: return float(x)
    except (TypeError, ValueError): return 0.0

def _btn(url, text, bg):
    return (f'<a href="{html.escape(url)}" style="display:inline-block;background:{bg};color:#fff;'
            'text-decoration:none;font-weight:bold;padding:10px 18px;border-radius:8px;margin:0 8px 8px 0">'
            f'{html.escape(text)}</a>')

def main():
    send = "--send" in sys.argv
    c = connect(); cur = c.cursor()
    cols = ["id","stage","show_date","deal_name","primary_contact_id","customize_token",
            "require_trivia","trivia_received_at","trivia_done_at",
            "photo_goh_limit","photo_guest_limit","photos_received_at","photos_done_at",
            "show_time","venue_address","occasion","company","guest_of_honor","audience_details","show_format",
            "amount","deposit_amount","balance_amount","event_type","performer_id","proposal_link","deposit_status",
            "cue_state"]   # cue_state only for _custsnooze (see below)
    cur.execute("select "+",".join(cols)+" from deals "
                "where stage='closed_won' and show_date is not null "
                "and show_date >= %s and show_date <= %s",
                (TODAY, TODAY + datetime.timedelta(days=MAX_LOOKAHEAD)))
    deals = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.execute("select id,first_name,last_name,full_name,email,phone_mobile,phone_other from contacts")
    CB = {r[0]: dict(zip(["id","first_name","last_name","full_name","email","phone_mobile","phone_other"], r))
          for r in cur.fetchall()}
    send_due.PERF = {}
    # c stays OPEN: pie.publish/pie.unseen need it. Closed at every exit below.

    alerts = []
    # ⚠⚠ `standing` is EVERY deal with an outstanding customization; `alerts` is the subset on a
    #    REMINDER DAY. They were the same list, gated by `days not in REMINDER_DAYS` right here, and
    #    that was fine while the email was the only output. It is wrong now that the phone bubble is
    #    the reminder: the feed would carry the item on day 42, drop it on day 41, and bring it back
    #    on day 28 - a bubble that vanishes while the work is still outstanding. REMINDER_DAYS is
    #    about how often to EMAIL him, not about whether the work exists.
    standing = []
    for d in deals:
        days = (d["show_date"] - TODAY).days
        if days < 0:
            continue                       # the show has been and gone; nothing to bubble
        trivia_req = (d.get("require_trivia") is not False)
        photos_req = (_numlim(d.get("photo_goh_limit")) > 0 or _numlim(d.get("photo_guest_limit")) > 0)
        trivia_out = trivia_req and not d.get("trivia_done_at")     # requested & not done by Simon
        photos_out = photos_req and not d.get("photos_done_at")
        # "Stop reminding me about pictures" on this deal (cue_state._custwaive.photos, Simon
        # 2026-10-23). BOTH flags are cleared, not just _out: leaving photos_req true would keep
        # printing a "requested / NOT done" row in an email that is no longer about pictures.
        # A deal whose TRIVIA is still outstanding still gets its email - this silences one half.
        # Unlike _custsnooze below this has no expiry and no 4-day floor, because it means the
        # pictures are not happening for this gig at all. Keep it in step with custOutstanding()
        # in app.js and the customizations counter in approve_counts.sql.
        if ((d.get("cue_state") or {}).get("_custwaive") or {}).get("photos"):
            photos_req = False
            photos_out = False
        if not (trivia_out or photos_out):
            continue
        # Snoozed from the CRM (cue_state._custsnooze.until). Muting the board but still mailing
        # would make the snooze useless -- the emails ARE the warning being snoozed. The app will
        # not let a snooze run closer than 4 days to the show, so this cannot silence the final
        # run-up. Any unparseable value is ignored, i.e. the reminder still goes out.
        _sn = ((d.get("cue_state") or {}).get("_custsnooze") or {}).get("until")
        if _sn:
            try:
                if datetime.datetime.fromisoformat(str(_sn).replace("Z", "+00:00")) > datetime.datetime.now(datetime.timezone.utc):
                    continue
            except (ValueError, TypeError):
                pass
        contact = CB.get(d.get("primary_contact_id")) or {}
        V = send_due.merge_values(d, contact)
        _row = {
            "id": d["id"], "token": d.get("customize_token") or "",
            "name": V.get("ClientFullName") or contact.get("full_name") or d.get("deal_name") or "client",
            "phone": V.get("ClientPhone") or "(no phone on file)",
            "email": V.get("ClientEmail") or "(no email on file)",
            "showdate": V.get("ShowDate") or str(d.get("show_date")),
            "days": days,
            "trivia_req": trivia_req, "trivia_out": trivia_out, "trivia_recv": bool(d.get("trivia_received_at")),
            "photos_req": photos_req, "photos_out": photos_out, "photos_recv": bool(d.get("photos_received_at")),
        }
        standing.append(_row)
        if days in REMINDER_DAYS:
            alerts.append(_row)            # the EMAIL cadence; the bubble above is continuous
    alerts.sort(key=lambda a: (a["days"], a["name"]))
    standing.sort(key=lambda a: (a["days"], a["name"]))

    # ── PIE CONTROL (step 4) ──────────────────────────────────────────────────────────────────
    # Published rather than derived: the qualifying set above is REMINDER_DAYS x snooze x waive x
    # requested/received/done, and restating that in SQL would be a second rulebook.
    # ⚠ Published even when `alerts` is EMPTY, and that is the important case - publish()
    #   reconciles, so an empty set is what DELETES yesterday's bubbles. Returning early before
    #   this would leave them on the phone for ever.
    _pie = [{"id": pie.item_id("cust", a["id"]), "title": a["name"], "deal_id": a["id"],
             "sub": "needs " + " and ".join([w for w, on in (("trivia", a["trivia_out"]),
                                                             ("pictures", a["photos_out"])) if on])
                    + (" - show TODAY" if a["days"] == 0 else " - show in %dd" % a["days"]),
             "url": f"{CRM_BASE}/?deal={a['id']}"} for a in standing]
    pie.publish(c, "cust", _pie)
    _pie_email = pie.unseen(c, "cust", [i["id"] for i in _pie])

    print(f"{TODAY}: {len(standing)} deal(s) with an outstanding customization; "
          f"{len(alerts)} on a reminder day (closed_won, requested & not done)")
    for a in alerts:
        out = [w for w, on in (("trivia", a["trivia_out"]), ("photos", a["photos_out"])) if on]
        print(f"  {a['name'][:26]:26s} in {a['days']}d  to-do: {', '.join(out)}  {a['phone']}")
    if not alerts:
        print("nothing outstanding on a reminder day - no alerts sent.")
        c.close()
        return
    if send and _pie:
        pie.doorbell(c, "CRM: customizations to do", "%d show%s need customizations - open Pie Control"
                     % (len(_pie), "" if len(_pie) == 1 else "s"))

    def _status_row(label, requested, received, done):
        if not requested: return ""
        recv = '<span style="color:#3ecf8e">received ✓</span>' if received else '<span style="color:#e0a92e">not received yet</span>'
        dn = '<span style="color:#3ecf8e">done ✓</span>' if done else '<span style="color:#c0392b;font-weight:bold">NOT done</span>'
        return (f'<tr><td style="padding:3px 14px 3px 0;color:#5f6368">{html.escape(label)}</td>'
                f'<td>requested &middot; {recv} &middot; {dn}</td></tr>')

    for a in alerts:
        dsc = "TODAY" if a["days"] == 0 else ("TOMORROW" if a["days"] == 1 else f"in {a['days']} days")
        todo = [w for w, on in (("trivia", a["trivia_out"]), ("picture customization", a["photos_out"])) if on]
        todo_txt = " and ".join(todo)
        subj = f"URGENT VIP customizations missing for {a['name']} / {a['showdate']}"
        deal_url = f"{CRM_BASE}/?deal={a['id']}"

        # mark-done buttons — only for outstanding (requested & not done) items; "BOTH" only if both are
        buttons = ""
        if a["token"]:
            if a["trivia_out"]:
                buttons += _btn(f"{MARKDONE_BASE}&t={a['token']}&which=trivia", "CLICK HERE to confirm you did trivia", "#1f8f5f")
            if a["photos_out"]:
                buttons += _btn(f"{MARKDONE_BASE}&t={a['token']}&which=photos", "CLICK HERE to confirm you did picture customization", "#1f8f5f")
            if a["trivia_out"] and a["photos_out"]:
                buttons += _btn(f"{MARKDONE_BASE}&t={a['token']}&which=both", "CLICK HERE to confirm you did BOTH", "#0b5")
        else:
            buttons = (f'<a href="{html.escape(deal_url)}" style="color:#1155cc;font-weight:bold">Open the deal to mark it done</a>'
                       ' (no customize link on this deal yet)')

        body = (
            '<div style="font-family:Verdana,Arial,sans-serif;font-size:15px;color:#202124">'
            '<h2 style="margin:0 0 6px;color:#c0392b">&#9888;&#65039; VIP customizations you still need to DO</h2>'
            f'<p style="margin:0 0 12px">The show is <b>{dsc}</b> ({html.escape(a["showdate"])}) and you have not '
            f'marked the <b>{html.escape(todo_txt)}</b> done.</p>'
            '<table style="border-collapse:collapse;font-size:15px;margin:0 0 12px">'
            f'<tr><td style="padding:3px 14px 3px 0;color:#5f6368">Client</td><td><b>{html.escape(a["name"])}</b></td></tr>'
            f'<tr><td style="padding:3px 14px 3px 0;color:#5f6368">Phone</td><td>{html.escape(a["phone"])}</td></tr>'
            f'<tr><td style="padding:3px 14px 3px 0;color:#5f6368">Email</td><td>{html.escape(a["email"])}</td></tr>'
            + _status_row("Trivia", a["trivia_req"], a["trivia_recv"], not a["trivia_out"])
            + _status_row("Pictures", a["photos_req"], a["photos_recv"], not a["photos_out"])
            + '</table>'
            f'<div style="margin:4px 0 12px">{buttons}</div>'
            f'<a href="{html.escape(deal_url)}" style="color:#5f6368;font-size:13px;text-decoration:underline">Open the deal</a>'
            '<p style="color:#9aa0a6;font-size:12px;margin-top:12px">You get this at 6 weeks, 4 weeks, 10 days and '
            '7 days out. Inside five days this email stops and your phone gets a CALL reminder instead, '
            'with their number on it — until you mark each requested item done.</p></div>'
        )
        # ⚠ Checked OUTSIDE `if send`, so a DRY RUN reports what it would really do. With it
        #   inside, a suppressed item still printed "(dry-run) would email ..." - a dry run that
        #   lies about the one decision being tested is worse than no dry run.
        # ⚠ The SMS goes with the email, not separately: both say the same thing, so letting the
        #   text through while suppressing the mail would defeat the whole point.
        # ⚠⚠ THE CALL PHASE IS DECIDED BEFORE THE SEEN-ON-THE-PHONE TEST, and the order matters.
        #    That test exists to stop an EMAIL repeating something already bubbled; inside five
        #    days there is no email to suppress, and skipping here would drop the call reminder
        #    too - the one signal that is supposed to survive.
        call_phase = a["days"] <= CALL_MAX_DAYS
        if not call_phase and pie.item_id("cust", a["id"]) not in _pie_email:
            print(f"  seen on the phone - not emailing: {a['name']}")
            continue
        if send and call_phase:
            # CALL THEM. One raised bubble carrying the number, and no email at all.
            # !! Keyed on the deal AND the show date, so the run-up raises exactly one bubble per
            #    gig however many times this runs - it is `once`, so trashing it is final.
            # !! raise=true: this is the escalation, and the standing `cust` bubble has been on
            #    screen for weeks by now and is easy to stop seeing.
            # !! COMMIT: this script does NOT set autocommit (likely_no_nag does; they differ), so
            #    without this the insert is rolled back when the connection closes and the bubble
            #    silently never appears.
            cur.execute("select public.pie_alert(%s,%s,%s,%s,%s,%s)",
                        ("vipcall_%s_%s" % (a["id"], a["showdate"]),
                         "CALL %s" % a["name"],
                         "%s - still need %s (%s)" % (a["phone"], todo_txt, dsc),
                         a["id"], f"{CRM_BASE}/?deal={a['id']}", True))
            c.commit()
            print(f"  CALL phase ({a['days']}d) -> bubble only, no email: {a['name']} {a['phone']}")
        elif send:
            r = mailer.send_email(OWNER, subj, body, owner=True)
            note = "email " + r["routed_to"]
            print(f"  sent -> {a['name']}  ({note})")
        elif call_phase:
            print(f"  (dry-run) CALL phase ({a['days']}d) would bubble {a['phone']}, no email: {a['name']}")
        else:
            print(f"  (dry-run) would email: {subj}")
    if not send:
        print("(dry-run; pass --send to email+bubble)")
    c.close()

if __name__ == "__main__":
    main()
