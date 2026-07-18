# -*- coding: utf-8 -*-
"""SMS draft enqueuer.

Scans deals and drops a pre-filled DRAFT text into sms_outbox for each due trigger.
This NEVER sends anything - it only queues drafts (client mobile + rendered starting
text). Tasker later lists the pending drafts, Simon edits + sends by hand, and Tasker
marks them sent via the mark_sms RPC. Mirrors the email mailer's run pattern.

Triggers (all client-facing, one draft per deal via the sms_outbox unique index):
  inquiry_ack        - new deal in the 'inquiry' stage
  proposal_sent      - proposal_sent_at is set
  booking_confirmed  - deal reaches booked / closed_won
  day_of             - the morning of show_date (>= 9am America/New_York)
  deposit_nudge      - deposit unpaid, ~2 days after booking. SINGLE SEND for now;
                       cadence (repeat/stop) is DEFERRED - do not add repeat logic here.

A go-live floor (private.config 'sms_start', default = install date) keeps historical
deals from flooding the queue on first run - only events on/after the floor enqueue.

Run: `python sms_due.py`  (add `--dry` to preview without inserting).
"""
import sys, re, datetime, time
import tz                            # shared ET helper (zoneinfo when available, manual EDT/EST fallback)
from db import connect

DAY_OF_HOUR = 9                      # earliest hour (ET) to queue the day-of draft
DEPOSIT_NUDGE_AFTER_DAYS = 2         # days after booking before the single deposit nudge
BOOKED = ("booked", "closed_won")

def _et_date(dt):
    """The US-Eastern calendar date of a datetime (aware or naive)."""
    if dt.tzinfo is None:
        return dt.date()
    etinfo = tz.now().tzinfo                       # ZoneInfo if tz data present, else None
    if etinfo is not None:
        return dt.astimezone(etinfo).date()
    utc = dt.astimezone(datetime.timezone.utc)
    return (utc + datetime.timedelta(hours=(-4 if tz._is_edt(utc) else -5))).date()

def _asdate(v):
    if v is None: return None
    if isinstance(v, datetime.datetime): return _et_date(v)
    if isinstance(v, datetime.date): return v
    try: return datetime.date.fromisoformat(str(v)[:10])
    except Exception: return None

def e164(raw):
    """Normalize a phone string to E.164, or None if it isn't a plausible number."""
    if not raw: return None
    s = str(raw).strip()
    if s.startswith("+"):
        d = re.sub(r"\D", "", s)
        return "+" + d if 8 <= len(d) <= 15 else None
    d = re.sub(r"\D", "", s)
    if len(d) == 10: return "+1" + d
    if len(d) == 11 and d.startswith("1"): return "+" + d
    if 8 <= len(d) <= 15: return "+" + d
    return None

def fmt_show(d):
    if not d: return "your event"
    return f"{d.strftime('%A, %B')} {d.day}"

def render(body, first, show_date):
    v = {"ClientFirstName": (first or "there"), "ShowDate": fmt_show(show_date)}
    return re.sub(r"{{\s*(\w+)\s*}}", lambda m: v.get(m.group(1), m.group(0)), body)

def booking_date(deal):
    for k in ("closed_at", "stage_changed_at", "created_at"):
        dt = _asdate(deal.get(k))
        if dt: return dt
    return None

def due_triggers(deal, floor, today, hour):
    """Pure logic: the list of trigger_type strings due for this deal (no phone/template needed)."""
    stage = deal.get("stage")
    sd = _asdate(deal.get("show_date"))
    out = []
    if stage == "inquiry":
        cd = _asdate(deal.get("created_at"))
        if cd and cd >= floor: out.append("inquiry_ack")
    ps = _asdate(deal.get("proposal_sent_at"))
    if ps and ps >= floor: out.append("proposal_sent")
    if stage in BOOKED:
        bd = booking_date(deal)
        if bd and bd >= floor: out.append("booking_confirmed")
        if sd == today and hour >= DAY_OF_HOUR: out.append("day_of")
        if deal.get("deposit_status") not in ("paid", "not_required"):
            if bd and bd >= floor and (today - bd).days >= DEPOSIT_NUDGE_AFTER_DAYS:
                out.append("deposit_nudge")
    return out

def compute_due(deal, contact, floor, today, hour, TPL):
    """Return list of (trigger_type, to_number, body) drafts due for this deal (or [])."""
    mob = e164((contact or {}).get("phone_mobile"))
    if not mob:
        return []
    first = (contact or {}).get("first_name") or (((contact or {}).get("full_name") or "").split() or [""])[0]
    sd = _asdate(deal.get("show_date"))
    out = []
    for t in due_triggers(deal, floor, today, hour):
        tpl = TPL.get(t)
        if tpl:
            out.append((t, mob, render(tpl, first, sd)))
    return out

def main():
    dry = "--dry" in sys.argv
    c = None
    for _ in range(20):
        try: c = connect(); break
        except Exception: time.sleep(6)
    if not c:
        print("DB UNREACHABLE - no drafts queued"); return
    c.autocommit = True
    cur = c.cursor()

    cur.execute("select value from private.config where key='sms_start'")
    r = cur.fetchone()
    now_et = tz.now()
    floor = _asdate(r[0]) if r and r[0] else now_et.date()
    today, hour = now_et.date(), tz.hour()

    cur.execute("select key, body from sms_templates where active=true")
    TPL = {k: b for k, b in cur.fetchall()}

    cols = ["id", "stage", "created_at", "proposal_sent_at", "show_date",
            "deposit_status", "primary_contact_id", "stage_changed_at", "closed_at", "deal_name"]
    # EGRESS: fetch only deals an SMS trigger can fire on (was: all 512 deals + all 2,700 contacts every run).
    # Stage-based triggers need inquiry / booked / closed_won; the proposal_sent trigger fires on ANY stage
    # when proposal_sent_at >= floor, so include those too. Matches due_triggers() exactly.
    _sms_where = ("stage in ('inquiry','schedule_call','qualifying','proposal_prep','proposal_sent','booked','closed_won') "
                  "or (proposal_sent_at is not null and proposal_sent_at >= %s)")
    cur.execute("select " + ",".join(cols) + " from deals where " + _sms_where, (floor,))
    deals = [dict(zip(cols, row)) for row in cur.fetchall()]
    _cids = list({str(d.get("primary_contact_id")) for d in deals if d.get("primary_contact_id")})
    if _cids:
        cur.execute("select id, first_name, full_name, phone_mobile from contacts where id::text = any(%s)", (_cids,))
        CB = {r[0]: {"first_name": r[1], "full_name": r[2], "phone_mobile": r[3]} for r in cur.fetchall()}
    else:
        CB = {}

    queued, skipped_no_mobile, preview = 0, 0, []
    for d in deals:
        contact = CB.get(d.get("primary_contact_id")) or {}
        if not e164(contact.get("phone_mobile")):
            skipped_no_mobile += 1
            continue
        for (t, mob, body) in compute_due(d, contact, floor, today, hour, TPL):
            if dry:
                preview.append((str(d.get("id"))[:8], t, mob, body[:55])); continue
            cur.execute("""insert into sms_outbox(deal_id, to_number, trigger_type, template_key, body)
                           values (%s,%s,%s,%s,%s)
                           on conflict (deal_id, trigger_type) do nothing""",
                        (d["id"], mob, t, t, body))
            queued += cur.rowcount

    if dry:
        print(f"[DRY] would queue {len(preview)} draft(s); {skipped_no_mobile} deals skipped (no mobile). floor={floor}")
        for p in preview[:40]:
            print("   ", p)
    else:
        cur.execute("""insert into private.config(key, value) values ('last_sms_run', %s)
                       on conflict (key) do update set value=excluded.value, updated_at=now()""",
                    (now_et.isoformat(),))
        print(f"queued {queued} new draft(s); {skipped_no_mobile} deals skipped (no mobile). floor={floor}")
    c.close()

if __name__ == "__main__":
    main()
