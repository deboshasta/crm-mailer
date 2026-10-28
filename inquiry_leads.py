# -*- coding: utf-8 -*-
"""Daily 7am reminder: for every deal still in the 'inquiry' stage, tell Simon
   "<Client> <ShowDate> is still in leads" with a link straight to the deal.

⚠⚠ AS OF 2026-10-28 THIS IS A PIE CONTROL BUBBLE AND NOT AN EMAIL. Every lead is published
   to the phone feed; the email is sent ONLY if that publish failed, or if the
   `pie_suppress_approvals` kill switch is off. See the long comment at the publish site for why
   this kind, alone among the published kinds, needs no ack-based email fallback.

Dry-run by default; pass --send to actually send. Guarded to send at most ONCE
PER DAY per deal (a bookkeeping key in cue_state), so re-running is safe.
Reminders stop automatically as soon as the deal leaves the inquiry stage.
"""
import sys, json, html, datetime
from db import connect
import mailer
import pie
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
    due=[]; standing=[]      # `standing` ignores the once-a-day guard - see below
    for (did, show_date, occasion, cue_state, who) in rows:
        st = cue_state or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        who=(who or "Unnamed lead").strip().title()
        when=short_date(show_date)
        subj=f"{who} {when} is still in leads"
        crm=f"{CRM_BASE}/?deal={did}"
        # ⚠ The phone's list is the STANDING condition - every deal still sitting in `inquiry` -
        #   taken BEFORE the once-a-day guard, which is about not emailing twice in one day and not
        #   about the lead having been dealt with. Publishing from `due` would delete the bubble a
        #   few hours after the morning email while the lead was still untouched. Same reasoning as
        #   likely_no_nag.
        standing.append((did, who, when, crm))
        if (st.get(GUARD_KEY) or {}).get("pinged")==TODAY: continue   # already reminded today
        due.append((did, subj, body_html(who, when, occasion, crm), st))
    # ── PIE CONTROL (step 5) ── a lead sitting in `inquiry` is a STANDING condition, so this is
    # published and reconciled exactly like the other nags, not fired once like the event kinds.
    _pie = [{"id": pie.item_id("leads", did), "title": who, "deal_id": did,
             "sub": "still in leads - %s" % when, "url": crm} for (did, who, when, crm) in standing]
    _published = pie.publish(c, "leads", _pie)
    # ⚠⚠ LEADS NEVER EMAIL - BUBBLE ONLY (Simon 2026-10-28: "this kind of email should go to
    #    pie control as a bubble - never email"). This is the ONE published kind with no ack-based
    #    email fallback, and four things make that safe rather than a hole:
    #    1. A lead is a STANDING condition that publish() RECONCILES on every run, so the bubble
    #       cannot be lost the way an event's single firing can. If the phone misses it today it is
    #       still in the feed tomorrow, and it disappears by itself the moment the deal leaves
    #       `inquiry`.
    #    2. It is not the only surface. An `inquiry` deal already satisfies isFlashing(), so it has
    #       a red card and a shield count in the CRM, and `v_inquiry > 0` is a term of
    #       approve_counts()'s own `trigger` AND its due_deals union - so it puts a ghost on the
    #       screen too. Email was the fourth place he was told.
    #    3. A new web inquiry ALSO gets an instant one-shot bubble from submit_booking's
    #       pie_alert(), so the phone hears about it the second it arrives, not at the next 7am.
    #    4. ⚠⚠ THE ACK FALLBACK COULD NEVER HAVE WORKED HERE ANYWAY, which is what produced the
    #       email he complained about. The item is PUBLISHED and the email DECIDED in the same run,
    #       so no ack can possibly exist yet - pie.unseen() therefore returned every brand-new lead
    #       and emailed it. *Measured: Rayner Abreu's row was created 21:43 UTC on 2026-10-27 and
    #       acked at 23:51 UTC; the email went out in between.* EVERY new lead got exactly one
    #       email, by construction, and no window or ack could have stopped it.
    # ⚠ It is still gated on the SAME kill switch as everything else (pie.suppress_on). Turn
    #   `pie_suppress_approvals` off and this goes straight back to emailing, which is the whole
    #   point of having one switch.
    # ⚠ And it is gated on the PUBLISH HAVING SUCCEEDED. publish() swallows its own errors by
    #   design, so without that test a failed feed write would mean no bubble AND no email - the
    #   one genuinely silent outcome. Simon's own rule: "email is a backup for if it fails to add
    #   to pie control cue". So the only email left from here is a delivery failure, not a nag.
    # ⚠ The guard key is deliberately NOT stamped when suppressing - nothing was sent, so a
    #   later run with the switch off must still be free to send.
    if _published and pie.suppress_on(c):
        if due:
            print("pie: %d lead reminder(s) are bubble-only - not emailing" % len(due))
        due = []
    else:
        _keep = pie.unseen(c, "leads", [pie.item_id("leads", t[0]) for t in due])
        due = [t for t in due if pie.item_id("leads", t[0]) in _keep]
    if SEND and _pie:
        pie.doorbell(c, "CRM: leads waiting", "%d lead%s still sitting in inquiries - open Pie Control"
                     % (len(_pie), "" if len(_pie) == 1 else "s"))

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
