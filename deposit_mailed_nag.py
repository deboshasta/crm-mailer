# -*- coding: utf-8 -*-
"""Weekly nag: every deposit the CLIENT has said is on its way but that is still not marked paid.

When someone taps "I mailed a cheque" (or Venmo/Zelle/PayPal) on the invoice page,
confirm_deposit_payment() stamps deposit_docs and emails Simon once. From then on send_due.py HOLDS
deposit_chase_1/2 for that deal (DEP_CLAIMED) — chasing a client for money they have told you they
sent reads badly. The risk in holding it is that the whole thing goes quiet and a cheque that never
arrived is simply forgotten, so the nag moves to SIMON: once a week, for as long as the deposit is
unpaid, with everything needed to chase the client by hand in one tap.

ACH is deliberately excluded. "Request ACH details" is a request FOR bank details, not a claim of
payment, so those deals keep getting chased normally and must not appear here.

Runs weekly from deposit-mailed-nag.yml. Dry-run by default; --send emails it.
"""
import sys, re, html, datetime
from db import connect
import mailer

CRM_BASE = "https://crm.thesimonshow.com"


def tel_href(s):
    """Dialable tel: URI, keeping an extension after a pause. Mirrors telHref in web/app.js."""
    parts = re.split(r"\s*(?:x|ext\.?)\s*", str(s or ""), maxsplit=1, flags=re.I)
    num = re.sub(r"[^\d+]", "", parts[0] or "")
    ext = re.sub(r"[^\d]", "", parts[1] if len(parts) > 1 else "")
    return ("tel:" + num + ("," + ext if ext else "")) if num else ""


def phone_link(s):
    """A phone as a tel: link, or plain text when there is nothing dialable in it."""
    s = (s or "").strip()
    if not s:
        return ""
    href = tel_href(s)
    if not href:
        return html.escape(s)
    return '<a href="%s" style="color:#1155cc;text-decoration:underline">%s</a>' % (
        html.escape(href), html.escape(s))


def weeks_ago(ts, now):
    """Whole weeks since the client confirmed — 0 means it was confirmed this week."""
    return int((now - ts).total_seconds() // 604800)


def phrase(w):
    if w <= 0:
        return "earlier this week"
    if w == 1:
        return "1 week ago"
    return "%d weeks ago" % w


def main():
    c = connect(); cur = c.cursor()
    cur.execute("""
        select dd.confirmed_method, dd.confirmed_name, dd.confirmed_at, dd.amount,
               d.id, d.deal_name, d.show_date, d.deposit_amount, d.deposit_status,
               co.full_name, co.email, co.phone_mobile, co.phone_other
        from deposit_docs dd
        join deals d on d.id = dd.deal_id
        left join contacts co on co.id = d.primary_contact_id
        where dd.confirmed_at is not null
          and coalesce(dd.confirmed_method,'') <> 'ACH'
          and coalesce(d.deposit_status::text,'') not in ('paid','not_required')
        order by dd.confirmed_at asc""")            # oldest (most overdue) first
    rows = cur.fetchall()
    c.close()

    now = datetime.datetime.now(datetime.timezone.utc)
    print("%s: %d outstanding client-confirmed deposit(s)" % (datetime.date.today(), len(rows)))
    for r in rows:
        print("   %-26s %-7s confirmed %s" % (str(r[5])[:26], r[0], phrase(weeks_ago(r[2], now))))
    if not rows:
        print("nothing outstanding - no nag sent.")
        return

    b = ['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    b.append('<h2 style="margin:0 0 4px">Payments the client says are on the way</h2>')
    b.append('<p style="color:#5f6368;margin:0 0 6px">%d deposit%s still unpaid in the CRM. '
             'The automatic deposit chase is <b>paused</b> for these — the client has already told you '
             'the money was sent, so the reminder comes to you instead.</p>'
             % (len(rows), '' if len(rows) == 1 else 's'))
    b.append('<p style="color:#5f6368;margin:0 0 18px"><b>If it has arrived:</b> open the deal, set '
             '<b>Deposit status = paid</b>, and send the receipt. That stops these emails and lets the '
             'normal booking sequence carry on. <b>If it has not:</b> the client\'s details are right '
             'here — tap the email or phone to chase them.</p>')

    for (method, who, conf_at, doc_amt, deal_id, deal_name, show_date, dep_amt, dep_status,
         client_name, email, mobile, other) in rows:
        w = weeks_ago(conf_at, now)
        stale = w >= 3                                   # 3+ weeks for a posted cheque is a real flag
        url = "%s/?deal=%s" % (CRM_BASE, deal_id)
        amt = dep_amt or doc_amt
        bits = []
        if email:
            bits.append('<div style="margin:2px 0"><b>Email:</b> <a href="mailto:%s" '
                        'style="color:#1155cc;text-decoration:underline">%s</a></div>'
                        % (html.escape(email), html.escape(email)))
        if mobile:
            bits.append('<div style="margin:2px 0"><b>Mobile:</b> %s</div>' % phone_link(mobile))
        if other:
            bits.append('<div style="margin:2px 0"><b>Other phone:</b> %s</div>' % phone_link(other))
        if not bits:
            bits.append('<div style="margin:2px 0;color:#c0392b">No email or phone on the contact.</div>')

        b.append('<div style="border:1px solid %s;border-radius:10px;padding:12px 14px;margin:0 0 12px;'
                 'background:%s">' % ('#e0a33e' if stale else '#e6e6e6', '#fffaf0' if stale else '#fff'))
        b.append('<div style="font-weight:bold;font-size:15px">%s</div>' % html.escape(deal_name or 'Deal'))
        b.append('<div style="color:#5f6368;font-size:13px;margin:2px 0 9px">'
                 '%s said they paid by <b>%s</b> <b style="color:%s">%s</b>%s%s</div>'
                 % (html.escape(who or client_name or 'The client'), html.escape(method or '?'),
                    '#b8860b' if stale else '#5f6368', phrase(w),
                    (' &middot; show ' + show_date.strftime('%b %d, %Y')) if show_date else '',
                    (' &middot; deposit $' + ('%0.0f' % float(amt))) if amt else ''))
        b.append('<div style="font-size:13px;line-height:1.55">')
        if client_name:
            b.append('<div style="margin:2px 0"><b>Client:</b> %s</div>' % html.escape(client_name))
        b.extend(bits)
        b.append('</div>')
        b.append('<div style="margin-top:10px"><a href="%s" style="display:inline-block;background:#1155cc;'
                 'color:#fff;text-decoration:none;font-weight:bold;padding:8px 16px;border-radius:8px">'
                 'Open the deal</a></div>' % html.escape(url))
        b.append('</div>')

    b.append('<p style="color:#9aa0a6;font-size:12px;margin-top:6px">Weekly, for as long as a '
             'client-confirmed deposit is still unpaid. Marking the deposit paid (or not required) '
             'removes it from this list and releases the deposit chase.</p></div>')
    body = "".join(b)

    if "--out" in sys.argv:
        path = sys.argv[sys.argv.index("--out") + 1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        print("wrote preview ->", path)

    if "--send" in sys.argv:
        n = len(rows)
        subj = "%d deposit%s the client says was sent - still unpaid" % (n, '' if n == 1 else 's')
        r = mailer.send_email(mailer.OWNER_ADDR, subj, body)
        print("emailed nag ->", r["routed_to"])
    else:
        print("(dry-run; pass --send to email)")


if __name__ == "__main__":
    main()
