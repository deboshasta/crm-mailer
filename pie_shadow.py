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


def feed_items(key):
    req = urllib.request.Request(FEED, headers={"x-bamfam-key": key, "User-Agent": "pie-shadow"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8", "replace")).get("items", [])


def main():
    key = (os.environ.get("BAMFAM_FEED_KEY") or "").strip()
    if not key:
        print("pie-shadow: BAMFAM_FEED_KEY is not set - cannot reach the feed")
        return 2                                  # not a disagreement; a setup problem

    feed = {i["id"]: i for i in feed_items(key)}

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
