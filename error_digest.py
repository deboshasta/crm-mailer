# -*- coding: utf-8 -*-
"""Failure-alerting Phase 4: a daily 'errors in the last 24h' digest.

Reads the central error_log table (written by deposit-webhook / deposit-checkout / send-now / the mailer)
and emails Simon a summary when there's anything to report. Dry-run by default; --send emails.
Runs from the blocked-digest workflow (same daily cron)."""
import sys, html
from db import connect
import mailer

def _esc(s):
    return html.escape("" if s is None else str(s))

def main():
    c = connect(); cur = c.cursor()
    cur.execute("select ts, source, message, deal_id from error_log "
                "where ts >= now() - interval '24 hours' order by ts desc")
    rows = cur.fetchall(); c.close()
    print("errors in last 24h:", len(rows))
    if not rows:
        print("(nothing to report)")
        return

    items = []
    for ts, source, message, deal_id in rows:
        when = ts.strftime("%b %d %H:%M") if hasattr(ts, "strftime") else str(ts)
        link = ('https://crm.thesimonshow.com/?deal=%s' % deal_id) if deal_id else ""
        deal_a = (' &middot; <a href="%s" style="color:#1155cc">open deal</a>' % _esc(link)) if link else ""
        items.append(
            '<tr><td style="padding:6px 10px;border-bottom:1px solid #eee;white-space:nowrap;color:#666">%s</td>'
            '<td style="padding:6px 10px;border-bottom:1px solid #eee;font-weight:600">%s</td>'
            '<td style="padding:6px 10px;border-bottom:1px solid #eee">%s%s</td></tr>'
            % (_esc(when), _esc(source), _esc(message), deal_a))

    body = ('<div style="font-family:sans-serif;font-size:14px;color:#111">'
            '<p><b>%d error(s)</b> were logged in the last 24 hours:</p>'
            '<table style="border-collapse:collapse;font-size:13px">%s</table>'
            '<p style="color:#888;font-size:12px;margin-top:14px">Sources covered: deposit-webhook, '
            'deposit-checkout, send-now, mailer. (SMS escalation for the critical ones waits on the SMS '
            'foundation.)</p></div>') % (len(rows), "".join(items))

    if "--send" in sys.argv:
        r = mailer.send_email("simon@thesimonshow.com", "%d CRM error(s) in the last 24h" % len(rows), body, owner=True)
        print("emailed ->", r.get("routed_to"))
    else:
        print("(dry-run; pass --send to email)")

if __name__ == "__main__":
    main()
