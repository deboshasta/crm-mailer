# -*- coding: utf-8 -*-
"""Nightly sender: walk every deal's cue, send the AUTO/WINDOW emails due TODAY that
aren't cancelled or already sent, and mark them sent. FLAG-mode emails are never
auto-sent (they are Focus reminders). Dry-run by default; pass --send to actually send.
Safe-mode still routes every send to Simon until mail_safe_mode is turned off.
"""
import sys, json, html, re, datetime, urllib.parse
from db import connect
import mailer
import tz

SEND = "--send" in sys.argv
TODAY = tz.today()          # Eastern "today" so cloud (UTC) runs match Simon's local day

# per-deal customization/trivia form base (must match app.js CUSTOMIZE_BASE).
# Update to the real deployed host at go-live.
CUSTOMIZE_BASE = "https://www.thesimonshow.com/trivia.html"

# CRM app base for deep-links in notification emails (?deal=<id> opens that deal).
# Local-only for now; update if/when the CRM is hosted somewhere reachable.
CRM_BASE = "http://localhost:5610"

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
    ("thank_you","show",1,"window",("closed_won",)),
    ("review_request","show",2,"window",("closed_won",)),
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
    ("refer","stage",0,"flag",("refer",)),
    ("closed_lost_daybefore","show",-1,"flag",("closed_lost",)),
    ("closed_lost_after","show",2,"flag",("closed_lost",)),
    ("refer_won_daybefore","show",-1,"auto",("refer_won",)),
    ("refer_won_after","show",2,"auto",("refer_won",)),
]
AUTO_MODES = ("auto","window")

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

def gcal_link(d, V):
    """Google Calendar add-event link prefilled from the deal (the gcal! generated link)."""
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

def merge_values(deal, contact):
    sd=_d(deal.get("show_date"))
    first = (contact.get("first_name") or (contact.get("full_name") or "").split(" ")[0] or "")
    first = re.sub(r"(^|\s)(\S)", lambda m: m.group(1)+m.group(2).upper(), first)   # always capitalize first names
    V={
        "ClientFirstName":first, "ClientFullName":contact.get("full_name") or "",
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
    cols_d=["id","stage","show_date","show_time","venue_address","occasion","company","guest_of_honor",
            "proposal_link","audience_details","show_format","amount","deposit_amount","balance_amount",
            "event_type","is_repeat","customize_token","trivia","trivia_received_at","trivia_notified_at",
            "performer_id","commission_amount","proposal_sent_at","photos_received_at","photos_notified_at",
            "deal_name","cue_state","stage_changed_at","created_at","primary_contact_id"]
    cur.execute("select "+",".join(cols_d)+" from deals")
    deals=[dict(zip(cols_d,r)) for r in cur.fetchall()]
    cur.execute("select id,first_name,full_name,email,phone_mobile,phone_other from contacts")
    CB={r[0]:dict(zip(["id","first_name","full_name","email","phone_mobile","phone_other"],r)) for r in cur.fetchall()}
    global PERF
    cur.execute("select id, first_name, full_name from performers")
    PERF={r[0]:{"first_name":r[1],"full_name":r[2]} for r in cur.fetchall()}
    cur.execute("select key,subject,body_html from templates where active=true")
    TPL={r[0]:{"subject":r[1],"body":r[2]} for r in cur.fetchall()}
    signature=TPL.get("_signature",{}).get("body","")
    cur.execute("select value from private.config where key='sequencer_start'")
    _r=cur.fetchone(); SEQ_START = datetime.date.fromisoformat(_r[0]) if _r and _r[0] else TODAY

    due=[]
    for d in deals:
        st = d.get("cue_state") or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        contact = CB.get(d.get("primary_contact_id")) or {}
        if _d(d.get("created_at")) < SEQ_START: continue   # go-live cutoff: never backfill pre-existing deals
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
            if send_date != TODAY: continue
            e = st.get(key) or {}
            if e.get("sent") or e.get("cancelled"): continue
            if not contact.get("email"): continue
            V=merge_values(d,contact)
            subj = e["subject"] if e.get("subject") is not None else fill_subject(t["subject"],V)
            body = e["body"] if e.get("body") is not None else render_html(t["body"],V,signature)
            due.append((d,key,contact["email"],subj,body,st))

    print(f"{TODAY}  -  {len(due)} email(s) due today  (mode: {'SEND' if SEND else 'DRY-RUN'})")
    for (d,key,to,subj,body,st) in due:
        print(f"  -> {to}  |  [{key}]  {subj[:70]}")
        if SEND:
            mailer.send_email(to, subj, body)
            st[key]={**(st.get(key) or {}), "sent":TODAY.isoformat()}
            cur.execute("update deals set cue_state=%s where id=%s",(json.dumps(st), d["id"]))
    if SEND and due: print("marked sent + saved.")

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
    c.close()

if __name__=="__main__":
    main()
