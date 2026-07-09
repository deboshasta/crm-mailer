# -*- coding: utf-8 -*-
"""EBLAST send worker (throttled bulk sender).

Picks a scheduled campaign, expands its audience into campaign_recipients (once), then
sends a throttled batch per run via SES SMTP - injecting open-pixel + click-tracking, an
unsubscribe footer with the CAN-SPAM postal address, and RFC-8058 List-Unsubscribe headers.
Each recipient is claimed atomically (double-send-safe) and marked sent/failed. When the last
recipient is sent the campaign flips to 'sent'.

DORMANT until go-live: if SES SMTP creds aren't in private.config the worker just idles, so
wiring it into the schedule now is safe. Two go-live levers (both yours):
  - add ses_smtp_host / ses_smtp_port / ses_smtp_user / ses_smtp_password to private.config
  - set private.config eblast_safe_mode = 'false'  (defaults to ON -> routes every send to Simon)

Run: `python eblast_send.py`   (add `--dry` to render + expand without sending or marking).
Tracking + unsubscribe endpoints live on crm-send (private.config crm_send_base) and are done.
"""
import sys, re, uuid, ssl, smtplib, time, datetime, json, html as _html
from email.message import EmailMessage
from db import connect

MAX_PER_RUN   = 200                       # hard cap per run (throttle_per_hour still applies below)
CLAIM_STATUS  = "sending"                 # interim per-recipient status while a send is in flight
DEFAULT_POSTAL = "Simon Mandal Magic &middot; 9 Mine Ave, Bernardsville, NJ 07924, USA"

# ---------- pure helpers (no DB / SMTP; unit-tested) ----------
def open_url(base, rid):          return f"{base}/api/eblast-open?r={rid}"
def click_url(base, tok, rid):    return f"{base}/api/eblast-click?l={tok}&r={rid}"
def unsub_url(base, tok):         return f"{base}/api/eblast-unsub?t={tok}"

def extract_links(html, base):
    """Distinct http(s) hrefs in the body, excluding our own tracking/unsub base and mailto/tel."""
    urls = re.findall(r'href\s*=\s*"(https?://[^"]+)"', html or "", re.I)
    out, seen = [], set()
    for u in urls:
        if base and u.startswith(base):    # already one of our tracking links
            continue
        if u in seen: continue
        seen.add(u); out.append(u)
    return out

def personalize(html, links_map, base, rid, unsub_token, postal, preheader):
    """Return the per-recipient HTML: links rewritten to click-tracking, an open pixel, a
    preheader, and an unsubscribe + CAN-SPAM footer appended."""
    body = html or ""
    # rewrite each tracked link -> click URL carrying this recipient id
    for target, tok in links_map.items():
        body = body.replace('href="%s"' % target, 'href="%s"' % click_url(base, tok, rid))
    pre = ""
    if preheader:
        pre = ('<div style="display:none;max-height:0;overflow:hidden;opacity:0">%s</div>'
               % _html.escape(preheader))
    uu = unsub_url(base, unsub_token)
    footer = (
        '<div style="margin-top:28px;padding-top:14px;border-top:1px solid #ddd;'
        'font-family:Arial,sans-serif;font-size:12px;color:#888;line-height:1.5">'
        '%s<br>'
        'You are receiving this because you are a contact of Simon Mandal Magic.<br>'
        '<a href="%s" style="color:#888">Unsubscribe</a>'
        '</div>' % (postal, uu))
    pixel = '<img src="%s" width="1" height="1" alt="" style="display:none">' % open_url(base, rid)
    return pre + body + footer + pixel

def html_to_text(html, unsub):
    t = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', html or "")
    t = re.sub(r'(?i)<br\s*/?>', '\n', t)
    t = re.sub(r'(?i)</p\s*>', '\n\n', t)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = _html.unescape(t)
    t = re.sub(r'[ \t]+', ' ', t)
    t = re.sub(r'\n\s*\n\s*\n+', '\n\n', t).strip()
    return t + "\n\n---\nUnsubscribe: " + unsub

# ---------- config ----------
def load_cfg(cur):
    cur.execute("select mail_safe_mode, safe_mode_recipient, send_from_email, send_from_name from settings where id=1")
    s = cur.fetchone()
    cur.execute("select key, value from private.config")
    p = {k: v for k, v in cur.fetchall()}
    return {
        "safe_recipient": str(s[1]),
        "from_email": str(s[2]), "from_name": s[3] or "Simon Mandal",
        "base": (p.get("crm_send_base") or "").rstrip("/"),
        "postal": p.get("eblast_postal") or DEFAULT_POSTAL,
        "eblast_safe": (p.get("eblast_safe_mode", "true").lower() != "false"),   # default ON
        "host": p.get("ses_smtp_host"), "port": int(p.get("ses_smtp_port") or "465"),
        "user": p.get("ses_smtp_user"), "password": p.get("ses_smtp_password"),
    }

def ses_ready(cfg):
    return bool(cfg["host"] and cfg["user"] and cfg["password"] and cfg["base"])

# ---------- audience expansion ----------
def build_recipients(cur, camp):
    """Materialize campaign_recipients from the audience (once). Mirrors the composer's
    mailable + filter logic. De-duped by lowercased email; skips suppressed/unsubscribed."""
    a = camp.get("audience") or {}
    if isinstance(a, str):
        try: a = json.loads(a)
        except Exception: a = {}
    where = ["c.email is not null", "c.email <> ''", "c.unsubscribed_at is null",
             "lower(c.email) not in (select email from suppressions)"]
    args = []
    if a.get("mode") == "filter":
        if a.get("event"):
            where.append("exists (select 1 from deals d where d.primary_contact_id=c.id and d.event_type=%s)")
            args.append(a["event"])
        if a.get("source"):
            where.append("c.lead_source=%s"); args.append(a["source"])
        if a.get("repeat"):
            where.append("c.is_repeat_client = true")
    sql = ("select distinct on (lower(c.email)) c.id, c.email from contacts c where "
           + " and ".join(where) + " order by lower(c.email)")
    cur.execute(sql, args)
    rows = cur.fetchall()
    ab = bool(camp.get("ab_test"))
    for i, (cid, email) in enumerate(rows):
        variant = ("a" if i % 2 == 0 else "b") if ab else None
        cur.execute("""insert into campaign_recipients(campaign_id, contact_id, email, status, ab_variant)
                       values (%s,%s,%s,'pending',%s)""", (camp["id"], cid, email, variant))
    return len(rows)

def link_map(cur, camp_id, body):
    """Get-or-create a campaign_links token per distinct target URL. Returns {url: token}."""
    m = {}
    cur.execute("select token, target_url from campaign_links where campaign_id=%s", (camp_id,))
    for tok, url in cur.fetchall():
        m[url] = tok
    return m

def ensure_links(cur, camp_id, base, body):
    existing = link_map(cur, camp_id, body)
    for url in extract_links(body, base):
        if url in existing: continue
        cur.execute("""insert into campaign_links(campaign_id, target_url) values (%s,%s) returning token""",
                    (camp_id, url))
        existing[url] = cur.fetchone()[0]
    return existing

# ---------- SMTP ----------
def build_message(from_hdr, to_addr, reply_to, subject, html_body, text_body, unsub, msg_id):
    msg = EmailMessage()
    msg["From"] = from_hdr
    msg["To"] = to_addr
    msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Message-ID"] = msg_id
    # RFC 8058 one-click unsubscribe (Gmail/Yahoo bulk requirement)
    msg["List-Unsubscribe"] = "<%s>" % unsub
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg

def smtp_send(cfg, msg):
    if int(cfg["port"]) == 465:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ssl.create_default_context()) as s:
            s.login(cfg["user"], cfg["password"]); s.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"]) as s:
            s.ehlo(); s.starttls(context=ssl.create_default_context()); s.ehlo()
            s.login(cfg["user"], cfg["password"]); s.send_message(msg)

# ---------- per-campaign orchestration ----------
def process_campaign(cur, cfg, camp, dry=False):
    base = cfg["base"]
    if camp["status"] != "sending":
        cur.execute("update campaigns set status='sending', sent_at=coalesce(sent_at, now()) where id=%s", (camp["id"],))
    # recover any rows left 'sending' by a crashed prior run (mailer concurrency-group => no live overlap)
    cur.execute("update campaign_recipients set status='pending' where campaign_id=%s and status=%s", (camp["id"], CLAIM_STATUS))

    cur.execute("select count(*) from campaign_recipients where campaign_id=%s", (camp["id"],))
    if cur.fetchone()[0] == 0:
        n = build_recipients(cur, camp)
        print(f"eblast: expanded audience -> {n} recipient(s)")

    links = ensure_links(cur, camp["id"], base, camp["body_html"] or "")

    # throttle: don't exceed throttle_per_hour across the trailing hour
    cur.execute("""select count(*) from campaign_recipients
                   where campaign_id=%s and sent_at > now() - interval '1 hour'""", (camp["id"],))
    sent_last_hour = cur.fetchone()[0]
    budget = max(0, int(camp["throttle_per_hour"] or 500) - sent_last_hour)
    per_run = min(MAX_PER_RUN, budget)
    if per_run <= 0:
        print(f"eblast: throttled (sent {sent_last_hour} in last hour), waiting")
        return {"sent": 0, "failed": 0, "skipped": 0, "remaining": None}

    from_hdr = "%s <%s>" % (camp["from_name"] or cfg["from_name"], camp["from_email"] or cfg["from_email"])
    reply_to = camp["reply_to"] or camp["from_email"] or cfg["from_email"]
    sent = failed = skipped = 0

    for _ in range(per_run):
        # atomic claim of one pending recipient
        cur.execute("""update campaign_recipients set status=%s
                       where id = (select id from campaign_recipients
                                   where campaign_id=%s and status='pending'
                                   order by created_at limit 1 for update skip locked)
                       returning id, email, ab_variant, unsub_token""", (CLAIM_STATUS, camp["id"]))
        got = cur.fetchone()
        if not got:
            break
        rid, email, variant, unsub_token = got
        low = (email or "").lower()
        # suppression re-check at send time
        cur.execute("select 1 from suppressions where email=%s union select 1 from contacts where lower(email)=%s and unsubscribed_at is not null limit 1", (low, low))
        if cur.fetchone():
            cur.execute("update campaign_recipients set status='skipped_suppressed' where id=%s", (rid,))
            cur.execute("insert into email_events(campaign_id, recipient_id, type) values (%s,%s,'fail')", (camp["id"], rid))
            skipped += 1
            continue

        subject = camp["subject_b"] if (variant == "b" and camp["subject_b"]) else camp["subject"]
        unsub = unsub_url(base, unsub_token)
        html_body = personalize(camp["body_html"] or "", links, base, rid, unsub_token, cfg["postal"], camp["preheader"])
        text_body = html_to_text(camp["body_html"] or "", unsub)
        msg_id = "<%s@thesimonshow.com>" % uuid.uuid4().hex
        to_addr = cfg["safe_recipient"] if cfg["eblast_safe"] else email
        subj = ("[EBLAST-SAFE -> %s] %s" % (email, subject or "")) if cfg["eblast_safe"] else (subject or "")

        if dry:
            cur.execute("update campaign_recipients set status='pending' where id=%s", (rid,))   # un-claim; dry changes nothing
            sent += 1
            continue
        try:
            msg = build_message(from_hdr, to_addr, reply_to, subj, html_body, text_body, unsub, msg_id)
            smtp_send(cfg, msg)
            cur.execute("update campaign_recipients set status='sent', sent_at=now(), message_id=%s where id=%s", (msg_id, rid))
            cur.execute("insert into email_events(campaign_id, recipient_id, type) values (%s,%s,'send')", (camp["id"], rid))
            sent += 1
        except Exception as e:
            cur.execute("update campaign_recipients set status='failed', error=%s where id=%s", (str(e)[:400], rid))
            cur.execute("insert into email_events(campaign_id, recipient_id, type) values (%s,%s,'fail')", (camp["id"], rid))
            failed += 1

    # campaign complete?
    cur.execute("select count(*) from campaign_recipients where campaign_id=%s and status in ('pending',%s)", (camp["id"], CLAIM_STATUS))
    remaining = cur.fetchone()[0]
    if remaining == 0 and not dry:
        cur.execute("update campaigns set status='sent', sent_at=coalesce(sent_at, now()) where id=%s", (camp["id"],))
    # cache a small rollup
    cur.execute("""select count(*), count(*) filter (where status='sent') from campaign_recipients where campaign_id=%s""", (camp["id"],))
    tot, done = cur.fetchone()
    cur.execute("update campaigns set stats = coalesce(stats,'{}'::jsonb) || %s::jsonb where id=%s",
                (json.dumps({"recipients": tot, "sent": done}), camp["id"]))

    mode = "DRY " if dry else ("SAFE " if cfg["eblast_safe"] else "")
    print(f"eblast: {mode}campaign '{camp['name']}' -> sent={sent} failed={failed} skipped={skipped} remaining={remaining}")
    return {"sent": sent, "failed": failed, "skipped": skipped, "remaining": remaining}

# ---------- main ----------
def main():
    dry = "--dry" in sys.argv
    c = None
    for _ in range(20):
        try: c = connect(); break
        except Exception: time.sleep(6)
    if not c:
        print("eblast: DB unreachable"); return
    c.autocommit = True
    cur = c.cursor()
    cfg = load_cfg(cur)
    if not dry and not ses_ready(cfg):
        print("eblast: SES not configured (private.config ses_smtp_*), worker idle"); c.close(); return
    cur.execute("""select id, name, status, from_name, from_email, reply_to, subject, subject_b,
                          ab_test, preheader, body_html, throttle_per_hour, scheduled_at, audience, stats
                   from campaigns
                   where status in ('scheduled','sending') and coalesce(scheduled_at, now()) <= now()
                   order by created_at limit 1""")
    row = cur.fetchone()
    if not row:
        print("eblast: no campaign due"); c.close(); return
    cols = ["id","name","status","from_name","from_email","reply_to","subject","subject_b",
            "ab_test","preheader","body_html","throttle_per_hour","scheduled_at","audience","stats"]
    process_campaign(cur, cfg, dict(zip(cols, row)), dry)
    c.close()

if __name__ == "__main__":
    main()
