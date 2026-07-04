# -*- coding: utf-8 -*-
"""Daily CRM digest emailed to Simon each morning: shows within 10 days, unpaid deposits,
open follow-ups awaiting a reply, and stale deals needing a next step. Read-only."""
import sys, datetime, html
from db import connect
import mailer

TODAY = datetime.date.today()
def _d(v):
    if not v: return None
    return datetime.date.fromisoformat(str(v)[:10])
def money(v):
    try: return f"${float(v):,.0f}"
    except: return "-"
STAGE_LABEL = {"inquiry":"Inquiry","schedule_call":"Schedule Call","qualifying":"Qualifying",
               "proposal_sent":"Proposal Sent","follow_up":"Follow-up","refer":"Refer",
               "booked":"Booked","closed_won":"Closed Won","closed_lost":"Closed Lost"}

def main():
    c=connect(); cur=c.cursor()
    cols=["id","stage","show_date","deposit_status","next_action_date","next_action","stage_changed_at",
          "amount","deposit_amount","balance_amount","occasion","deal_name","referred_by","primary_contact_id"]
    cur.execute("select "+",".join(cols)+" from deals")
    deals=[dict(zip(cols,r)) for r in cur.fetchall()]
    cur.execute("select id,full_name from contacts")
    NAME={r[0]:r[1] for r in cur.fetchall()}
    c.close()

    def nm(d): return NAME.get(d.get("primary_contact_id")) or d.get("deal_name") or "-"
    in10 = TODAY + datetime.timedelta(days=10)
    is_open = lambda d: d.get("stage") not in ("closed_won","closed_lost")
    paid = lambda d: d.get("deposit_status") in ("paid","not_required")

    shows10 = sorted([d for d in deals if _d(d.get("show_date")) and TODAY<=_d(d["show_date"])<=in10 and d.get("stage")!="closed_lost"],
                     key=lambda d:d["show_date"])
    unpaid  = sorted([d for d in deals if _d(d.get("show_date")) and _d(d["show_date"])>=TODAY and not paid(d) and d.get("stage")!="closed_lost"],
                     key=lambda d:d["show_date"])
    followups = sorted([d for d in deals if d.get("stage") in ("proposal_sent","follow_up")],
                       key=lambda d: str(d.get("stage_changed_at") or ""))
    stale = sorted([d for d in deals if is_open(d) and (not d.get("next_action_date") or _d(d["next_action_date"])<TODAY)],
                   key=lambda d: str(d.get("next_action_date") or ""))

    def section(title, items, cols_fn, empty="Nothing here - nice."):
        b=[f'<h3 style="margin:18px 0 6px">{html.escape(title)} <span style="color:#9aa0a6;font-weight:400">({len(items)})</span></h3>']
        if not items: return "".join(b)+f'<div style="color:#5f6368">{empty}</div>'
        b.append('<table style="border-collapse:collapse;font-size:13px;width:100%">')
        for d in items[:25]:
            b.append('<tr>'+''.join(f'<td style="padding:3px 12px 3px 0;border-bottom:1px solid #eee">{cell}</td>' for cell in cols_fn(d))+'</tr>')
        if len(items)>25: b.append(f'<tr><td style="padding:4px 0;color:#9aa0a6">+{len(items)-25} more</td></tr>')
        b.append('</table>')
        return "".join(b)

    def daysto(d):
        n=(_d(d["show_date"])-TODAY).days; return "today" if n==0 else ("tomorrow" if n==1 else f"{n}d")

    body=['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124;max-width:640px">']
    body.append(f'<h2 style="margin:0">Daily CRM digest - {TODAY:%A, %b %d, %Y}</h2>')
    body.append(section("Shows within 10 days", shows10,
        lambda d:[f'<b>{html.escape(nm(d))}</b>', f'{_d(d["show_date"]):%b %d} ({daysto(d)})',
                  STAGE_LABEL.get(d["stage"],d["stage"]), money(d.get("amount"))]))
    body.append(section("Unpaid deposits (upcoming shows)", unpaid,
        lambda d:[f'<b>{html.escape(nm(d))}</b>', f'{_d(d["show_date"]):%b %d}',
                  f'<span style="color:#c0392b">{d.get("deposit_status") or "-"}</span>', money(d.get("deposit_amount"))]))
    body.append(section("Open follow-ups (awaiting reply)", followups,
        lambda d:[f'<b>{html.escape(nm(d))}</b>', STAGE_LABEL.get(d["stage"],d["stage"]),
                  f'in stage since {(_d(d["stage_changed_at"]) or TODAY):%b %d}']))
    body.append(section("Stale - needs a next step", stale,
        lambda d:[f'<b>{html.escape(nm(d))}</b>', STAGE_LABEL.get(d["stage"],d["stage"]),
                  ('no next-action set' if not d.get("next_action_date") else f'overdue since {_d(d["next_action_date"]):%b %d}')]))
    referrals = sorted([d for d in deals if d.get("referred_by") and d.get("stage")=="closed_won"
                        and _d(d.get("show_date")) and _d(d["show_date"])<=TODAY and (TODAY-_d(d["show_date"])).days<=45],
                       key=lambda d: str(d.get("show_date")))
    body.append(section("Referrers to thank (recent referral wins)", referrals,
        lambda d:[f'<b>{html.escape(nm(d))}</b>', f'{_d(d["show_date"]):%b %d}',
                  f'referred by <b>{html.escape(d.get("referred_by") or "")}</b>'],
        empty="No referral wins in the last 45 days."))
    body.append('</div>')
    html_body="".join(body)

    print(f"Digest {TODAY}: shows10={len(shows10)} unpaid={len(unpaid)} followups={len(followups)} stale={len(stale)}")
    if "--send" in sys.argv:
        r=mailer.send_email("simon@thesimonshow.com", f"[CRM] Daily digest - {TODAY:%b %d}", html_body)
        print("emailed ->", r["routed_to"])
    return html_body

if __name__=="__main__":
    main()
