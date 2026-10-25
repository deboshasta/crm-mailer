#!/usr/bin/env python3
"""Shadow check for the Pie Control feed (step 1 of briefs/pie-control-queue.md).

Compares the LIVE `?a=piefeed` payload against the approval queue computed straight from the
database, and alerts if they disagree. READ-ONLY on both sides: it changes nothing, sends no
client mail, and exists only so the feed can be trusted before anything starts suppressing email.

⚠⚠ IT RUNS IN THE CLOUD, NOT ON THE PC AND NOT ON THE PHONE.
   Simon 2026-10-24: "windows is sometimes off", "my phone is always on". The phone was the
   obvious suggestion and is the wrong home: the whole point of the shadow is that it re-derives
   the rule INDEPENDENTLY, so implementing it on the handset would mean a THIRD copy of
   approvalPending() (after app.js and the pie_approvals RPC), in Kotlin. Two copies drifting is
   what produced three production bugs on the day this was written. It would also put database
   credentials on the phone. GitHub Actions is more reliably "always on" than either machine, and
   already holds the DB secrets.

⚠ The SQL below deliberately restates the predicate rather than calling public.pie_approvals().
  Comparing the function against itself would pass no matter what the function did.

⚠ It mirrors app.js's approvalPending(), which is what the browser queue shows:
      pending_approval AND NOT cancelled AND NOT sent AND NOT _sending AND snooze not in future
  No 4am-ET gate - that belongs to the overnight nag, not the queue.

⚠ A DISAGREEMENT writes one error_log row, which an AFTER INSERT trigger turns into a phone push
  and an AHK dialog. At most ONE per calendar day: a systematic disagreement would otherwise fire
  every half hour, and a nag that is always on is one you stop reading.

Exit 0 = agree, 1 = disagree. Prints a one-line verdict either way for the Actions log.
"""
import datetime
import json
import os
import sys
import urllib.request

from db import connect

FEED = "https://crm-send-the-simon-show.vercel.app/api/approve-email?a=piefeed"

SQL = """
select 'appr_' || d.id::text || '_' || e.key as id,
       coalesce(nullif(btrim(c.full_name), ''), 'Client') as title,
       coalesce(nullif(btrim(e.val->>'subject'), ''), '(no subject)') as sub
from deals d
left join contacts c on c.id = d.primary_contact_id
cross join lateral jsonb_each(d.cue_state) e(key, val)
where d.cue_state is not null
  and jsonb_typeof(e.val) = 'object'
  and (e.val->>'pending_approval') = 'true'
  and (e.val->>'sent') is null
  and (e.val->>'cancelled') is null
  and (e.val->>'_sending') is null
  and coalesce((e.val->>'snoozed_until')::numeric, 0) <= extract(epoch from now()) * 1000
"""


# How long an approval may sit UNSEEN by the phone before this alarms. See the block in main().
PIE_STALE_ALERT_H = 12


def feed_items(key):
    req = urllib.request.Request(FEED, headers={"x-bamfam-key": key, "User-Agent": "pie-shadow"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8", "replace")).get("items", [])


def main():
    key = (os.environ.get("BAMFAM_FEED_KEY") or "").strip()
    if not key:
        print("pie-shadow: BAMFAM_FEED_KEY is not set - cannot reach the feed")
        return 2                                  # not a disagreement; a setup problem

    # ⚠⚠ APPROVALS ONLY. As of step 4 the feed also carries cust / blocked / deposit /
    #    likelyno / inquiry items, and the SQL below restates the APPROVAL predicate alone - so
    #    without this filter every one of those would read as "only in feed" and the shadow would
    #    alert on every single run, immediately and for ever. The other kinds need no shadow:
    #    they are PUBLISHED by the very script that decides them, so there are no two copies to
    #    drift apart, which is the whole thing this check exists to catch.
    feed = {i["id"]: i for i in feed_items(key) if (i.get("kind") or "approval") == "approval"}

    conn = connect()
    cur = conn.cursor()
    cur.execute(SQL)
    rows = {r[0]: {"title": r[1], "sub": r[2]} for r in cur.fetchall()}

    only_feed = sorted(set(feed) - set(rows))
    only_db = sorted(set(rows) - set(feed))
    mismatched = sorted(
        i for i in (set(feed) & set(rows))
        if feed[i].get("title") != rows[i]["title"] or feed[i].get("sub") != rows[i]["sub"]
    )
    agree = not (only_feed or only_db or mismatched)

    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    print("%s  feed=%d db=%d  %s" % (stamp, len(feed), len(rows), "AGREE" if agree else "DISAGREE"))

    # ── ack health (step 2, briefs/pie-control-queue.md open risk 1) ─────────────────────────────
    # ⚠⚠ REPORTED, NEVER ALERTED. The phone being off, away or asleep is normal, so an alert here
    #    would fire most evenings - the same reasoning that keeps crm-suggest's last_suggest_run out
    #    of heartbeat.py. This line exists so that when acks DO stop, the Actions log already says
    #    when they stopped instead of the question starting from nothing.
    # ⚠ `acked` is read from the FEED's own seen_at, which is episode-scoped in pie_approvals(). It
    #   is deliberately NOT re-derived here: unlike the pending predicate (which this file restates
    #   on purpose, so the comparison is independent), the episode rule has exactly one home and a
    #   second copy would be a liability rather than a cross-check.
    try:
        n_acked = sum(1 for i in feed.values() if i.get("seen_at"))
        cur.execute("select value from private.config where key='last_pie_ack'")
        row = cur.fetchone()
        if row and row[0]:
            try:
                t = datetime.datetime.strptime(str(row[0])[:19], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=datetime.timezone.utc)
                age_h = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600.0
                print("  acks: %d/%d items seen; last ack %.1fh ago (%s)" % (n_acked, len(feed), age_h, row[0]))
            except Exception:
                print("  acks: %d/%d items seen; last ack stamp unparseable: %r" % (n_acked, len(feed), row[0]))
        else:
            print("  acks: %d/%d items seen; NO ack has ever been recorded "
                  "(expected until Pie Control's phone side is wired up)" % (n_acked, len(feed)))
    except Exception as e:
        print("  acks: could not read ack health: %s" % e)
    # ── stale-approval alarm (added 2026-10-25) ──────────────────────────────────────────────────
    # ⚠⚠ THIS IS THE REPLACEMENT FOR THE AUTHORIZE EMAIL, which stopped being sent the same day
    #    (Simon: "stop sending email approval requests to email, only to pie control"). With no
    #    email fallback, a phone that quietly stops polling means an approval is announced NOWHERE,
    #    so this is the one thing left that can say so. If it is ever removed, put the fallback in
    #    send_due.py back first.
    # ⚠⚠ IT ALARMS ON AN UNSEEN **APPROVAL**, NOT ON ACK HEALTH IN GENERAL. The block above is
    #    deliberately report-only because a phone that is off, away or asleep is normal; what is
    #    NOT normal is a client email waiting for approval that the phone has never once received.
    # ⚠ 12 HOURS, not one or six: the phone polls every 30 min and on unlock, but Android Doze can
    #   hold a background poll for most of a night, so a shorter window would alarm most mornings -
    #   the same false-alarm reasoning that keeps last_suggest_run out of heartbeat.py. 12h is
    #   longer than any overnight gap and still catches a real failure inside a day.
    # ⚠ It alarms through error_log -> phone push + AHK dialog, which is NOT email, so it respects
    #   the instruction it exists to make safe.
    try:
        stale = []
        for i in feed.values():
            if i.get("kind") != "approval" or i.get("seen_at"):
                continue
            q = i.get("pending_since")
            if not q:
                continue
            try:
                qt = datetime.datetime.strptime(str(q)[:10], "%Y-%m-%d").replace(
                    tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            age_h = (datetime.datetime.now(datetime.timezone.utc) - qt).total_seconds() / 3600.0
            if age_h >= PIE_STALE_ALERT_H:
                stale.append((i.get("id"), i.get("title"), age_h))
        if stale:
            print("  STALE APPROVALS (never reached the phone, and no email is sent any more):")
            for sid, title, age_h in stale:
                print("    %s  %s  unseen %.0fh" % (sid, title, age_h))
            summary = ("%d approval(s) have never reached Pie Control and no authorize email is "
                       "sent any more: %s" % (len(stale),
                       "; ".join("%s (unseen %.0fh)" % (t, a) for _, t, a in stale)))
            today = datetime.date.today().isoformat()
            cur.execute("select 1 from error_log where source=%s and ts::date = %s limit 1",
                        ("pie-approval-stale", today))
            if cur.fetchone():
                print("  (already alerted today - logged only)")
            else:
                cur.execute("insert into error_log (source, detail) values (%s, %s)",
                            ("pie-approval-stale", summary))
                conn.commit()
                print("  -> alerted via error_log")
    except Exception as e:                        # an observer must never be the thing that breaks
        print("  stale-approval check failed: %s" % e)

    for i in only_feed:
        print("  ONLY IN FEED (would bubble something not in the queue): %s" % i)
    for i in only_db:
        print("  ONLY IN DB (queue item that would never bubble): %s  %s | %s"
              % (i, rows[i]["title"], rows[i]["sub"][:60]))
    for i in mismatched:
        print("  TEXT DIFFERS: %s  feed=%r/%r  db=%r/%r"
              % (i, feed[i].get("title"), feed[i].get("sub"), rows[i]["title"], rows[i]["sub"]))
    if agree and not feed:
        print("  (empty on both sides - correct, but proves little; the useful runs are when work is queued)")

    if not agree:
        summary = ("Pie Control shadow DISAGREES: feed=%d db=%d; only_in_feed=%s; only_in_db=%s; "
                   "text_differs=%s" % (len(feed), len(rows), only_feed, only_db, mismatched))
        today = datetime.date.today().isoformat()
        try:
            cur.execute("select 1 from error_log where source=%s and ts::date = %s limit 1",
                        ("pie-shadow", today))
            if cur.fetchone():
                print("  (already alerted today - logged only)")
            else:
                cur.execute("insert into error_log (source, detail) values (%s, %s)",
                            ("pie-shadow", summary))
                conn.commit()
                print("  -> alerted via error_log")
        except Exception as e:                    # an observer must never be the thing that breaks
            print("  -> alert failed: %s" % e)

    cur.close()
    conn.close()
    return 0 if agree else 1


if __name__ == "__main__":
    raise SystemExit(main())
