# -*- coding: utf-8 -*-
"""Nightly sender: walk every deal's cue, send the AUTO/WINDOW emails due TODAY that
aren't cancelled or already sent, and mark them sent. FLAG-mode emails are never
auto-sent (they are Focus reminders). Dry-run by default; pass --send to actually send.
Safe-mode still routes every send to Simon until mail_safe_mode is turned off.
"""
import sys, json, html, re, datetime, urllib.parse, secrets
from db import connect
import mailer
import tz
import attachments

SEND = "--send" in sys.argv
TODAY = tz.today()          # Eastern "today" so cloud (UTC) runs match Simon's local day

# per-deal customization/trivia form base (must match app.js CUSTOMIZE_BASE).
# Update to the real deployed host at go-live.
CUSTOMIZE_BASE = "https://www.thesimonshow.com/trivia.html"

# CRM app base for deep-links in notification emails (?deal=<id> opens that deal).
CRM_BASE = "https://crm.thesimonshow.com"
# Approval-mode: Approve/Cancel buttons in the authorize email point at this Vercel endpoint.
APPROVE_BASE = "https://crm-send-the-simon-show.vercel.app/api/approve-email"
# GCal!: the "Update GCal link" button opens this paste page to store the calendar-event URL.
GCAL_PASTE_BASE = "https://crm-send-the-simon-show.vercel.app/api/gcal-link"

# trivia questions (full text) - must match the labels/keys on trivia.html
TRIVIA_QUESTIONS = [
    ("best_qualities",   "What are some of the guest of honor's best qualities?"),
    ("hobbies",          "What are some of the guest of honor's hobbies?"),
    ("everyone_knows",   "What are a few things everyone knows about the guest of honor?"),
    ("no_one_knows",     "What are a few things almost no-one knows about the guest of honor? (obscure facts, nothing embarrassing)"),
    ("accomplishments",  "What are some of the guest of honor's biggest accomplishments?"),
    ("special_location", "What is a location that has special meaning to the guest of honor?"),
    ("location_why",     "Why is this location special?"),
    ("anything_else",    "Anything else you'd like me to know about the guest of honor?"),
]

PERF = {}   # performer_id -> {first_name, full_name}, loaded in main(); PerformerName merge uses first_name

# (key, anchor, offset_days, mode, stages)
CUE = [
    ("customization_request","show",-21,"flag",("closed_won",)),
    ("pre_show_2week","show",-14,"auto",("closed_won",)),
    ("pre_show_reminder","show",-3,"auto",("closed_won",)),
    ("deposit_chase_1","stage",2,"flag",("booked","closed_won")),
    ("deposit_chase_2","stage",7,"flag",("booked","closed_won")),
    ("balance_reminder","show",-2,"flag",("booked","closed_won")),
    ("thank_you","show",1,"auto",("closed_won",)),
    ("review_request","show",2,"auto",("closed_won",)),
    ("popped_into","show",14,"flag",("closed_won",)),
    ("rebook","show",240,"flag",("closed_won",)),
    ("pf_fu1","proposal_sent",3,"auto",("proposal_sent",)),
    ("pf_abcde","proposal_sent",7,"auto",("proposal_sent",)),
    ("pf_shark","proposal_sent",10,"auto",("proposal_sent",)),
    ("pf_breakup","proposal_sent",14,"auto",("proposal_sent",)),
    ("precall_fu1","stage",1,"auto",("schedule_call",)),
    ("precall_fu2","stage",4,"auto",("schedule_call",)),
    ("precall_abcde","stage",7,"auto",("schedule_call",)),
    ("precall_shark","stage",10,"auto",("schedule_call",)),
    ("precall_breakup","stage",14,"auto",("schedule_call",)),
    ("guest_excited","stage",0,"auto",("closed_won","booked")),
    ("confirm_details","stage",0,"auto",("closed_won",)),
    ("refer","stage",0,"flag",("refer",)),
    ("closed_lost_daybefore","show",-1,"flag",("closed_lost",)),
    ("closed_lost_after","show",2,"flag",("closed_lost",)),
    ("refer_won_daybefore","show",-1,"auto",("refer_won",)),
    ("refer_won_after","show",2,"auto",("refer_won",)),
]
AUTO_MODES = ("auto",)   # 'window' mode retired -> folded into 'auto' (they always behaved identically)

# ---- Missing-field guard ---------------------------------------------------
# An AUTO email is PAUSED (not sent) when the template uses a {{merge field}} that is blank for
# this deal, so a client never receives an email with a hole in it. The pause is recorded on
# cue_state[key].blocked = {"since": iso, "fields": [...]}. blocked_digest.py nags Simon once a
# day until it is filled (then the email auto-sends, see the re-check pass) or cancelled.
# OPTIONAL fields are legitimately often-blank and never block (kept in sync with check_missing.py).
OPTIONAL_FIELDS = {"Company", "GuestOfHonor", "ProposalLink", "LastShowYear", "EventDetails"}
# when one of these is BLANK, drop the WHOLE line it sits on (don't show it, don't flag it)
OPTIONAL_LINE_FIELDS = {"EventDetails"}
def _drop_empty_optional_lines(raw, V):
    out=[]
    for ln in (raw or "").split("\n"):
        toks=re.findall(r"\{\{(\w+)\}\}", ln)
        if any(t in OPTIONAL_LINE_FIELDS and not V.get(t) for t in toks): continue   # blank optional -> drop line
        out.append(ln)
    return "\n".join(out)
def _tpl_fields(t):
    return set(re.findall(r"\{\{(\w+)\}\}", (t.get("subject") or "") + " " + (t.get("body") or "")))
def missing_fields(t, e, V):
    """Blank required merge fields for this template+deal. Returns [] when Simon wrote a manual
    override (e.subject / e.body) - a hand-written email is assumed complete."""
    if (e.get("subject") is not None) or (e.get("body") is not None):
        return []
    return sorted(f for f in _tpl_fields(t) if not V.get(f) and f not in OPTIONAL_FIELDS)

def _blocked_alert_html(rows):
    """Immediate 'an email just got paused' alert to Simon. rows: [(client_name, key, [blanks], deal_id)]."""
    b=['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    b.append('<h2 style="margin:0 0 4px">Email paused - missing info</h2>')
    b.append('<p style="color:#5f6368;margin:0 0 16px">An email just came due but is on hold because a '
             'required field is blank. Fill it in and it sends automatically:</p>')
    for nm,key,blanks,did in rows:
        url=f"{CRM_BASE}/?deal={did}&fixemail={key}"
        b.append('<div style="border:1px solid #e6e6e6;border-radius:10px;padding:12px 14px;margin:0 0 10px">')
        b.append(f'<div style="font-weight:bold;margin-bottom:2px">{html.escape(str(nm))}</div>')
        b.append(f'<div style="color:#5f6368;font-size:12px;margin-bottom:9px">{html.escape(key)} &middot; '
                 f'missing: <span style="color:#c0392b">{html.escape(", ".join(blanks))}</span></div>')
        b.append(f'<a href="{html.escape(url)}" style="display:inline-block;background:#1155cc;color:#fff;'
                 'text-decoration:none;font-weight:bold;padding:8px 16px;border-radius:8px">Add the missing info</a>')
        b.append('</div>')
    b.append('<p style="color:#9aa0a6;font-size:12px;margin-top:6px">You will also get a daily reminder until it is resolved.</p></div>')
    return "".join(b)

def _authorize_email_html(rows):
    """Approval-mode authorize email: each held email shows the Approve / Edit / Cancel buttons FIRST,
    then the recipient, subject, and a preview of the full body.
    rows: [(client_name, key, to, subject, body, token, deal_id)]."""
    b=['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    b.append('<h2 style="margin:0 0 4px">Approve to send</h2>')
    b.append('<p style="color:#5f6368;margin:0 0 16px">These emails are held and will NOT go to the client '
             'until you Approve. Edit opens it in the CRM; Cancel drops it (you can revive it later).</p>')
    for nm,key,to,subj,body,token,did in rows:
        appr=f"{APPROVE_BASE}?t={token}&a=approve"
        canc=f"{APPROVE_BASE}?t={token}&a=cancel"
        edit=f"{CRM_BASE}/?deal={did}&editemail={key}"
        b.append('<div style="border:1px solid #e6e6e6;border-radius:10px;padding:14px 16px;margin:0 0 16px">')
        # buttons FIRST, above the To field
        b.append('<div style="margin:0 0 12px">')
        b.append(f'<a href="{html.escape(appr)}" style="display:inline-block;background:#1f8f5f;color:#fff;'
                 'text-decoration:none;font-weight:bold;padding:9px 18px;border-radius:8px;margin:0 8px 8px 0">Approve &amp; send</a>')
        b.append(f'<a href="{html.escape(edit)}" style="display:inline-block;background:#1155cc;color:#fff;'
                 'text-decoration:none;font-weight:bold;padding:9px 18px;border-radius:8px;margin:0 8px 8px 0">Edit</a>')
        b.append(f'<a href="{html.escape(canc)}" style="display:inline-block;background:#b23b3b;color:#fff;'
                 'text-decoration:none;font-weight:bold;padding:9px 18px;border-radius:8px;margin:0 0 8px 0">Cancel</a>')
        b.append('</div>')
        b.append(f'<div style="color:#5f6368;font-size:12px">To: {html.escape(str(nm))} &lt;{html.escape(str(to))}&gt;</div>')
        b.append(f'<div style="font-weight:bold;margin:3px 0 10px">{html.escape(str(subj))}</div>')
        b.append(f'<div style="border-top:1px solid #eee;padding-top:12px">{body or ""}</div>')   # full body preview (rendered HTML)
        b.append('</div>')
    b.append('</div>')
    return "".join(b)

# Self gig check-ins: reminders emailed TO Simon before each booked gig (old GCal! function).
# (days_before_show, label). Best-guess cadence - adjust freely.
SELF_CHECKINS = [(7,"in 1 week"), (3,"in 3 days"), (1,"tomorrow"), (0,"TODAY")]

def _parse_time(s):
    if not s: return None
    m=re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?", str(s), re.I)
    if not m: return None
    h=int(m.group(1)); mm=int(m.group(2) or 0); ap=(m.group(3) or "").lower().replace(".","")
    if ap=="pm" and h<12: h+=12
    if ap=="am" and h==12: h=0
    return (h,mm) if 0<=h<=23 and 0<=mm<=59 else None

def _gcal_day_link(d):
    """Google Calendar day view for the show date, with the show time in the query so Simon can see it:
    https://calendar.google.com/calendar/u/0/r/customday/YYYY/MM/DD?<show_time>"""
    sd=_d(d.get("show_date"))
    if not sd: return ""
    stime=re.sub(r"\s+","",(d.get("show_time") or ""))   # drop spaces so it stays readable in the URL (7pm, 5:30PM)
    base="https://calendar.google.com/calendar/u/0/r/customday/%04d/%02d/%02d" % (sd.year, sd.month, sd.day)
    return base + ("?"+stime if stime else "")

def gcal_link(d, V):
    """The calendar link for a deal: the SAVED event URL once Simon has stored it, else the add-event link."""
    if d.get("gcal_url"): return d["gcal_url"]                 # saved event URL wins for every gcal link
    sd=_d(d.get("show_date"))
    if not sd: return ""
    who=V.get("ClientFullName") or d.get("deal_name") or "Gig"
    title=f"Simon Mandal - {who}" + (f' ({V.get("Occasion")})' if V.get("Occasion") else "")
    t=_parse_time(d.get("show_time"))
    if t:
        s0=datetime.datetime(sd.year,sd.month,sd.day,t[0],t[1]); s1=s0+datetime.timedelta(hours=2)
        dates=f"{s0:%Y%m%dT%H%M%S}/{s1:%Y%m%dT%H%M%S}"
    else:
        dates=f"{sd:%Y%m%d}/{(sd+datetime.timedelta(days=1)):%Y%m%d}"
    details=" | ".join(x for x in [V.get("EventDetails"), V.get("FormatDetails"),
        f'Fee ${V.get("AppearanceFee") or "?"}, deposit ${V.get("DepositAmount") or "0"}, balance ${V.get("BalanceAmount") or "?"}',
        (f'Client: {V.get("ClientPhone")}' if V.get("ClientPhone") else "")] if x)
    params={"action":"TEMPLATE","text":title,"dates":dates,"location":V.get("Venue") or "","details":details}
    return "https://calendar.google.com/calendar/render?"+urllib.parse.urlencode(params)

def _gcal_email_html(d, V, token, reminder):
    """GCal! email to Simon: a calendar link for the show + an 'Update GCal link' button (paste page).
    reminder=True adds the 'add to calendar and update gcal link' banner at the top."""
    who = V.get("ClientFullName") or d.get("deal_name") or "this booking"
    when = V.get("ShowDate") or (str(d.get("show_date")) if d.get("show_date") else "TBD")
    stime = (d.get("show_time") or "").strip()
    link = d.get("gcal_url") or _gcal_day_link(d)   # saved event URL wins, else the custom-day link
    paste = "%s?t=%s" % (GCAL_PASTE_BASE, token)
    b=['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    if reminder:
        b.append('<div style="background:#fff4d6;border:1px solid #e0a92e;border-radius:8px;padding:10px 12px;'
                 'margin:0 0 14px;font-weight:bold;color:#7a5a00">Reminder - add to calendar and update gcal link</div>')
    b.append('<h2 style="margin:0 0 4px">Add to calendar: %s</h2>' % html.escape(str(who)))
    b.append('<p style="color:#5f6368;margin:0 0 14px">%s%s</p>' % (html.escape(str(when)), (" at "+html.escape(stime)) if stime else ""))
    if link:
        open_lbl = "Open your event" if d.get("gcal_url") else "Open calendar to create appointment"
        b.append('<a href="%s" style="display:inline-block;background:#1155cc;color:#fff;text-decoration:none;'
                 'font-weight:bold;padding:10px 20px;border-radius:8px;margin:0 8px 10px 0">%s</a>' % (html.escape(link), open_lbl))
    b.append('<a href="%s" style="display:inline-block;background:#1f8f5f;color:#fff;text-decoration:none;'
             'font-weight:bold;padding:10px 20px;border-radius:8px;margin:0 0 10px 0">Update GCal link</a>' % html.escape(paste))
    # all the deal info, so Simon can paste it into the calendar appointment
    rows=[("Client", (V.get("ClientFullName") or "-") + (("   "+V["ClientPhone"]) if V.get("ClientPhone") else "")),
          ("When", (V.get("ShowDate") or "-") + ((" at "+stime) if stime else "")),
          ("Venue", V.get("Venue") or "(not set)"),
          ("Occasion", V.get("Occasion") or "-"),
          ("Event details", V.get("EventDetails") or "-"),
          ("Format", V.get("FormatDetails") or "-"),
          ("Money", "Fee $%s  -  deposit $%s  -  balance $%s" % (V.get("AppearanceFee") or "?", V.get("DepositAmount") or "0", V.get("BalanceAmount") or "?"))]
    b.append('<table style="border-collapse:collapse;margin-top:16px;font-size:13px">')
    for k,val in rows:
        b.append('<tr><td style="padding:3px 16px 3px 0;color:#5f6368;vertical-align:top;white-space:nowrap"><b>%s</b></td>'
                 '<td style="padding:3px 0">%s</td></tr>' % (html.escape(k), html.escape(str(val))))
    b.append('</table>')
    b.append('</div>')
    return "".join(b)

def selfcheckin_html(d, V, label):
    rows=[("Client", (V["ClientFullName"] or "-") + (f'   {V["ClientPhone"]}' if V["ClientPhone"] else "")),
          ("When", (V["ShowDate"] or "-") + (f' at {V["ShowTime"]}' if V["ShowTime"] else "")),
          ("Venue", V["Venue"] or "(not set)"),
          ("Occasion", V["Occasion"] or "-"),
          ("Event details", V["EventDetails"] or "-"),
          ("Format", V["FormatDetails"] or "-"),
          ("Money", f'Fee ${V["AppearanceFee"] or "?"}  -  deposit ${V["DepositAmount"] or "0"}  -  balance ${V["BalanceAmount"] or "?"} ({d.get("balance_status") or "?"})')]
    b=['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    _cal=gcal_link(d,V)
    if _cal:
        b.append(f'<p style="margin:0 0 12px"><a href="{html.escape(_cal)}" '
                 f'style="color:#1155cc;font-weight:bold;text-decoration:underline">&#128197; calendar LINK</a></p>')
    b.append(f'<h2 style="margin:0 0 10px">Gig {html.escape(label)}: {html.escape(V["ClientFullName"] or "gig")}</h2>')
    b.append('<table style="border-collapse:collapse">')
    for k,val in rows:
        b.append(f'<tr><td style="padding:3px 14px 3px 0;color:#5f6368;vertical-align:top"><b>{html.escape(k)}</b></td>'
                 f'<td style="padding:3px 0">{html.escape(str(val))}</td></tr>')
    b.append('</table>')
    b.append('<p style="margin-top:14px;color:#5f6368">Prep: confirm arrival time &amp; parking &middot; pack props &middot; '
             'custom poster / trivia ready &middot; route mapped.</p></div>')
    return "".join(b)

def trivia_notify_html(d, V, crm_url, cal_url, answers):
    """Notification TO Simon when a client submits the trivia form: deal link, calendar link, all Q&A."""
    b=['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    b.append(f'<p style="margin:0 0 8px"><a href="{html.escape(crm_url)}" '
             f'style="color:#1155cc;font-weight:bold;text-decoration:underline">&#128220; Open this deal in the CRM</a></p>')
    if cal_url:
        b.append(f'<p style="margin:0 0 12px"><a href="{html.escape(cal_url)}" '
                 f'style="color:#1155cc;font-weight:bold;text-decoration:underline">&#128197; calendar LINK</a></p>')
    who=V.get("ClientFullName") or "the client"
    when=V.get("ShowDate") or "-"
    b.append(f'<h2 style="margin:0 0 4px">Trivia received from {html.escape(who)}</h2>')
    b.append(f'<p style="margin:0 0 14px;color:#5f6368">For {html.escape(when)}</p>')
    b.append('<table style="border-collapse:collapse;width:100%;max-width:640px">')
    for key,q in TRIVIA_QUESTIONS:
        a=answers.get(key)
        a=("" if a is None else str(a)).strip()
        val = html.escape(a) if a else '<em style="color:#9aa0a6">(not answered)</em>'
        b.append(f'<tr><td style="padding:9px 0;border-top:1px solid #eee">'
                 f'<div style="color:#5f6368;font-size:12px;margin-bottom:3px"><b>{html.escape(q)}</b></div>'
                 f'<div>{val}</div></td></tr>')
    b.append('</table></div>')
    return "".join(b)

WD=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
MO=["January","February","March","April","May","June","July","August","September","October","November","December"]
def _ord(n):
    return f"{n}{'th' if 11<=n%100<=13 else {1:'st',2:'nd',3:'rd'}.get(n%10,'th')}"
def _d(v):
    if v is None: return None
    if isinstance(v,(datetime.date,datetime.datetime)): return v.date() if isinstance(v,datetime.datetime) else v
    return datetime.date.fromisoformat(str(v)[:10])
def _num(v):
    try: return f"{float(v):,.0f}"
    except: return ""

# Name capitalization rule (global for email templates): capitalize the first letter of a name, but
# leave names that already have 2+ capitals untouched (MaryClaire, D'Arcy, CBRE, J.G., initials).
def _cap_first(s):
    if not s: return s or ""
    if sum(1 for ch in s if ch.isupper()) >= 2: return s
    for i, ch in enumerate(s):
        if ch.isalpha(): return s[:i] + ch.upper() + s[i+1:]
    return s
def _cap_full(contact):
    f = contact.get("first_name"); l = contact.get("last_name"); full = contact.get("full_name")
    if f and l: return _cap_first(f) + " " + _cap_first(l)
    if full:    return _cap_first(full)
    return _cap_first(f or "")

def merge_values(deal, contact):
    sd=_d(deal.get("show_date"))
    first = (contact.get("first_name") or (contact.get("full_name") or "").split(" ")[0] or "")
    first = re.sub(r"(^|\s)(\S)", lambda m: m.group(1)+m.group(2).upper(), first)   # always capitalize first names
    V={
        "ClientFirstName":first, "ClientFullName":_cap_full(contact),
        "ClientEmail":contact.get("email") or "", "ClientPhone":contact.get("phone_mobile") or contact.get("phone_other") or "",
        "ShowDate": (f"{WD[sd.weekday()]}, {MO[sd.month-1]} {_ord(sd.day)}, {sd.year}" if sd else ""),
        "ShowDateShort": (f"{MO[sd.month-1][:3]} {sd.day}, {sd.year}" if sd else ""),
        "ShowDay": (WD[sd.weekday()] if sd else ""), "ShowTime": deal.get("show_time") or "",
        "Venue": deal.get("venue_address") or "", "Occasion": deal.get("occasion") or "",
        "EventDetails": deal.get("audience_details") or "", "FormatDetails": deal.get("show_format") or "",
        "AppearanceFee": _num(deal.get("amount")), "DepositAmount": _num(deal.get("deposit_amount")),
        "BalanceAmount": _num(deal.get("balance_amount")),
        "Month": (MO[sd.month-1] if sd else MO[TODAY.month-1]), "Year": str(sd.year if sd else TODAY.year),
        "Company": deal.get("company") or "", "GuestOfHonor": deal.get("guest_of_honor") or "",
        "ProposalLink": deal.get("proposal_link") or "",
        "CustomizeLink": (f"{CUSTOMIZE_BASE}?t={deal.get('customize_token')}" if deal.get("customize_token") else ""),
        "PerformerName": (PERF.get(deal.get("performer_id")) or {}).get("first_name") or "",
        "EventType": (deal.get("event_type","").replace("_"," ").title()+" event") if deal.get("event_type") else "event",
        "ProposalSubject": deal.get("deal_name") or deal.get("occasion") or "your event",
        "ThreadSubject": deal.get("deal_name") or deal.get("occasion") or "your event",
        "LastShowYear": "",
    }
    return V

def fill_subject(s, V):
    return re.sub(r"\{\{(\w+)\}\}", lambda m: V[m.group(1)] if V.get(m.group(1)) else m.group(0), s or "")

def _lists_and_breaks(s):
    """Group consecutive '- ' lines into a <ul>; join the rest with <br> (same as before)."""
    lines = s.split("\n")
    out = []; prev_text = False; i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("- "):
            items = ""
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                items += "<li>" + lines[i].lstrip()[2:] + "</li>"
                i += 1
            out.append('<ul style="margin:6px 0;padding-left:22px;">' + items + "</ul>")
            prev_text = False
        else:
            if prev_text: out.append("<br>")
            out.append(lines[i]); prev_text = True; i += 1
    return "".join(out)

def render_html(raw, V, signature):
    raw = _drop_empty_optional_lines(raw, V)   # blank EventDetails etc. -> drop the whole line
    s = html.escape(raw or "")
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    # markdown links: target may be a literal https URL OR a {{Field}} that resolves to a URL
    def _link(m):
        text, field = m.group(1), m.group(3)
        url = (V.get(field) or "") if field else m.group(2)
        return f'<a href="{html.escape(url)}">{text}</a>' if url else text
    s = re.sub(r"\[([^\]]+)\]\((\{\{(\w+)\}\}|https?://[^)]+)\)", _link, s)
    s = re.sub(r"\{\{(\w+)\}\}", lambda m: html.escape(V[m.group(1)]) if V.get(m.group(1)) else m.group(0), s)
    s = _lists_and_breaks(s)
    if signature:
        s += "<br><br>" + signature   # signature is trusted raw HTML (image + links), do NOT escape
    return s

def anchor_date(deal, anchor, stages):
    if stages and deal.get("stage") not in stages: return None
    if anchor=="show":    return _d(deal.get("show_date"))
    if anchor=="stage":   return _d(deal.get("stage_changed_at"))
    if anchor=="proposal_sent": return _d(deal.get("proposal_sent_at"))   # follow-ups start when the proposal was sent
    if anchor=="created": return _d(deal.get("created_at"))
    return None

def main():
    c=connect(); c.autocommit=True; cur=c.cursor()
    # PHASE-1 RESILIENCE: never let one bad send crash the whole run. Every mailer.send_email goes through a
    # wrapper that catches errors, records them, and keeps going; a summary is emailed to Simon at the end.
    _orig_send = mailer.send_email
    _send_fails = []
    def _safe_send(to, subj, body, **kw):
        try:
            return _orig_send(to, subj, body, **kw)
        except Exception as _e:
            _send_fails.append((str(to), str(subj), str(_e)[:300]))
            print("  !! SEND FAILED:", to, "-", str(_e)[:200])
            return None
    mailer.send_email = _safe_send   # covers every send site in this run without touching each call
    cols_d=["id","stage","show_date","show_time","venue_address","occasion","company","guest_of_honor",
            "proposal_link","audience_details","show_format","amount","deposit_amount","balance_amount",
            "event_type","is_repeat","customize_token","trivia","trivia_received_at","trivia_notified_at",
            "performer_id","commission_amount","proposal_sent_at","photos_received_at","photos_notified_at",
            "deal_name","cue_state","stage_changed_at","created_at","primary_contact_id","gcal_url",
            "deposit_status","deposit_paid_at"]
    cur.execute("select "+",".join(cols_d)+" from deals")
    deals=[dict(zip(cols_d,r)) for r in cur.fetchall()]
    cur.execute("select id,first_name,last_name,full_name,email,phone_mobile,phone_other from contacts")
    CB={r[0]:dict(zip(["id","first_name","last_name","full_name","email","phone_mobile","phone_other"],r)) for r in cur.fetchall()}
    global PERF
    cur.execute("select id, first_name, full_name from performers")
    PERF={r[0]:{"first_name":r[1],"full_name":r[2]} for r in cur.fetchall()}
    cur.execute("select key,subject,body_html from templates where active=true")
    TPL={r[0]:{"subject":r[1],"body":r[2]} for r in cur.fetchall()}
    signature=TPL.get("_signature",{}).get("body","")
    # SEQ_START = migration cutover = the SEND-DATE floor (the day the new CRM took over from Zoho).
    # Emails scheduled before it are never sent; today's + any missed-since-cutover are sent/caught up.
    cur.execute("select value from private.config where key='sequencer_start'")
    _r=cur.fetchone(); SEQ_START = datetime.date.fromisoformat(_r[0]) if _r and _r[0] else TODAY
    # APPROVAL MODE: when on, auto emails are HELD (not sent) and Simon approves each via the authorize email.
    try:
        cur.execute("select approval_mode from settings where id=1")
        _am=cur.fetchone(); APPROVAL_MODE=bool(_am and _am[0])
    except Exception:
        APPROVAL_MODE=False

    due=[]; blocked_today=[]; new_blocks=[]; held_new=[]
    for d in deals:
        st = d.get("cue_state") or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        d["cue_state"]=st   # make in-memory cue_state the single source of truth across the passes below
        contact = CB.get(d.get("primary_contact_id")) or {}
        for (key,anchor,off,mode,stages) in CUE:
            # customization/trivia chase stops once the trivia has been received
            if key=="customization_request" and d.get("trivia_received_at"): continue
            # refer-won check-ins need a performer linked first
            if key in ("refer_won_daybefore","refer_won_after") and not d.get("performer_id"): continue
            # repeat clients: post-show emails become FLAG/manual (don't auto-send the same note yearly)
            eff = "flag" if (d.get("is_repeat") and anchor=="show" and off>0) else mode
            if eff not in AUTO_MODES: continue
            t=TPL.get(key)
            if not t: continue
            base=anchor_date(d,anchor,stages)
            if not base: continue
            send_date = base + datetime.timedelta(days=off)
            # Migration cutover: SEQ_START is the send-date FLOOR (the day the new CRM took over from Zoho).
            # Skip anything scheduled BEFORE it (Zoho's backlog - permanently). Send today's, and CATCH UP any
            # scheduled on/after the floor that slipped (missed). Never re-send the pre-cutover backlog.
            if send_date < SEQ_START or send_date > TODAY: continue
            e = st.get(key) or {}
            if e.get("sent") or e.get("cancelled"): continue
            if not contact.get("email"): continue
            V=merge_values(d,contact)
            blanks = missing_fields(t, e, V)
            if blanks:
                # PAUSE: a required merge field is blank -> do NOT send. Flag it so blocked_digest.py
                # nags Simon daily and the re-check pass below auto-sends it once the field is filled.
                was_blocked = bool(e.get("blocked"))
                st[key] = {**e, "blocked": {"since": (e.get("blocked") or {}).get("since", TODAY.isoformat()), "fields": blanks}}
                blocked_today.append((d, key, contact["email"], blanks))
                if not was_blocked:   # first time this email is paused -> immediate alert to Simon (below)
                    new_blocks.append((contact.get("full_name") or d.get("deal_name") or "deal", key, blanks, d["id"]))
                if SEND:
                    cur.execute("update deals set cue_state=%s where id=%s", (json.dumps(st), d["id"]))
                continue
            if e.get("blocked"):                          # was paused, now complete -> clear the flag before sending
                e = {k: v for k, v in e.items() if k != "blocked"}; st[key] = e
            subj = e["subject"] if e.get("subject") is not None else fill_subject(t["subject"],V)
            body = e["body"] if e.get("body") is not None else render_html(t["body"],V,signature)
            if APPROVAL_MODE:
                # HOLD for approval: never auto-send. Store the email WITHOUT the signature - the authorize
                # email shows a clean preview, and the signature is added inline (CID image) on Approve so it
                # renders without "display images". Store a token for the Approve/Cancel links.
                was_pending = bool(e.get("pending_approval"))
                token = e.get("approve_token") or secrets.token_urlsafe(24)
                hbody = e["body"] if e.get("body") is not None else render_html(t["body"], V, "")   # no signature
                st[key] = {**e, "pending_approval": True, "approve_token": token,
                           "to": contact["email"], "subject": subj, "body": hbody,
                           "pending_since": e.get("pending_since") or TODAY.isoformat()}
                if not was_pending:
                    held_new.append((contact.get("full_name") or d.get("deal_name") or "deal", key, contact["email"], subj, hbody, token, d["id"]))
                if SEND:
                    cur.execute("update deals set cue_state=%s where id=%s", (json.dumps(st), d["id"]))
                continue
            due.append((d,key,contact["email"],subj,body,st))

    # CLIENT CADENCE WINDOW (Simon 2026-07-04): only send the due-today client emails between
    # 10am and 12pm Eastern, so they cluster at a consistent time each day. Send-now, the
    # trivia/photo notifications, self gig check-ins and the self-nags are NOT gated - they run
    # whenever the sweep runs. Pass --anytime to bypass the window (manual / one-off runs).
    _cad_ok = ('--anytime' in sys.argv) or (10 <= tz.hour() <= 12)
    print(f"{TODAY}  -  {len(due)} email(s) due today  (mode: {'SEND' if SEND else 'DRY-RUN'})"
          + ('' if _cad_ok else f'  [holding: outside 10am-12pm ET window, now {tz.hour()}:00 ET]'))
    for (d,key,to,subj,body,st) in due:
        print(f"  -> {to}  |  [{key}]  {subj[:70]}")
        if SEND and _cad_ok:
            mailer.send_email(to, subj, body)
            ne={**(st.get(key) or {}), "sent":TODAY.isoformat()}; ne.pop("blocked",None); st[key]=ne
            cur.execute("update deals set cue_state=%s where id=%s",(json.dumps(st), d["id"]))
    if SEND and _cad_ok and due: print("marked sent + saved.")
    if blocked_today:
        print(f"{TODAY}  -  {len(blocked_today)} auto email(s) PAUSED for missing fields:")
        for (d,key,to,blanks) in blocked_today:
            print(f"  -> [paused] {to}  |  [{key}]  missing: {', '.join(blanks)}")
    if new_blocks:
        print(f"{TODAY}  -  {len(new_blocks)} NEWLY paused this run -> immediate alert to Simon")
        if SEND:
            mailer.send_email("simon@thesimonshow.com",
                              f"{len(new_blocks)} email(s) paused - missing info",
                              _blocked_alert_html(new_blocks))
    if held_new:
        print(f"{TODAY}  -  {len(held_new)} email(s) HELD for approval -> individual authorize emails to Simon")
        for row in held_new:
            nm,key,to,subj,body,token,did = row
            print(f"  -> [held] {to}  |  [{key}]  {subj[:60]}")
            if SEND:
                mailer.send_email("simon@thesimonshow.com",
                                  f"CRM Approval for {nm}: {subj}",
                                  _authorize_email_html([row]), owner=True)   # one authorize email per held email

    # ---- SEND NOW: emails the app flagged for immediate send (cue_state[key].send_now) ----
    # The "Send now" button in the CRM sets cue_state[key].send_now = true and kicks a run.
    # We send that specific email regardless of its normal schedule date, then clear the flag.
    now_due=[]
    for d in deals:
        st = d.get("cue_state") or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        contact = CB.get(d.get("primary_contact_id")) or {}
        for key, e in list(st.items()):
            if not isinstance(e, dict) or not e.get("send_now"): continue
            if e.get("sent") or e.get("cancelled"): continue
            t=TPL.get(key)
            to = (e.get("to") or contact.get("email") or "").strip()   # app can override the recipient
            if not to: continue
            V=merge_values(d,contact)
            # use the app-provided subject/body when present, else fall back to the template.
            subj = e["subject"] if e.get("subject") is not None else (fill_subject(t["subject"],V) if t else None)
            body = e["body"]    if e.get("body")    is not None else (render_html(t["body"],V,signature) if t else None)
            if subj is None or body is None: continue   # no template and no override -> nothing to send
            now_due.append((d,key,to,subj,body,st))
    print(f"{TODAY}  -  {len(now_due)} send-now email(s)  (mode: {'SEND' if SEND else 'DRY-RUN'})")
    for (d,key,to,subj,body,st) in now_due:
        print(f"  -> [now] {to}  |  [{key}]  {subj[:70]}")
        if SEND:
            mailer.send_email(to, subj, body)
            ne={**(st.get(key) or {}), "sent":TODAY.isoformat()}; ne.pop("send_now",None)
            st[key]=ne
            cur.execute("update deals set cue_state=%s where id=%s",(json.dumps(st), d["id"]))
    if SEND and now_due: print("send-now emails sent + flags cleared.")

    # ---- BLOCKED re-check: emails paused for a missing field. Auto-send any that are now complete
    #      (Simon filled the field), keep the rest paused (blocked_digest.py nags him daily). Runs
    #      regardless of the original send date, so a fixed email still goes out even a day or two
    #      late; a stale one Simon no longer wants is stopped with the cue's Cancel (x) button. ----
    unblock=[]
    for d in deals:
        st = d.get("cue_state") or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        d["cue_state"]=st
        contact = CB.get(d.get("primary_contact_id")) or {}
        for key, e in list(st.items()):
            if not isinstance(e, dict) or not e.get("blocked"): continue
            if e.get("sent") or e.get("cancelled"):        # already handled -> drop the stale flag
                e.pop("blocked",None); st[key]=e
                if SEND: cur.execute("update deals set cue_state=%s where id=%s",(json.dumps(st), d["id"]))
                continue
            t=TPL.get(key)
            if not t: continue
            to=(e.get("to") or contact.get("email") or "").strip()
            if not to: continue
            V=merge_values(d,contact)
            blanks = missing_fields(t, e, V)
            if blanks:                                     # still missing -> stay paused, refresh field list if it changed
                if (e.get("blocked") or {}).get("fields") != blanks:
                    e["blocked"]={"since":(e.get("blocked") or {}).get("since",TODAY.isoformat()),"fields":blanks}; st[key]=e
                    if SEND: cur.execute("update deals set cue_state=%s where id=%s",(json.dumps(st), d["id"]))
                continue
            subj = e["subject"] if e.get("subject") is not None else fill_subject(t["subject"],V)
            body = e["body"] if e.get("body") is not None else render_html(t["body"],V,signature)
            unblock.append((d,key,to,subj,body,st))
    print(f"{TODAY}  -  {len(unblock)} paused email(s) now complete -> sending")
    for (d,key,to,subj,body,st) in unblock:
        print(f"  -> [unblocked] {to}  |  [{key}]  {subj[:60]}")
        if SEND:
            mailer.send_email(to,subj,body)
            ne={**(st.get(key) or {}), "sent":TODAY.isoformat()}; ne.pop("blocked",None); ne.pop("send_now",None)
            st[key]=ne
            cur.execute("update deals set cue_state=%s where id=%s",(json.dumps(st), d["id"]))
    if SEND and unblock: print("unblocked emails sent.")

    # ---- GCal!: on Closed Won, email Simon a calendar link every day until he saves the event URL ----
    gcal_due = []
    for d in deals:
        if d.get("stage") != "closed_won": continue
        if d.get("gcal_url"): continue                       # done - Simon saved the event URL
        sd = _d(d.get("show_date"))
        if not sd or sd < TODAY: continue                    # only upcoming gigs need a calendar entry
        mv = _d(d.get("stage_changed_at"))
        if not mv or mv < SEQ_START: continue                # only deals moved to Closed Won since go-live
        st = d.get("cue_state") or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        g = st.get("_gcal") or {}
        if g.get("last") == TODAY.isoformat(): continue      # once per day
        token = g.get("token") or secrets.token_urlsafe(18)
        gcal_due.append((d, token, not g.get("first_sent"), st))
    print(f"{TODAY}  -  {len(gcal_due)} GCal! reminder(s) due")
    for (d, token, first, st) in gcal_due:
        contact = CB.get(d.get("primary_contact_id")) or {}
        V = merge_values(d, contact)
        who = V.get("ClientFullName") or d.get("deal_name") or "booking"
        when = V.get("ShowDateShort") or str(d.get("show_date"))
        subj = ("GCal! - %s - %s" % (who, when)) if first else ("Reminder: add to calendar - %s - %s" % (who, when))
        print("  -> [gcal] %s" % subj[:70])
        if SEND:
            mailer.send_email("simon@thesimonshow.com", subj, _gcal_email_html(d, V, token, reminder=not first), owner=True)
            g = {**(st.get("_gcal") or {}), "token": token, "last": TODAY.isoformat()}
            if first: g["first_sent"] = TODAY.isoformat()
            st["_gcal"] = g
            cur.execute("update deals set cue_state=%s where id=%s", (json.dumps(st), d["id"]))
    if SEND and gcal_due: print("gcal reminders sent.")

    # ---- Magic Castle invite: ONE per deal (shared marker cue_state['magic_castle']). Closed-Won version
    #      fires at Closed Won (priority); deposit version ~10 min after the deposit is marked paid. Whichever
    #      fires first claims the marker; the other is suppressed. Has an attachment, so it goes via
    #      mailer.send_email (safe mode routes it to Simon for auth) - NOT the approve/hold flow. ----
    _now = datetime.datetime.now(datetime.timezone.utc)
    mc_due = []
    for d in deals:
        st = d.get("cue_state") or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        e = st.get("magic_castle") or {}
        if e.get("sent") or e.get("cancelled"): continue          # already went (or cancelled) - one per deal
        contact = CB.get(d.get("primary_contact_id")) or {}
        if not contact.get("email"): continue
        version = None
        mv = _d(d.get("stage_changed_at"))
        if d.get("stage")=="closed_won" and mv and mv >= SEQ_START:
            version = "magic_castle_cw"                            # Closed-Won version wins if both apply
        else:
            _p = d.get("deposit_paid_at"); paid = None
            if isinstance(_p, datetime.datetime):
                paid = _p if _p.tzinfo else _p.replace(tzinfo=datetime.timezone.utc)
            if d.get("deposit_status")=="paid" and paid and _now >= paid + datetime.timedelta(minutes=10) and paid.date() >= SEQ_START:
                version = "magic_castle"                           # deposit version, ~10 min after paid
        if not version: continue
        t = TPL.get(version)
        if not t: continue
        V = merge_values(d, contact)
        subj = fill_subject(t["subject"], V)
        body = render_html(t["body"], V, signature)
        atts = attachments.attachments_for("magic_castle", d, contact)
        mc_due.append((d, contact["email"], subj, body, atts, version, st))
    print(f"{TODAY}  -  {len(mc_due)} magic castle invite(s) due")
    for (d, to, subj, body, atts, version, st) in mc_due:
        print("  -> [magic:%s] %s  |  %s  |  att=%s" % (version, to, subj[:46], [a[0] for a in atts]))
        if SEND:
            mailer.send_email(to, subj, body, attachments=atts)
            st["magic_castle"] = {**(st.get("magic_castle") or {}), "sent": TODAY.isoformat(), "via": version}
            cur.execute("update deals set cue_state=%s where id=%s", (json.dumps(st), d["id"]))
    if SEND and mc_due: print("magic castle invites sent.")

    # ---- W9: on Closed Won for CORPORATE deals (event_type='corporate'), send the W9 once, with its PDF
    #      attachment. Like the Magic Castle it goes via mailer.send_email (safe mode -> Simon for auth),
    #      not the CUE/approve flow which can't carry attachments. One per deal (cue_state['w9_email']). ----
    w9_due = []
    for d in deals:
        if d.get("event_type") != "corporate": continue
        if d.get("stage") != "closed_won": continue
        mv = _d(d.get("stage_changed_at"))
        if not mv or mv < SEQ_START: continue                 # only deals moved to Closed Won since go-live
        st = d.get("cue_state") or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        e = st.get("w9_email") or {}
        if e.get("sent") or e.get("cancelled"): continue
        contact = CB.get(d.get("primary_contact_id")) or {}
        if not contact.get("email"): continue
        t = TPL.get("w9_email")
        if not t: continue
        V = merge_values(d, contact)
        w9_due.append((d, contact["email"], fill_subject(t["subject"],V), render_html(t["body"],V,signature),
                       attachments.attachments_for("w9_email", d, contact), st))
    print(f"{TODAY}  -  {len(w9_due)} W9 email(s) due (corporate Closed Won)")
    for (d, to, subj, bodyw, atts, st) in w9_due:
        print("  -> [w9] %s  |  %s  |  att=%s" % (to, subj[:46], [a[0] for a in atts]))
        if SEND:
            mailer.send_email(to, subj, bodyw, attachments=atts)
            st["w9_email"] = {**(st.get("w9_email") or {}), "sent": TODAY.isoformat()}
            cur.execute("update deals set cue_state=%s where id=%s", (json.dumps(st), d["id"]))
    if SEND and w9_due: print("w9 emails sent.")

    # ---- self gig check-ins: reminders TO Simon about upcoming booked gigs ----
    self_to = mailer.load_config()["from_email"]
    self_due = []
    for d in deals:
        if d.get("stage") not in ("closed_won","booked"): continue
        sd = _d(d.get("show_date"))
        if not sd: continue
        st = d.get("cue_state") or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        contact = CB.get(d.get("primary_contact_id")) or {}
        V = merge_values(d, contact)
        for (days,label) in SELF_CHECKINS:
            if sd - datetime.timedelta(days=days) != TODAY: continue
            key=f"selfcheckin_{days}d"
            e=st.get(key) or {}
            if e.get("sent") or e.get("cancelled"): continue
            who = V["ClientFullName"] or (d.get("deal_name") or "gig")
            subj=f"[GIG {label}] {who} - {V['ShowDateShort']}"
            self_due.append((d,key,subj,selfcheckin_html(d,V,label)))
    print(f"{TODAY}  -  {len(self_due)} self gig check-in(s) due")
    for (d,key,subj,body) in self_due:
        print(f"  -> [self] {subj[:70]}")
        if SEND:
            mailer.send_email(self_to, subj, body)
            cur.execute("select cue_state from deals where id=%s",(d["id"],))   # read-modify-write (avoid clobber)
            cs=cur.fetchone()[0] or {}
            if isinstance(cs,str): cs=json.loads(cs or "{}")
            cs[key]={"sent":TODAY.isoformat()}
            cur.execute("update deals set cue_state=%s where id=%s",(json.dumps(cs),d["id"]))
    if SEND and self_due: print("self check-ins sent + marked.")

    # ---- trivia-received notifications: email Simon when a client submits the trivia form ----
    triv_to = "simon@thesimonshow.com"
    triv_due = []
    for d in deals:
        rec = d.get("trivia_received_at"); notif = d.get("trivia_notified_at")
        if not rec: continue
        if notif and notif >= rec: continue               # notify once per submission; re-notify if they resubmit
        ans = d.get("trivia") or {}
        if isinstance(ans,str): ans=json.loads(ans or "{}")
        if not ans: continue
        contact = CB.get(d.get("primary_contact_id")) or {}
        V = merge_values(d, contact)
        who  = V["ClientFullName"] or (contact.get("full_name") or "client")
        when = V["ShowDateShort"] or (str(d.get("show_date")) if d.get("show_date") else "TBD")
        subj = f"TRIVIA received from {who} for {when}"
        crm_url = f"{CRM_BASE}/?deal={d['id']}"
        cal_url = gcal_link(d, V)
        triv_due.append((d, subj, trivia_notify_html(d, V, crm_url, cal_url, ans)))
    print(f"{TODAY}  -  {len(triv_due)} trivia notification(s) due")
    for (d, subj, body) in triv_due:
        print(f"  -> [trivia] {subj[:70]}")
        if SEND:
            mailer.send_email(triv_to, subj, body)
            cur.execute("update deals set trivia_notified_at=now() where id=%s", (d["id"],))
    if SEND and triv_due: print("trivia notifications sent + marked.")

    # ---- photos-received notifications: email Simon when a client submits the photo form ----
    photo_to = "simon@thesimonshow.com"
    photo_due = []
    for d in deals:
        rec = d.get("photos_received_at"); notif = d.get("photos_notified_at")
        if not rec: continue
        if notif and notif >= rec: continue               # once per submission; re-notify on resubmit
        contact = CB.get(d.get("primary_contact_id")) or {}
        V = merge_values(d, contact)
        who  = V["ClientFullName"] or (contact.get("full_name") or d.get("deal_name") or "client")
        when = V["ShowDate"] or (str(d.get("show_date")) if d.get("show_date") else "TBD")
        subj = f"pictures received for {when} / {who}"
        crm_url = f"{CRM_BASE}/?deal={d['id']}"
        body = ('<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">'
                f'<p style="margin:0">Download pictures at <a href="{html.escape(crm_url)}" '
                f'style="color:#1155cc;font-weight:bold;text-decoration:underline">{html.escape(crm_url)}</a></p></div>')
        photo_due.append((d, subj, body))
    print(f"{TODAY}  -  {len(photo_due)} photo notification(s) due")
    for (d, subj, body) in photo_due:
        print(f"  -> [photos] {subj[:70]}")
        if SEND:
            mailer.send_email(photo_to, subj, body)
            cur.execute("update deals set photos_notified_at=now() where id=%s", (d["id"],))
    if SEND and photo_due: print("photo notifications sent + marked.")

    # PHASE-1: if any individual send failed above, email Simon one summary (the run itself did not crash).
    if SEND and _send_fails:
        _body = ('<div style="font-family:sans-serif;font-size:14px;color:#111">'
                 '<p>&#9888; <b>%d email send(s) failed</b> in this run (the sweep kept going and finished):</p><ul>'
                 % len(_send_fails)
                 + "".join('<li>%s &middot; %s<br><span style="color:#c0392b;font-size:12px">%s</span></li>'
                           % (html.escape(t), html.escape(s), html.escape(e)) for t,s,e in _send_fails)
                 + '</ul></div>')
        try:
            _orig_send("simon@thesimonshow.com", "[CRM] %d email send(s) FAILED" % len(_send_fails), _body, owner=True)
            print("failure-summary email sent.")
        except Exception as _ex:
            print("failure-summary email ALSO failed:", _ex)
    c.close()

if __name__=="__main__":
    main()
