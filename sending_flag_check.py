# -*- coding: utf-8 -*-
"""Integrity check: a `_sending` flag STORED in deals.cue_state.

`_sending` is an in-memory transient in web/app.js, set the instant "Approve & send" is clicked so
the row leaves the approval queue at once. It must never reach the database, because
`approvalPending()` hides any entry carrying it -- so a stored one hides that email from the
approval queue FOR EVER, surviving every reload, while `approve_counts()` does not test the flag
and keeps counting the deal. The result is a ghost on the watcher band with an empty approval
queue, and on the bad path a client who is silently never written to.

Simon 2026-11-06, asked which way to resolve that disagreement: *"make it loud"*. So
`approve_counts()` is deliberately left alone -- hiding the flag there would make a genuinely stuck
email invisible on every surface -- and a stored flag instead raises an `error_log` row, which
`private.error_log_push()` (live since 2026-07-08, rate-limited to 1 push / 10 min) turns into a
phone push, and which `error_digest.py` also picks up in the daily 24h digest.

Three raw cue_state writes that could store the flag were fixed in crm-app 03afb21 the same day
(ladderRestartYes, cancelStageMove, saveDeal via `edits`). This check is how a NEW one gets noticed
instead of being found by hand months later.

Dry-run by default; pass --send to actually write the error_log rows.
  --all   also report the INERT ones (flag stored on an entry that also carries `sent`, which
          approvalPending rejects on its own). They are noise, not damage -- never alerted on.

Reads nothing but `deals` and `email_dispatch`; writes nothing but `error_log`.
Runs from the self-nags workflow (nags.yml), 2x/day -- the 20h guard below means at most one
alert per deal per day even though the workflow fires twice.
"""
import sys, json
from db import connect
import tz

SEND = "--send" in sys.argv
SHOW_INERT = "--all" in sys.argv
SOURCE = "sending-flag"          # error_log.source; keep in step with the guard query below
GUARD_HOURS = 20                 # one alert per deal per day, across both workflow runs


def _cue(v):
    if v is None:
        return {}
    if isinstance(v, str):
        try:
            return json.loads(v or "{}")
        except Exception:
            return {}
    return v or {}


def main():
    c = connect(); c.autocommit = True; cur = c.cursor()

    cur.execute("select d.id, coalesce(ct.full_name, d.deal_name, 'This deal'), d.stage, d.cue_state "
                "from deals d left join contacts ct on ct.id = d.primary_contact_id "
                "where d.cue_state is not null")
    rows = cur.fetchall()

    live, inert = [], []
    for did, who, stage, cue_state in rows:
        for key, e in _cue(cue_state).items():
            if not isinstance(e, dict) or e.get("_sending") is None:
                continue
            # The dangerous case: still pending, never sent, not cancelled -- so it is invisible in
            # the approval queue, on the card and in the pie feed, and counted only by the band.
            hiding = (str(e.get("pending_approval")).lower() == "true"
                      and not (e.get("sent") or "")
                      and str(e.get("cancelled") or "false").lower() in ("", "false"))
            (live if hiding else inert).append((did, who, stage, key, e))

    print("%s  -  stored _sending: %d total, %d hiding a live entry, %d inert"
          % (tz.today(), len(live) + len(inert), len(live), len(inert)))

    if SHOW_INERT:
        for did, who, stage, key, _e in inert:
            print("  inert   %s / %s  (%s)  %s" % (who, key, stage, did))

    if not live:
        print("  nothing to alert on")
        c.close()
        return

    for did, who, stage, key, e in live:
        # Was it actually delivered? PROOF OF DELIVERY IS THE email_dispatch CLAIM, not sent_emails
        # -- approve-email.js never writes sent_emails, and reading that table as evidence sent one
        # session down the wrong path (2026-11-06, Iria Diaz). The endpoint DELETES the claim when
        # sendMail throws, so a surviving claim means the send succeeded and this is a resurrected
        # approval; no claim means a genuinely stuck, never-sent email, which is worse.
        cur.execute("select 1 from email_dispatch where deal_id=%s and cue_key=%s limit 1", (did, key))
        delivered = cur.fetchone() is not None

        # Fully parameterised: an interval cannot take a placeholder directly, so it is built from
        # a bound value rather than formatted into the SQL string.
        cur.execute("select 1 from error_log where source=%s and deal_id=%s "
                    "and ts >= now() - (%s || ' hours')::interval limit 1",
                    (SOURCE, did, GUARD_HOURS))
        if cur.fetchone():
            print("  already alerted today: %s / %s" % (who, key))
            continue

        # Kept short and front-loaded: error_log_push truncates to 110 chars for the phone push.
        msg = ("_sending stored on %s / %s - hidden from the approval queue (%s). Entry queued %s."
               % (who, key,
                  "already delivered, approval resurrected" if delivered else "NOT SENT",
                  e.get("pending_since") or e.get("approval_queued_at") or "unknown"))
        print("  ALERT   " + msg)
        if SEND:
            cur.execute("insert into error_log(source, deal_id, message) values (%s,%s,%s)",
                        (SOURCE, did, msg[:500]))

    if not SEND:
        print("  (dry run - pass --send to write the error_log rows)")
    c.close()


if __name__ == "__main__":
    main()
