# -*- coding: utf-8 -*-
"""Pie Control step 4: publish an owner-nag's items to the phone feed, and decide whether to also
email. See briefs/pie-control-queue.md.

⚠⚠ EVERY FUNCTION HERE FAILS OPEN. publish() swallows its own errors, and unseen() returns the
   WHOLE list on any problem - switch off, unreadable config, missing table, bad data. A nag that
   emails when it did not strictly need to is noise; a nag that goes quiet is missed work.

⚠⚠ THE SUPPRESSION SWITCH IS THE SAME ONE STEP 3 USES: private.config 'pie_suppress_approvals'.
   One switch for the whole feature, so there is one thing to turn off when it misbehaves - and it
   ships OFF. Publishing happens regardless of the switch (it changes no email and is what lets the
   acks be watched); only the EMAIL suppression is gated.

─── why these windows, and why they are not the brief's figures ──────────────────────────────────
The brief specifies a DEADLINE per item type ("email if still unseen after 1 hour / next 7am"),
which assumes the sender can email at an arbitrary moment after the item appears. That is true of
approvals: send_due.py runs every 30 minutes, so step 3 implements the deadline literally.

It is NOT true of these four. vip_missing and blocked_digest run once a day, likely_no_nag twice,
deposit_mailed_nag once a WEEK - so the only moment any of them can email is its own scheduled run,
and the only question that can be asked then is "has he already seen this on the phone?". A
deadline collapses into an ACK FRESHNESS WINDOW, sized to roughly one run of that script:

⚠ The window must never be much SHORTER than the gap between runs, or an ack can never be fresh
  enough to count and the suppression silently does nothing at all.
⚠ Nor much LONGER, or one glance at the phone silences several days of nagging.
"""
import datetime
import json

# ⚠⚠ THE ACK WINDOWS USED TO LIVE HERE (WINDOW_H) AND DELIBERATELY NO LONGER DO.
# They moved into public.pie_ack_window() in build/db/pie_items.sql the moment a third consumer
# appeared: Pie Control on the phone also has to decide whether to bubble, and a window carried
# here AND in SQL AND in Kotlin is three copies of one rule - the mistake the whole published/
# derived split exists to avoid. pie_feed() now applies the window, so `seen_at` means HANDLED
# RIGHT NOW and every consumer reduces to the same single test: is seen_at null?

def _cur(db):
    """Accept a CONNECTION or a CURSOR, the way join.py does.

    ⚠ Returns (cursor, connection_or_None) so publish() can commit its own writes. These four
      scripts do not agree about autocommit - likely_no_nag sets it, vip_missing does not - so a
      helper that assumed either would silently write nothing in some of them.
    """
    return (db.cursor(), db) if hasattr(db, "cursor") else (db, None)


def _commit(conn):
    try:
        if conn is not None and not getattr(conn, "autocommit", False):
            conn.commit()
    except Exception as e:
        print("pie: commit failed:", e)


def item_id(kind, *parts):
    """Stable id for a published item: '<kind>_<part>_<part>'.

    ⚠⚠ STABLE PER DEAL, DELIBERATELY NOT DATE-STAMPED. The BAMFAM feed date-stamps its ids so a
       moved meeting re-bubbles, and step 2 notes that property is wanted - but it is wanted for an
       EVENT. These are standing conditions: a date-stamped id would be brand new every single day,
       so it could never be acked in advance and the email would never once be suppressed. The
       re-nagging comes from the freshness WINDOW above instead.
    """
    return "_".join([kind] + [str(p) for p in parts])


def suppress_on(db):
    """Is email suppression switched on? Defaults OFF and FAILS OFF."""
    cur, _ = _cur(db)
    try:
        cur.execute("select value from private.config where key='pie_suppress_approvals'")
        row = cur.fetchone()
        return bool(row and str(row[0]).strip().lower() in ("on", "true", "1", "yes"))
    except Exception as e:
        print("pie: could not read the suppression switch - emailing everything:", e)
        return False


def publish(db, kind, items):
    """Upsert this kind's items and DELETE the ones that no longer qualify.

    items: [{"id","title","sub","deal_id","url"}]  (deal_id may be None)

    ⚠ Reconciled, never appended to: the caller has just computed the complete qualifying set, so
      anything of this kind not in it is finished and must stop bubbling. That is what gives the
      feed the "an item disappears the moment the work is done" property the brief asks for, even
      though these rows are published rather than derived.
    ⚠ Swallows everything. A phone feed must never be the reason a nag fails to go out.
    """
    cur, conn = _cur(db)
    try:
        ids = [str(i["id"]) for i in items]
        for i in items:
            cur.execute(
                """insert into public.pie_items (item_id, kind, title, sub, deal_id, url)
                   values (%s,%s,%s,%s,%s,%s)
                   on conflict (item_id) do update
                     set kind=excluded.kind, title=excluded.title, sub=excluded.sub,
                         deal_id=excluded.deal_id, url=excluded.url""",
                (str(i["id"]), kind, (i.get("title") or "")[:200], (i.get("sub") or "")[:400],
                 i.get("deal_id"), (i.get("url") or "")[:500]))
        # ⚠⚠ THE ACK GOES WITH THE ITEM. An ack never expires any more (Simon: see one thing
        #    once), so an ack left behind by a finished item would silence its EMAIL for ever if
        #    the same condition ever came back - a customization ticked off and later un-ticked,
        #    a deal pushed out of Likely No and back in. Clearing it here makes a recurrence a
        #    genuinely fresh item: new bubble, and the email fallback armed again.
        #    The bubble itself returns regardless, because ZusCrmQueue bubbles everything in the
        #    feed - this is about the fallback, not the reminder.
        if ids:
            cur.execute("delete from public.pie_ack where item_id in "
                        "(select item_id from public.pie_items where kind=%s and not (item_id = any(%s)))",
                        (kind, ids))
            cur.execute("delete from public.pie_items where kind=%s and not (item_id = any(%s))",
                        (kind, ids))
        else:
            cur.execute("delete from public.pie_ack where item_id in "
                        "(select item_id from public.pie_items where kind=%s)", (kind,))
            cur.execute("delete from public.pie_items where kind=%s", (kind,))
        _commit(conn)
        print("pie: published %d %s item(s)" % (len(ids), kind))
        return True
    except Exception as e:
        print("pie: publish failed for %s (harmless, nothing is suppressed by it):" % kind, e)
        return False


def unseen(db, kind, ids):
    """Of `ids`, which should still be EMAILED? Returns a set.

    Suppresses an id only when the switch is on AND that id carries an ack newer than this kind's
    window. Anything else - switch off, no ack, stale ack, any error at all - is returned.
    """
    cur, _ = _cur(db)
    ids = [str(i) for i in ids]
    if not ids:
        return set()
    try:
        if not suppress_on(cur):
            return set(ids)
        # Read the feed, which has already applied each kind's window - so "handled" is decided
        # in ONE place and this cannot drift from what the phone decided to bubble.
        cur.execute("select public.pie_feed()")
        feed = cur.fetchone()[0] or []
        if isinstance(feed, str):
            feed = json.loads(feed)
        handled = {i["id"] for i in feed if isinstance(i, dict) and i.get("seen_at")}
        seen = set(ids) & handled
        if seen:
            print("pie: %d %s item(s) already seen on the phone - not emailing those"
                  % (len(seen), kind))
        return set(ids) - seen
    except Exception as e:
        print("pie: unseen() failed for %s - emailing everything:" % kind, e)
        return set(ids)


def doorbell(db, title, text):
    """Ring Join so Pie Control polls now instead of in up to 30 minutes.

    ⚠ Carries NO item data, the same contract as the approvals doorbell and the CRM page's doorbell
      to the AHK watcher: a duplicate costs one cheap poll, a missed one costs the normal tick.
    """
    try:
        import join
        return join.push(title, text, db)
    except Exception as e:
        print("pie: doorbell failed (harmless):", e)
        return False
