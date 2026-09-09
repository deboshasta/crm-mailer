# -*- coding: utf-8 -*-
"""Daily morning email: every pipeline stage, how many deals sit in it, and the email ladder that
stage runs — including the stages that have NO ladder yet, which are called out explicitly so the
gaps stay visible rather than being invisible by omission.

The ladder is read straight out of send_due.CUE, the engine's own table, so this digest can never
drift from what actually sends. Nothing here is a second copy of the ladder rules: if a cue's stage
gating or offset changes in send_due.py, this email changes with it on the next run.

Runs once a day from stage-ladder-digest.yml. Dry-run by default; --send emails it.
"""
import sys, html, datetime
from db import connect
import send_due, mailer

TODAY = datetime.date.today()
CRM_BASE = "https://crm.thesimonshow.com"

# Board order and colours mirror STAGES / STAGE_COLOR_DEFAULT in web/app.js, so the email reads like
# the kanban. Deliberately excluded: 'castle_invite_sent' (an archive bucket, not a pipeline stage),
# 'nothing_due' (display-only, never a deals.stage value), and 'follow_up' (a dead enum value no row
# uses). If you add a stage to the board, add it here too.
STAGES = [
    ("inquiry",       "Inquiry",                  "#8b93a1"),
    ("schedule_call", "Schedule Call",            "#4db6e8"),
    ("qualifying",    "Qualifying",               "#5b8cf0"),
    ("proposal_prep", "Proposal Prep",            "#7a6fe8"),
    ("proposal_sent", "Proposal Sent / Follow-up","#a78bfa"),
    ("refer",         "Refer",                    "#d072b0"),
    ("booked",        "Booked Pending Paperwork", "#f0913e"),
    ("on_hold",       "On Hold",                  "#6b747e"),
    ("closed_won",    "Closed Won",               "#3ecf8e"),
    ("refer_won",     "Refer Won",                "#3bb9a3"),
    ("closed_lost",   "Closed Lost",              "#f0616d"),
    ("likely_no",     "Likely No",                "#b5766e"),
]

# Chronological-ish reading order within a stage: what fires on arrival, then the proposal clock,
# then the run-up to the show, then what follows the thank-you.
ANCHOR_ORDER = {"stage": 0, "proposal_sent": 1, "show": 2, "after_thank_you": 3}


def _when(anchor, off):
    """Plain-English timing for one ladder step."""
    if anchor == "after_thank_you":
        return "right after the thank-you email"
    noun = {"stage": "entering the stage",
            "proposal_sent": "the proposal went out",
            "show": "the show"}.get(anchor, anchor)
    if off == 0:
        return "on " + noun if anchor != "show" else "on show day"
    d = abs(off)
    unit = "day" if d == 1 else "days"
    return f"{d} {unit} {'before' if off < 0 else 'after'} {noun}"


def ladder_for(stage):
    """Every cue send_due.py would run for this stage, in reading order.
    CUE rows are (key, anchor, offset_days, mode, stages)."""
    rows = [c for c in send_due.CUE if stage in c[4]]
    rows.sort(key=lambda c: (ANCHOR_ORDER.get(c[1], 9), c[2]))
    return rows


def main():
    c = connect(); cur = c.cursor()
    cur.execute("select stage::text, count(*) from deals group by 1")
    counts = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute("select key, subject from templates where active=true")
    TPL = {r[0]: r[1] for r in cur.fetchall()}
    c.close()

    withl = [s for s in STAGES if ladder_for(s[0])]
    without = [s for s in STAGES if not ladder_for(s[0])]

    print(f"{TODAY}: {len(STAGES)} stages, {len(withl)} with a ladder, {len(without)} without")
    for key, label, _ in STAGES:
        print(f"  {label:26s} {counts.get(key,0):4d} deals  {len(ladder_for(key))} ladder step(s)")

    b = ['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    b.append('<h2 style="margin:0 0 4px">Pipeline stages &amp; their ladders</h2>')
    total = sum(counts.get(k, 0) for k, _, _ in STAGES)
    b.append(f'<p style="color:#5f6368;margin:0 0 18px">{TODAY.strftime("%A, %B %d, %Y")} &middot; '
             f'{total} deals across {len(STAGES)} stages &middot; '
             f'{len(without)} stage(s) still have no ladder.</p>')

    for key, label, colour in STAGES:
        n = counts.get(key, 0)
        steps = ladder_for(key)
        b.append('<div style="border:1px solid #e6e6e6;border-radius:10px;padding:12px 14px;margin:0 0 12px">')
        b.append(f'<div style="font-weight:bold;font-size:15px;margin-bottom:2px">'
                 f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
                 f'background:{colour};margin-right:7px"></span>{html.escape(label)}'
                 f'<span style="color:#5f6368;font-weight:normal;font-size:13px"> &middot; '
                 f'{n} deal{"" if n == 1 else "s"}</span></div>')

        if not steps:
            b.append('<div style="margin-top:8px;padding:9px 11px;border-radius:8px;background:#fff8e1;'
                     'border:1px solid #ffe0a3;color:#8a6d1f;font-size:13px">'
                     '<b>No ladder yet.</b> Nothing is scheduled to go out on its own while a deal '
                     'sits here.</div>')
        else:
            auto = sum(1 for s in steps if s[3] in send_due.AUTO_MODES)
            b.append(f'<div style="color:#5f6368;font-size:12px;margin:6px 0 9px">'
                     f'{len(steps)} step{"" if len(steps) == 1 else "s"} &middot; '
                     f'{auto} send automatically, {len(steps) - auto} wait for your approval</div>')
            b.append('<table style="width:100%;border-collapse:collapse;font-size:13px">')
            for ckey, anchor, off, mode, _ in steps:
                is_auto = mode in send_due.AUTO_MODES
                chip_bg, chip_fg, chip = ("#e6f4ea", "#137333", "AUTO") if is_auto else ("#e8f0fe", "#1155cc", "APPROVE")
                subject = TPL.get(ckey) or "(no active template)"
                b.append('<tr>'
                         f'<td style="padding:5px 8px 5px 0;vertical-align:top;white-space:nowrap">'
                         f'<span style="background:{chip_bg};color:{chip_fg};font-size:10px;font-weight:bold;'
                         f'padding:2px 6px;border-radius:4px">{chip}</span></td>'
                         f'<td style="padding:5px 8px 5px 0;vertical-align:top">'
                         f'<div style="font-weight:bold">{html.escape(subject)}</div>'
                         f'<div style="color:#5f6368;font-size:12px">{html.escape(ckey)}</div></td>'
                         f'<td style="padding:5px 0;vertical-align:top;color:#5f6368;white-space:nowrap;'
                         f'text-align:right">{html.escape(_when(anchor, off))}</td>'
                         '</tr>')
            b.append('</table>')
        b.append('</div>')

    if without:
        names = ", ".join(html.escape(l) for _, l, _ in without)
        b.append('<div style="border:1px solid #ffe0a3;background:#fff8e1;border-radius:10px;'
                 'padding:12px 14px;margin:4px 0 12px;font-size:13px;color:#8a6d1f">'
                 f'<b>Stages with no ladder yet:</b> {names}.</div>')

    b.append(f'<p style="color:#9aa0a6;font-size:12px;margin-top:6px">Read live from send_due.py\'s cue '
             f'table, so this always matches what actually sends. '
             f'<a href="{CRM_BASE}" style="color:#1155cc">Open the CRM</a></p></div>')
    body = "".join(b)

    # --out writes the exact HTML the email would carry, for eyeballing it without sending.
    if "--out" in sys.argv:
        path = sys.argv[sys.argv.index("--out") + 1]
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        print("wrote preview ->", path)

    if "--send" in sys.argv:
        r = mailer.send_email(mailer.OWNER_ADDR, "Stages & ladders", body)
        print("emailed digest ->", r["routed_to"])
    else:
        print("(dry-run; pass --send to email)")


if __name__ == "__main__":
    main()
