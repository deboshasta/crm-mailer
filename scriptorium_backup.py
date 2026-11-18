#!/usr/bin/env python3
"""Bubble a Pie Control item when Scriptorium's nightly backup goes stale.

Scriptorium is a SEPARATE Supabase project (bgxzuhaivtyeewjlqklt) with its own
nightly backup, published as a GitHub Release on deboshasta/scriptorium. That
backup already pushes Join two ways of its own - a failed run shouts on the way
down, and a pg_cron watchdog in its own database shouts if no backup has been
recorded for 36 hours. This adds the third surface Simon asked for: the same
condition as a bubble in Pie Control.

⚠⚠ WHY THIS LIVES IN THE CRM AND NOT IN SCRIPTORIUM. The phone reads ONE feed,
   `?a=piefeed` on crm-send, which serves public.pie_items in THIS database.
   Getting a row in there from the other project would mean carrying a CRM
   credential over to Scriptorium - and a CRM key reads deals, contacts,
   payments and every email ever sent. Running the check here instead costs one
   read-only key for a writing app, which is a far smaller thing to hand over,
   and it reuses pie.publish() rather than inventing a second way to publish.

⚠ `backup` is NOT one of the event kinds in pie_feed (`sent`, `agreement`,
  `deposit_paid`, `alert`), so the phone treats it as a STANDING condition:
  it keeps the bubble while the item is in the feed and drops it the moment it
  leaves. That is exactly right here - publish() reconciles, so a healthy
  backup removes the bubble by itself with nothing to tidy up.

Reads SCRIPTORIUM_SECRET_KEY from the environment. Dry run unless --send.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from db import connect
import pie

PROJECT = "bgxzuhaivtyeewjlqklt"
REST = "https://%s.supabase.co/rest/v1/backup_log" % PROJECT

# ⚠ The SAME 36 hours the Scriptorium-side watchdog uses. Two thresholds for
#   one condition would mean the phone and the push disagree about whether
#   anything is wrong, which is worse than either number being slightly off.
#   36h = two missed nights; one missed night is survivable and GitHub's cron
#   drifts by hours under load.
STALE_H = 36

RUNS_URL = "https://github.com/deboshasta/scriptorium/actions/workflows/backup.yml"
SEND = "--send" in sys.argv


def newest():
    """The most recent recorded backup, or None. Raises on any failure, which
    the caller turns into 'change nothing' - see main()."""
    key = os.environ.get("SCRIPTORIUM_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("SCRIPTORIUM_SECRET_KEY is not set")
    req = urllib.request.Request(
        REST + '?select=takenAt,documents,words,tag&order=takenAt.desc&limit=1',
        headers={"apikey": key, "Authorization": "Bearer " + key,
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        rows = json.loads(r.read().decode())
    return rows[0] if rows else None


def main():
    c = connect()

    try:
        row = newest()
    except Exception as e:
        # ⚠⚠ A READ FAILURE MUST NOT CLEAR THE BUBBLE. Publishing [] here would
        #    reconcile away a genuine staleness alert because of a network
        #    blip, which is the one outcome worse than not checking at all.
        #    Leave whatever is published exactly as it is and say so.
        print("could not read Scriptorium's backup log (%s) - leaving the feed "
              "untouched rather than risk clearing a real alert" % e)
        return

    if row is None:
        age_h = None
        sub = ("No Scriptorium backup has ever been recorded. "
               "Check the backup workflow.")
    else:
        taken = datetime.fromisoformat(row["takenAt"].replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - taken).total_seconds() / 3600.0
        sub = ("Last backup %s ago (%s docs, %s words). Check the backup workflow."
               % (_ago(age_h), row.get("documents"), row.get("words")))

    # ⚠ No rows at all is an ALARM, not a pass. A check whose instinct on an
    #   empty table is silence would sit quiet for ever if it never once ran.
    stale = age_h is None or age_h > STALE_H

    if stale:
        items = [{"id": pie.item_id("backup"),
                  "title": "Scriptorium backup is stale",
                  "sub": sub,
                  "deal_id": None,
                  "url": RUNS_URL}]
        print("STALE: %s" % sub)
    else:
        items = []
        print("healthy: last backup %s ago (threshold %dh) - clearing any bubble"
              % (_ago(age_h), STALE_H))

    if not SEND:
        print("(dry run - pass --send to publish)")
        return

    pie.publish(c, "backup", items)
    print("published %d item(s) of kind 'backup'" % len(items))


def _ago(h):
    if h is None:
        return "never"
    if h < 1:
        return "%d min" % max(1, int(h * 60))
    if h < 48:
        return "%.1f hours" % h
    return "%.1f days" % (h / 24.0)


if __name__ == "__main__":
    main()
