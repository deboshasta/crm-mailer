# -*- coding: utf-8 -*-
"""Monthly performance report, emailed on the 1st of each month (~7am ET) by monthly-report.yml.

Focus = the month that JUST ENDED (the month before the run month). The report also covers the
trailing 3 completed months, the current month, and the current month + all future months.

A "gig" = a closed_won deal that has a show_date and amount > 0. Zero-dollar gigs are EXCLUDED
from every count, total, and average (Simon's rule, 2026-07-07).

Usage:
  python monthly_report.py --dry      # print the computed numbers, send nothing
  python monthly_report.py --sample   # email the report to simon@thesimonshow.com ONLY
  python monthly_report.py --send     # email the report to all RECIPIENTS (the monthly cron)
"""
import sys, html, datetime
from db import connect
import mailer
import tz

RECIPIENTS = ["simon@thesimonshow.com", "rubuda@gmail.com"]
SAMPLE_TO  = ["simon@thesimonshow.com"]
MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

# ---- date helpers -----------------------------------------------------------
def _add(y, m, delta):
    """Shift (year, month) by delta months. m is 1-12."""
    idx = (y * 12 + (m - 1)) + delta
    return idx // 12, (idx % 12) + 1

def _idx(y, m):
    return y * 12 + (m - 1)

def _money(n):
    return "$" + format(int(round(n)), ",")

# ---- data -------------------------------------------------------------------
def load_gigs():
    """Every qualifying gig as (year, month, amount): closed_won, has a show_date, amount > 0."""
    c = connect(); cur = c.cursor()
    cur.execute("select show_date, amount from deals "
                "where stage = 'closed_won' and show_date is not null "
                "and coalesce(amount, 0) > 0")
    out = [(r[0].year, r[0].month, float(r[1])) for r in cur.fetchall()]
    c.close()
    return out

def _agg(gigs, keep):
    """keep(y, m) -> bool. Returns (count, total, average) over kept gigs."""
    sel = [amt for (y, m, amt) in gigs if keep(y, m)]
    n = len(sel); tot = sum(sel)
    return n, tot, (tot / n if n else 0.0)

# ---- report model -----------------------------------------------------------
def build_model(run_date, gigs):
    cy, cm = run_date.year, run_date.month                 # current month (the run month)
    jy, jm = _add(cy, cm, -1)                              # month that just ended = focus

    # the 3 completed months ending with the just-ended month (most recent first)
    completed = [_add(cy, cm, -k) for k in (1, 2, 3)]      # [ (jm), (jm-1), (jm-2) ]

    def one(y, m):
        n, tot, avg = _agg(gigs, lambda yy, mm: yy == y and mm == m)
        return {"y": y, "m": m, "label": "%s %d" % (MONTHS[m - 1], y),
                "n": n, "tot": tot, "avg": avg}

    last3_n, last3_tot, last3_avg = _agg(
        gigs, lambda yy, mm: _idx(completed[2][0], completed[2][1]) <= _idx(yy, mm) <= _idx(jy, jm))

    onward_n, onward_tot, onward_avg = _agg(
        gigs, lambda yy, mm: _idx(yy, mm) >= _idx(cy, cm))

    # current month + next 11 months (12-month forward view)
    forward = [one(*_add(cy, cm, k)) for k in range(12)]

    return {
        "focus": one(jy, jm),
        "current": one(cy, cm),
        "completed": [one(y, m) for (y, m) in completed],
        "last3": {"n": last3_n, "tot": last3_tot, "avg": last3_avg},
        "onward": {"n": onward_n, "tot": onward_tot, "avg": onward_avg},
        "forward": forward,
    }

# ---- html -------------------------------------------------------------------
_MUTED = "#5f6368"; _INK = "#202124"; _LINE = "#e6e8eb"; _ACC = "#1a73e8"; _PANEL = "#f6f7f9"

def _line(n, tot, avg):
    """'6 gigs  ·  $16,250  ·  avg $2,708' (or a calm zero line)."""
    gigs = "1 gig" if n == 1 else "%d gigs" % n
    return "%s &nbsp;&middot;&nbsp; %s &nbsp;&middot;&nbsp; avg %s" % (gigs, _money(tot), _money(avg))

def _row(label, n, tot, avg, dim=False):
    col = _MUTED if (dim or n == 0) else _INK
    gigs = "1 gig" if n == 1 else "%d gigs" % n
    return (
        '<tr>'
        '<td style="padding:9px 4px;border-bottom:1px solid %s;color:%s">%s</td>'
        '<td style="padding:9px 4px;border-bottom:1px solid %s;text-align:right;color:%s">%s</td>'
        '<td style="padding:9px 4px;border-bottom:1px solid %s;text-align:right;color:%s;font-variant-numeric:tabular-nums">%s</td>'
        '<td style="padding:9px 4px;border-bottom:1px solid %s;text-align:right;color:%s;font-variant-numeric:tabular-nums">%s</td>'
        '</tr>'
    ) % (_LINE, col, html.escape(label),
         _LINE, col, gigs,
         _LINE, col, _money(tot),
         _LINE, col, _money(avg))

def _table(rows_html):
    return (
        '<table style="width:100%%;border-collapse:collapse;font-size:14px">'
        '<tr style="color:%s;font-size:11px;text-transform:uppercase;letter-spacing:.4px">'
        '<td style="padding:0 4px 6px">Month</td>'
        '<td style="padding:0 4px 6px;text-align:right">Gigs</td>'
        '<td style="padding:0 4px 6px;text-align:right">Total</td>'
        '<td style="padding:0 4px 6px;text-align:right">Average</td></tr>'
        '%s</table>'
    ) % (_MUTED, rows_html)

def render_html(model):
    f = model["focus"]; c = model["current"]
    hero = (
        '<div style="background:%s;border:1px solid %s;border-radius:14px;padding:22px 22px 18px;margin:0 0 22px">'
        '<div style="color:%s;font-size:12px;text-transform:uppercase;letter-spacing:.6px;margin:0 0 4px">Month just ended</div>'
        '<div style="color:%s;font-size:22px;font-weight:700;margin:0 0 10px">%s</div>'
        '<div style="color:%s;font-size:17px">%s</div>'
        '</div>'
    ) % (_PANEL, _LINE, _MUTED, _INK, html.escape(f["label"]), _INK, _line(f["n"], f["tot"], f["avg"]))

    # recent: last-3 summary + the 3 completed months
    recent_rows = "".join(_row(x["label"], x["n"], x["tot"], x["avg"]) for x in model["completed"])
    recent = (
        '<h2 style="font-size:14px;color:%s;margin:0 0 4px">Last 3 months</h2>'
        '<div style="color:%s;font-size:15px;margin:0 0 12px">%s</div>%s'
    ) % (_INK, _INK, _line(model["last3"]["n"], model["last3"]["tot"], model["last3"]["avg"]),
         _table(recent_rows))

    # ahead: current month + current-and-future summary + 12-month forward table
    fwd_rows = "".join(
        _row(x["label"] + (" (current)" if (x["y"] == c["y"] and x["m"] == c["m"]) else ""),
             x["n"], x["tot"], x["avg"], dim=(x["y"] != c["y"] or x["m"] != c["m"]) and x["n"] == 0)
        for x in model["forward"])
    ahead = (
        '<h2 style="font-size:14px;color:%s;margin:26px 0 4px">Current month &mdash; %s</h2>'
        '<div style="color:%s;font-size:15px;margin:0 0 10px">%s</div>'
        '<div style="color:%s;font-size:13px;margin:0 0 12px">%s onward (current + all future): <b style="color:%s">%s</b></div>'
        '%s'
    ) % (_INK, html.escape(c["label"]),
         _INK, _line(c["n"], c["tot"], c["avg"]),
         _MUTED, html.escape(c["label"]), _INK, _line(model["onward"]["n"], model["onward"]["tot"], model["onward"]["avg"]),
         _table(fwd_rows))

    foot = ('<p style="color:%s;font-size:12px;margin:22px 0 0;line-height:1.5">'
            'A gig = a Closed Won deal with a show date and a fee above $0. Zero-dollar gigs are '
            'excluded from every count, total, and average. Figures are bucketed by show date.</p>'
            ) % _MUTED

    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:640px;'
        'margin:0 auto;padding:8px 4px;color:%s">'
        '<div style="color:%s;font-size:12px;text-transform:uppercase;letter-spacing:.6px;margin:0 0 2px">The Simon Show</div>'
        '<h1 style="font-size:20px;margin:0 0 10px">Monthly Report</h1>'
        '<p style="font-size:15px;font-weight:700;color:%s;margin:0 0 18px">Feel free with requests to change format or add metrics.</p>'
        '%s%s%s%s</div>'
    ) % (_INK, _MUTED, _INK, hero, recent, ahead, foot)

def subject(model):
    f = model["focus"]
    return "%s %d Monthly Report" % (MONTHS[f["m"] - 1], f["y"])

# ---- entrypoint -------------------------------------------------------------
def main():
    mode = "--dry"
    for a in sys.argv[1:]:
        if a in ("--dry", "--sample", "--send"):
            mode = a
    run_date = tz.now().date()
    gigs = load_gigs()
    model = build_model(run_date, gigs)
    subj = subject(model)
    body = render_html(model)

    if mode == "--dry":
        print("run_date:", run_date, "| subject:", subj)
        f = model["focus"]; print("FOCUS   ", f["label"], f["n"], "gigs", _money(f["tot"]), "avg", _money(f["avg"]))
        l = model["last3"]; print("LAST 3  ", l["n"], "gigs", _money(l["tot"]), "avg", _money(l["avg"]))
        for x in model["completed"]:
            print("  detail", x["label"], x["n"], "gigs", _money(x["tot"]), "avg", _money(x["avg"]))
        c = model["current"]; print("CURRENT ", c["label"], c["n"], "gigs", _money(c["tot"]), "avg", _money(c["avg"]))
        o = model["onward"]; print("ONWARD  ", o["n"], "gigs", _money(o["tot"]), "avg", _money(o["avg"]))
        for x in model["forward"]:
            print("  fwd   ", x["label"], x["n"], "gigs", _money(x["tot"]), "avg", _money(x["avg"]))
        return

    to = SAMPLE_TO if mode == "--sample" else RECIPIENTS
    subj_out = ("SAMPLE - " + subj) if mode == "--sample" else subj
    for addr in to:
        r = mailer.send_email(addr, subj_out, body, owner=True)   # owner=True: internal report, bypasses safe mode
        print("sent ->", addr, r.get("routed_to"))

if __name__ == "__main__":
    main()
