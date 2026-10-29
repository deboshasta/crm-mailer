# -*- coding: utf-8 -*-
"""Nightly sender: walk every deal's cue, send the AUTO/WINDOW emails due TODAY that
aren't cancelled or already sent, and mark them sent. FLAG-mode emails are never
auto-sent (they are Focus reminders). Dry-run by default; pass --send to actually send.
Safe-mode still routes every send to Simon until mail_safe_mode is turned off.
"""
import sys, os, json, html, re, datetime, urllib.parse, secrets, base64
from db import connect
import mailer
import tz
import attachments
import heartbeat

SEND = "--send" in sys.argv
TODAY = tz.today()          # Eastern "today" so cloud (UTC) runs match Simon's local day
# ⚠ UTC, deliberately, and only used for the Pie Control hold (step 3). TODAY is Eastern because it
# is compared against stored DATES; the hold is a DURATION in minutes, so it needs an unambiguous
# instant — and pie_ack.seen_at is timestamptz. Mixing the two is how an hour becomes five.
NOW_UTC = datetime.datetime.now(datetime.timezone.utc)

def _now_iso():
    """Absolute send instant (UTC, with offset) stored alongside the 'sent' date so the app can show
    the sent TIME. UTC so it's unambiguous; the app converts it to Eastern for display."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

# per-deal customization/trivia form base (must match app.js CUSTOMIZE_BASE).
# Hosted on the dedicated forms.thesimonshow.com Vercel project (repo crm-forms).
CUSTOMIZE_BASE = "https://forms.thesimonshow.com/trivia.html"
PHOTO_BASE = "https://forms.thesimonshow.com/custom-images.html"   # photo upload form (must match app.js PHOTO_BASE)

# CRM app base for deep-links in notification emails (?deal=<id> opens that deal).
CRM_BASE = "https://crm.thesimonshow.com"
# Approval-mode: Approve/Cancel buttons in the authorize email point at this Vercel endpoint.
APPROVE_BASE = "https://crm-send-the-simon-show.vercel.app/api/approve-email"
# GCal!: the "Update GCal link" button opens this paste page to store the calendar-event URL.
GCAL_PASTE_BASE = "https://crm-send-the-simon-show.vercel.app/api/gcal-link"

# trivia questions (full text) - keys/labels must match trivia.html (crm-forms).
# One set per situation; the mailer picks the SAME set the form showed the client
# (guest-of-honor takes precedence, then event_type) so Simon's summary lines up.
TRIVIA_SETS = {
    "goh": [
        ("goh_number",           "Is there a number that means something to the guest of honor? A jersey number, a year, how many grandchildren, how many years doing what they do — anything at all.", "num"),
        ("special_location",     "What is a location that has special meaning to the guest of honor?", "place"),
        ("location_why",         "Why is this location special? Please be specific.", "place"),
        ("best_qualities",       "What are some of the guest of honor's best qualities?", "people"),
        ("hobbies",              "What are some of the guest of honor's hobbies?", "people"),
        ("everyone_knows",       "What are a few things everyone knows about the guest of honor?", "people"),
        ("no_one_knows",         "What are a few things almost no-one knows about the guest of honor? (Looking for obscure facts, nothing embarrassing please)", "people"),
        ("accomplishments",      "What are some of the guest of honor's biggest accomplishments?", "people"),
        ("goh_lots",             "What does the guest of honor have a lot of? Records, tools, shoes, mugs, stories about one particular thing — anything they have more of than most people. Roughly how many?", "lots"),
        ("anything_else",        "Anything else you'd like me to know about the guest of honor?", "more"),
    ],
    "nonprofit": [
        ("np_number",            "What is a number that has significance to your organization? (For example: we donated 125 kidneys, or we built 32 libraries.)", "num"),
        ("np_place",             "What is a place that is important to your organization, and why?", "place"),
        ("np_people",            "Who is someone everyone in your organization knows — a founder, a long-time volunteer, a board member? What are two or three things they do outside of this work?", "people"),
        ("np_lots",              "What is there a LOT of in your world? Volunteers, meals, boxes, phone calls, forms — and roughly how many?", "lots"),
        ("np_goals",             "What are some of your goals for this year?", "more"),
        ("np_accomplishments",   "What are some recent accomplishments of your organization?", "more"),
        ("np_obscure",           "What is an obscure fact about your organization, or your mission?", "more"),
        ("np_good",              "What are some good things that come from people supporting your organization?", "more"),
        ("np_anything",          "Anything else you'd like me to know?", "more"),
    ],
    "company": [
        ("co_big_number",        "What is a big number your company is proud of? Customers served, projects completed, years in business, cups of coffee — anything you count.", "num"),
        ("co_inside_number",     "Is there a number only people inside the company would recognise? A room number, a model number, the year you started, an extension everyone knows by heart.", "num"),
        ("co_location",          "What is a special location to your company? What is special about it?", "place"),
        ("co_known_person",      "Is there someone everyone in the company knows? (A leader, a long-timer, the person at the front desk — anyone at all. Feel free to skip this one if nobody springs to mind.)", "people"),
        ("co_person_hobbies",    "If you named someone above — what are two or three things they do outside of work? (Golf, guitar, marathons — specific activities are perfect.)", "people"),
        ("co_person_qualities",  "What are some of this person's good qualities, or accomplishments?", "people"),
        ("co_lots",              "What is there a LOT of at your company? Clients, trucks, spreadsheets, coffee cups, acronyms — and roughly how many?", "lots"),
        ("co_signature_thing",   "What is one specific product, service, or thing your company is known for? (The exact one, please — the more specific, the better.)", "more"),
        ("co_motto",             "What is a motto or slogan that is known within your company?", "more"),
        ("co_surprising",        "What is something most people would find surprising about your company or industry?", "more"),
        ("co_accomplished",      "What is something your company has accomplished this year?", "more"),
    ],
    "school": [
        ("sc_number",            "Is there a number everyone at the school knows? The size of the graduating class, a jersey number, the year the school opened, how many days until something big.", "num"),
        ("sc_place",             "What is a place at or near the school that everyone knows? What happens there?", "place"),
        ("sc_people",            "Who are some interesting people who graduated from the school, or some interesting people who visited recently?", "people"),
        ("sc_admired",           "Who is a teacher, coach, or staff member that many students admire, and why?", "people"),
        ("sc_lots",              "What is there a LOT of at your school? Students, trophies, lockers, stairs, spirit days — and roughly how many?", "lots"),
        ("sc_learning",          "What are some things that all the students are learning about as a school?", "more"),
        ("sc_accomplishments",   "What are some accomplishments within the school that everyone is familiar with? (For example: a student won a national award for a scientific discovery, or a team finally beat a big rival school.)", "more"),
        ("sc_goals",             "What are some goals that the students are working toward together as a group?", "more"),
        ("sc_tradition",         "What is a tradition or event that everyone at the school looks forward to?", "more"),
        ("sc_known_for",         "What is something your school is especially known for?", "more"),
        ("sc_anything",          "Anything else you'd like me to know?", "more"),
    ],
    "private": [
        ("pp_number",            "Is there a number that means something to this group? A house number, how many years you have known each other, how many kids between you, a date everyone remembers.", "num"),
        ("pp_place",             "What is a place that means something to this group? What happened there?", "place"),
        ("pp_funfacts",          "What are some fun facts about individuals who will be attending the party? (Please include their names.)", "people"),
        ("pp_longest",           "Who has known each other the longest? Tell me a bit about them.", "people"),
        ("pp_newest",            "Who is newest to the group? Tell me their name and a bit about them.", "people"),
        ("pp_celebrating",       "Is anyone attending celebrating something special? Tell me about it.", "people"),
        ("pp_lots",              "What is there a LOT of when this group gets together? Dogs, cousins, casseroles, opinions about one particular topic — and roughly how many?", "lots"),
        ("pp_inside_jokes",      "What are some inside jokes amongst the group?", "more"),
        ("pp_memory",            "What is a favorite memory or funny story this group shares?", "more"),
        ("pp_together",          "What is something this group loves to do together?", "more"),
        ("pp_anything",          "Anything else you'd like me to know?", "more"),
    ],
}
TRIVIA_QUESTIONS = TRIVIA_SETS["goh"]                 # default / backward-compat
# Section id -> heading, mirroring app_settings.trivia_question_sets -> sections and
# crm-forms/trivia.html SECTIONS. The summary mail groups the answers the SAME way the client
# saw them, so a gap in a section reads here as the missing ingredient it actually is.
TRIVIA_SECTIONS = [
    ("num", "Important numbers"),
    ("place", "Important places"),
    ("people", "Important people"),
    ("lots", "What there's a lot of"),
    ("more", "Anything else"),
]
# Questions no longer ASKED, kept so an answer a client ALREADY GAVE still gets real wording in
# Simon's summary mail instead of a raw key. _trivia_html surfaces any answered key outside the
# chosen set via TRIVIA_LABELS.get(key, key), so dropping a key from TRIVIA_SETS *alone* silently
# degrades a historical answer. Retired 2026-10-27: co_goals, because the one client ever asked it
# replied "(Not sure what to put here - can we skip this one please?)". A live set always wins, so
# re-adding a key here is harmless.
RETIRED_TRIVIA_LABELS = {
    "co_goals": "What are your company's mid-term goals?",
    "co_charity": "Is there a charity or cause your company supports?",
}
TRIVIA_LABELS = {k: q for qs in TRIVIA_SETS.values() for (k, q, _s) in qs}   # key -> label, all sets
TRIVIA_LABELS.update({k: v for k, v in RETIRED_TRIVIA_LABELS.items() if k not in TRIVIA_LABELS})

def pick_trivia_set(deal):
    """Mirror trivia.html: GOH precedence, then event_type; municipal falls back to the non-profit
    set (Simon plans custom municipal questions after the Closed-Won alert)."""
    if deal.get("guest_of_honor"):
        return TRIVIA_SETS["goh"]
    et = deal.get("event_type")
    if et == "non_profit": return TRIVIA_SETS["nonprofit"]
    if et == "corporate":  return TRIVIA_SETS["company"]
    if et == "school":     return TRIVIA_SETS["school"]
    if et == "municipal":  return TRIVIA_SETS["nonprofit"]
    return TRIVIA_SETS["private"]

PERF = {}   # performer_id -> {first_name, full_name}, loaded in main()

# Deal ids where the CLIENT has said the money is on its way (confirm_deposit_payment on the invoice
# page), the deposit is not marked paid yet, and the method is not ACH. Loaded in main().
# ACH is deliberately excluded: "Request ACH details" is a request FOR details, not a claim of
# payment, so those deals must keep getting chased.
DEP_CLAIMED = set()
# deal ids whose CLIENT has come back since this deal was lost (they have a newer deal). The
# multi-year win-back chain stops for them -- chasing someone who already rebooked reads badly.
REENGAGE_BLOCKED = set()
# The win-back chain, in order. Each step is anchored to the PREVIOUS one actually being SENT,
# not to a fixed offset from stage_changed_at, because these are flag-mode: Simon approves them
# when he gets to them, so 'a year after the last one went out' is the only cadence that holds.
REENGAGE_CHAIN = ("closed_lost_reengage", "closed_lost_reengage_y2",
                  "closed_lost_reengage_y3", "closed_lost_reengage_y4")


def perf_first_name(p):
    """{{PerformerName}} is ALWAYS just a first name -- "Have fun with Marco", never "Marco Fishman".

    Takes the LEADING WORD rather than trusting the column: a performer record can hold a full name
    in first_name. As of 2026-09-12 one does -- first_name "Marco Fishman", full_name "Marco", the
    two swapped -- and reading first_name alone put the full name into a client email. Falls back to
    full_name for a record whose first_name is blank. Mirrored in app.js perfFirstName().
    """
    p = p or {}
    return (((p.get("first_name") or p.get("full_name") or "").strip().split()) or [""])[0]

# (key, anchor, offset_days, mode, stages)
CUE = [
    # show-21 is now only the FLOOR: _cr_send_date brings this forward to TODAY while the show is
    # still ahead, so the ask is queued as soon as customizations are required (Simon 2026-10-24).
    ("customization_request","show",-21,"flag",("closed_won",)),
    # Client-side chases for anything still outstanding (Simon 2026-10-24: "i want them chased.
    # ask again 14 days out, then 10 days out. only ask for items that are required but have not
    # been sent"). Both reuse customization_request's conditional blocks, so each email carries
    # ONLY the items still missing; _flag_suppressed drops them when nothing is outstanding.
    # A gentler nudge at show-60, owed only to a gig booked far enough out that the ask promised
    # one (see _flag_suppressed). ⚠️ Mirrored in app.js's CUE table.
    ("customization_remind_60","show",-60,"flag",("closed_won",)),
    ("customization_chase_1","show",-14,"flag",("closed_won",)),
    ("customization_chase_2","show",-10,"flag",("closed_won",)),
    ("pre_show_2week","show",-14,"auto",("closed_won",)),
    ("pre_show_reminder","show",-3,"auto",("closed_won",)),
    ("stage_intro","show",-4,"flag",("closed_won",)),
    ("deposit_chase_1","stage",2,"flag",("booked","closed_won")),
    ("deposit_chase_2","stage",7,"flag",("booked","closed_won")),
    ("balance_reminder","show",-2,"flag",("booked","closed_won")),
    ("thank_you","show",1,"auto",("closed_won",)),
    ("review_request","after_thank_you",0,"auto",("closed_won",)),   # NOT show-anchored: follows thank_you's outcome (see anchor_date)
    # 90, not 14 (Simon 2026-10-01). The body opens "You popped into my head!", which is only
    # true once time has passed -- at +14 it landed on top of thank_you (+1) and review_request,
    # i.e. a third email in two weeks claiming to be a spontaneous thought. It also now fills the
    # 226-day silence that ran from here to rebook (+240). ⚠️ Mirrored in app.js's CUE table.
    ("popped_into","show",90,"flag",("closed_won",)),
    ("rebook","show",240,"flag",("closed_won",)),
    # The family-worded twin. BOTH templates have always existed and differ on purpose -- rebook
    # says "your group", rebook_family says "your family" -- and app.js's CUE mirror has always
    # gated them on event_type. The ENGINE never had the split, so every family client was sent the
    # corporate wording while the board showed them the family one. Gated in _flag_suppressed.
    ("rebook_family","show",240,"flag",("closed_won",)),
    # flag, not auto (Simon 2026-09-20): these chase a live prospect mid-negotiation, so each one
    # routes through the approval request instead of sending itself. Mirrored in app.js's CUE table.
    ("pf_fu1","proposal_sent",3,"flag",("proposal_sent",)),
    ("pf_abcde","proposal_sent",7,"flag",("proposal_sent",)),
    ("pf_shark","proposal_sent",10,"flag",("proposal_sent",)),
    ("pf_breakup","proposal_sent",14,"flag",("proposal_sent",)),
    ("precall_fu1","stage",1,"auto",("schedule_call",)),
    ("precall_fu2","stage",4,"auto",("schedule_call",)),
    ("precall_abcde","stage",7,"auto",("schedule_call",)),
    ("precall_shark","stage",10,"auto",("schedule_call",)),
    ("precall_breakup","stage",14,"auto",("schedule_call",)),
    ("guest_excited","stage",0,"auto",("closed_won","booked")),
    ("guest_excited_2","show",-5,"auto",("closed_won","booked")),
    ("confirm_details","stage",0,"auto",("closed_won",)),
    ("refer","stage",0,"flag",("refer",)),
    ("closed_lost_daybefore","show",-1,"flag",("closed_lost",)),
    ("closed_lost_after","show",2,"flag",("closed_lost",)),
    ("closed_lost_reengage","stage",319,"flag",("closed_lost",)),   # 10.5mo after MARKED lost: annual win-back, ~6wk before the decision anniversary
    # Years 2-4 of the win-back, each one year after the PREVIOUS step actually went out.
    ("closed_lost_reengage_y2","after_sent:closed_lost_reengage",365,"flag",("closed_lost",)),
    ("closed_lost_reengage_y3","after_sent:closed_lost_reengage_y2",365,"flag",("closed_lost",)),
    ("closed_lost_reengage_y4","after_sent:closed_lost_reengage_y3",365,"flag",("closed_lost",)),
    ("refer_won_daybefore","show",-1,"auto",("refer_won",)),
    ("refer_won_after","show",2,"auto",("refer_won",)),
]
AUTO_MODES = ("auto",)   # 'window' mode retired -> folded into 'auto' (they always behaved identically)

# ---- Client replied -> pause the chase (brief gig-workflow R4, 2026-10-19) -------------------
# The scheduled Claude task on Simon's PC stamps deals.last_client_reply_at when a client email
# arrives. A pre-call chase or proposal follow-up that comes due AFTER such a reply must not go out
# on its own: the client is talking to Simon, and "just following up!" on top of their answer reads
# badly. It turns into FLAG instead (approval request + approval queue, nothing auto-sent).
# "After a reply" = the reply is newer than the LATEST of: the deal's stage entry, and the newest
# email this ladder actually sent. So once Simon sends a follow-up after the reply (approving a
# flagged one), the ladder is back on auto until the client writes again.
# Booking / post-show / everything else is untouched. Mirrored in app.js dealSchedule (replyFlag).
REPLY_PAUSE_LADDERS = (
    ("precall_fu1", "precall_fu2", "precall_abcde", "precall_shark", "precall_breakup"),
    ("pf_fu1", "pf_abcde", "pf_shark", "pf_breakup"),
)
def _ts(v):
    """timestamptz / ISO text / plain date -> aware UTC datetime (a bare date counts as its start)."""
    if not v: return None
    if isinstance(v, datetime.datetime):
        return v if v.tzinfo else v.replace(tzinfo=datetime.timezone.utc)
    if isinstance(v, datetime.date):
        return datetime.datetime(v.year, v.month, v.day, tzinfo=datetime.timezone.utc)
    t = str(v).strip().replace("Z", "+00:00")
    try:
        x = datetime.datetime.fromisoformat(t)
    except ValueError:
        try: x = datetime.datetime.fromisoformat(t[:10])
        except ValueError: return None
    return x if x.tzinfo else x.replace(tzinfo=datetime.timezone.utc)

def _client_replied(key, d, st):
    ladder = next((ks for ks in REPLY_PAUSE_LADDERS if key in ks), None)
    if not ladder: return False
    rep = _ts(d.get("last_client_reply_at"))
    if not rep: return False
    marks = [_ts(d.get("stage_changed_at"))]
    for k in ladder:
        e = st.get(k) or {}
        if isinstance(e, dict) and e.get("sent"):
            marks.append(_ts(e.get("sent_at") or e.get("sent")))
    marks = [m for m in marks if m]
    return (rep > max(marks)) if marks else True

# The customization ask and its two chases. One set, so the suppression rule cannot drift from
# the CUE table. Mirrored by CUST_ASK_KEYS in app.js.
CUST_ASK_KEYS = ("customization_request", "customization_remind_60",
                 "customization_chase_1", "customization_chase_2")
# How far out a show has to be for the ask to carry the "I'll nudge you nearer the date" wording,
# and therefore for that nudge to be owed at all. Mirrored by CUST_FAR_DAYS in app.js.
CUST_FAR_DAYS = 60


def _flag_suppressed(key, d):
    """A FLAG email that should NOT surface at all (mirror of the app's showIf guards), so we never send
    Simon an approval request for it. (customization_request is also guarded in the loop.)"""
    if key in ("deposit_chase_1", "deposit_chase_2"):
        if d.get("deposit_status") in ("paid", "not_required"):
            return True
        # The client already told us a cheque/Venmo/Zelle/PayPal payment is on its way. Chasing them
        # for money they have said they sent reads badly, so hold the chase. deposit_mailed_nag.py
        # then nags SIMON weekly instead, until he marks the deposit paid.
        return d.get("id") in DEP_CLAIMED
    if key in REENGAGE_CHAIN:
        # Every year of the win-back, year 1 included (Simon 2026-09-19): a client who has already
        # come back should never be told "you were looking into having me perform".
        return d.get("id") in REENGAGE_BLOCKED
    if key == "balance_reminder":
        try: bal = float(d.get("balance_amount") or 0)
        except (TypeError, ValueError): bal = 0.0
        return bal <= 0 or d.get("balance_status") in ("paid", "not_required")
    if key in ("rebook", "rebook_family"):
        # Exactly one of the pair goes, decided by event_type. Mirrors the showIf gates that app.js
        # has always had on these two; the engine simply never implemented its half.
        is_family = (d.get("event_type") == "family")
        return is_family if key == "rebook" else (not is_family)
    if key == "stage_intro":
        return not d.get("send_stage_intro")
    if key in CUST_ASK_KEYS:
        trivia_needed = (d.get("require_trivia") is not False) and not d.get("trivia_received_at")
        photos_needed = ((float(d.get("photo_goh_limit") or 0) > 0 or float(d.get("photo_guest_limit") or 0) > 0)
                         and not d.get("photos_received_at"))
        if not (trivia_needed or photos_needed):
            return True
        # A show that has already HAPPENED is never chased, and never re-asked. The ask carries
        # this guard inside _cr_send_date; the chases are plain show-14 / show-10 anchors, so for a
        # past show BOTH dates are in the past and both came due on the very first sweep.
        # Measured 2026-10-24, the day this shipped: 20 chases queued, 18 of them for shows already
        # gone, the oldest three months back. Flag mode held every one in the approval queue so no
        # client was written to - but that is the safety net working, not the guard.
        show = _d(d.get("show_date"))
        if not show or show < TODAY:
            return True
        if key != "customization_request":
            # Inside 10 days BOTH chase dates have passed, so both would queue on the same sweep and
            # Simon would be approving two near-identical notes to one client on one day. Once the
            # last call is due, it is the only one that goes.
            if key == "customization_chase_1" and show - datetime.timedelta(days=10) <= TODAY:
                return True
            # A CHASE only makes sense once the original ask actually went out. If it is still
            # sitting in the approval queue, chasing would hand Simon two near-identical emails to
            # approve; if he cancelled it, he decided not to ask this client at all. Either way the
            # thing to send is the ask, not a follow-up to an email the client never received.
            st = d.get("cue_state") or {}
            if isinstance(st, str):
                try:
                    st = json.loads(st or "{}")
                except ValueError:
                    st = {}
            asked = st.get("customization_request")
            if not (isinstance(asked, dict) and asked.get("sent")):
                return True
            if key == "customization_remind_60":
                # ⚠⚠ ONLY WHEN THE ASK ACTUALLY PROMISED THIS NUDGE, i.e. when it went out more
                # than CUST_FAR_DAYS before the show and so rendered the far-out block. Without
                # this a gig booked INSIDE the window is the broken case, not the edge one: its
                # show-60 anchor is ALREADY IN THE PAST, so the reminder would come due on the
                # very first sweep, land in the queue beside the ask, and chase a client twice on
                # day one for something they were told nothing about.
                _sent = _d(asked.get("sent"))
                if not _sent or (show - _sent).days <= CUST_FAR_DAYS:
                    return True
                # Once the show-14 chase is due there is no room for a gentler nudge as well -
                # the same reasoning that makes chase_1 stand down for chase_2.
                if show - datetime.timedelta(days=14) <= TODAY:
                    return True
        return False
    return False

# ---- Missing-field guard ---------------------------------------------------
# An AUTO email is PAUSED (not sent) when the template uses a {{merge field}} that is blank for
# this deal, so a client never receives an email with a hole in it. The pause is recorded on
# cue_state[key].blocked = {"since": iso, "fields": [...]}. blocked_digest.py nags Simon once a
# day until it is filled (then the email auto-sends, see the re-check pass) or cancelled.
# OPTIONAL fields are legitimately often-blank and never block (kept in sync with check_missing.py).
OPTIONAL_FIELDS = {"Company", "GuestOfHonor", "ProposalLink", "LastShowYear", "EventDetails",
                   "PhotoLink", "PhotoGohLimit", "PhotoGuestLimit"}   # only shown inside their active photo block
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
    """Approval-mode authorize email: each held email shows the Preview & Send / Cancel buttons FIRST,
    then the recipient, subject, and a preview of the full body.
    rows: [(client_name, key, to, subject, body, token, deal_id)]."""
    b=['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    b.append('<h2 style="margin:0 0 4px">Approve to send</h2>')
    b.append('<p style="color:#5f6368;margin:0 0 16px">These emails are held and will NOT go to the client '
             'until you send them. <b>Preview &amp; Send</b> opens it in the CRM to review and send; '
             '<b>Cancel</b> drops it (you can revive it later).</p>')
    for row in rows:
        nm,key,to,subj,body,token,did = row[:7]
        note = row[7] if len(row) > 7 else None      # optional plain-language heads-up shown atop the box
        appr=f"{APPROVE_BASE}?t={token}&a=approve"
        canc=f"{APPROVE_BASE}?t={token}&a=cancel"
        edit=f"{CRM_BASE}/?deal={did}&editemail={key}"
        b.append('<div style="border:1px solid #e6e6e6;border-radius:10px;padding:14px 16px;margin:0 0 16px">')
        if note:
            b.append('<div style="background:#fff8e1;border:1px solid #f0d48a;border-radius:8px;'
                     'padding:8px 10px;margin:0 0 12px;color:#7a5c00;font-size:13px">%s</div>' % html.escape(str(note)))
        # buttons FIRST, above the To field
        b.append('<div style="margin:0 0 12px">')
        b.append(f'<a href="{html.escape(edit)}" style="display:inline-block;background:#1f8f5f;color:#fff;'
                 'text-decoration:none;font-weight:bold;padding:9px 18px;border-radius:8px;margin:0 8px 8px 0">Preview &amp; Send</a>')
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
    """Google Calendar day view for the show date, with the show time in the query so Simon can see it.
    No leading zeros - matches the format Simon uses, e.g. .../customday/2026/8/15?3:30PM-5:30PM"""
    sd=_d(d.get("show_date"))
    if not sd: return ""
    stime=re.sub(r"\s+","",(d.get("show_time") or ""))   # drop spaces so it stays readable in the URL (7pm, 5:30PM)
    base="https://calendar.google.com/calendar/u/0/r/customday/%d/%d/%d" % (sd.year, sd.month, sd.day)
    return base + ("?"+stime if stime else "")

def _double_check_btn(d):
    """Amber 'DOUBLE CHECK DATE' button -> the Google Calendar day view for the show date, so Simon can
    eyeball that day for conflicts before adding the event. Still used by the self gig check-in email
    (removed from the GCal! reminder 2026-10-04 as redundant with STEP 1, which opens the same day)."""
    link=_gcal_day_link(d)
    if not link: return ""
    return ('<a href="%s" style="display:inline-block;background:#e0a92e;color:#3a2a00;text-decoration:none;'
            'font-weight:bold;padding:10px 20px;border-radius:8px;margin:0 8px 10px 0">&#128269; DOUBLE CHECK DATE</a>'
            % html.escape(link))

def _gcal_event_url(d):
    """deals.gcal_url ONLY when it is a real link. Confirming "it's on my calendar" stores a marker
    there instead (see GCAL_CONFIRMED / _gcalEventUrl in app.js): every CONDITION on the column still
    reads it as "handled", but nothing may put it in an href. MIRROR of app.js's _gcalEventUrl."""
    v = str(d.get("gcal_url") or "").strip()
    return v if re.match(r"^https?://", v, re.I) else ""

def gcal_link(d, V):
    """The calendar link for a deal: the SAVED event URL once Simon has stored it, else the add-event link."""
    if _gcal_event_url(d): return _gcal_event_url(d)           # a real saved event URL wins for every gcal link
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

def _maps_link(addr):
    """Event address as a Google Maps search link (or '(not set)' when blank)."""
    a = str(addr or "").strip()
    if not a: return "(not set)"
    url = "https://www.google.com/maps?q=" + urllib.parse.quote_plus(a)
    return '<a href="%s" style="color:#1155cc;text-decoration:underline">%s</a>' % (html.escape(url), html.escape(a))

def _client_info_rows(V):
    """Client contact block as separate (label, html_value) lines - shown the same way in every
    self-notification to Simon. Company deal -> "Client Name: <Company>" + a "Contact:" line for the
    person (Simon 2026-09-17); otherwise a single "Name" line. Then Email (mailto), Phone (tel).
    Missing email/phone -> em dash."""
    name    = V.get("ClientFullName") or "-"
    company = (V.get("Company") or "").strip()
    email = (V.get("ClientEmail") or "").strip()
    phone = (V.get("ClientPhone") or "").strip()
    email_html = ('<a href="mailto:%s" style="color:#1155cc;text-decoration:underline">%s</a>'
                  % (html.escape(email), html.escape(email))) if email else "&mdash;"
    phone_html = "&mdash;"
    if phone:
        num = re.sub(r"[^\d+]", "", re.split(r"(?i)\s*(?:x|ext\.?)\s*", phone, maxsplit=1)[0])
        phone_html = (('<a href="tel:%s" style="color:#1155cc;text-decoration:underline">%s</a>'
                       % (html.escape(num), html.escape(phone))) if num else html.escape(phone))
    name_rows = ([("Client Name", html.escape(company)), ("Contact", html.escape(str(name)))]
                 if company else [("Name", html.escape(str(name)))])
    # Pronunciation sits on the line AFTER the name, and only when it is filled in -- a blank one
    # prints no label at all rather than an empty row (Simon 2026-09-19). This block is owner-only
    # by construction: _client_info_rows is never called from a client-facing builder.
    pron = (V.get("pron.line") or "").strip()
    pron_rows = [("Pronunciation", html.escape(pron))] if pron else []
    return name_rows + pron_rows + [("Email", email_html), ("Phone", phone_html)]

def _gcal_email_html(d, V, token, reminder):
    """GCal! email to Simon: a calendar link for the show + an 'Update GCal link' button (paste page).
    reminder=True adds the 'add to calendar and update gcal link' banner at the top."""
    who = V.get("ClientFullName") or d.get("deal_name") or "this booking"
    when = V.get("ShowDate") or (str(d.get("show_date")) if d.get("show_date") else "TBD")
    stime = (d.get("show_time") or "").strip()
    link = _gcal_event_url(d) or _gcal_day_link(d)  # real saved event URL wins, else STEP 1's day view
    paste = "%s?t=%s" % (GCAL_PASTE_BASE, token)
    b=['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    if reminder:
        b.append('<div style="background:#fff4d6;border:1px solid #e0a92e;border-radius:8px;padding:10px 12px;'
                 'margin:0 0 14px;font-weight:bold;color:#7a5a00">Reminder - add to calendar and update gcal link</div>')
    b.append('<h2 style="margin:0 0 4px">Add to calendar: %s</h2>' % html.escape(str(who)))
    b.append('<p style="color:#5f6368;margin:0 0 14px">%s%s</p>' % (html.escape(str(when)), (" at "+html.escape(stime)) if stime else ""))
    # EMAIL-ONLY easy path (mirrors app.js buildGcalEmail): an inbox can't run JS, so offer a button into
    # the CRM preview page (?gcal=1) where STEP 1 copies the info to the clipboard. Only when no saved link.
    if not d.get("gcal_url"):
        b.append('<div style="margin:0 0 10px"><a href="https://crm.thesimonshow.com/?deal=%s&amp;gcal=1" '
                 'style="display:inline-block;background:#6a4ea3;color:#fff;text-decoration:none;font-weight:bold;'
                 'padding:10px 20px;border-radius:8px">&#128203; Open in CRM to copy the info &amp; add &#8594;</a></div>'
                 % html.escape(str(d.get("id") or "")))
    if link:
        open_lbl = "Open your event" if d.get("gcal_url") else "STEP 1: Copy info below to GCal"
        b.append('<a href="%s" style="display:inline-block;background:#1155cc;color:#fff;text-decoration:none;'
                 'font-weight:bold;padding:10px 20px;border-radius:8px;margin:0 8px 10px 0">%s</a>' % (html.escape(link), open_lbl))
    b.append('<a href="%s" style="display:inline-block;background:#1f8f5f;color:#fff;text-decoration:none;'
             'font-weight:bold;padding:10px 20px;border-radius:8px;margin:0 0 10px 0">STEP 2: SAVE LINK to apt</a>' % html.escape(paste))
    # horizontal rule, then the paste-ready info box (the CRM record link lives INSIDE the box, top line)
    b.append('<hr style="border:0;border-top:1px solid #dcdcdc;margin:18px 0 14px">')
    # all the deal info, so Simon can paste it into the calendar appointment.
    # Each line is its own <div> (block) - NOT a <pre> - so the breaks survive Google Calendar's SAVE
    # round-trip when Simon pastes this into an event description (a <pre> collapses to one line on save).
    _lst = "font-family:Verdana,Arial,sans-serif;font-size:13px;line-height:1.5;color:#202124;margin:0"
    def _line(t): return '<div style="%s">%s</div>' % (_lst, t)
    def _a(href, txt): return '<a href="%s" style="color:#1155cc;text-decoration:underline">%s</a>' % (html.escape(href), html.escape(txt))
    def _tel_href(s):
        parts = re.split(r"(?i)\s*(?:x|ext\.?)\s*", str(s or ""), maxsplit=1)   # split off any extension
        num = re.sub(r"[^\d+]", "", parts[0] if parts else "")
        ext = re.sub(r"[^\d]", "", parts[1]) if len(parts) > 1 else ""
        return ("tel:%s%s" % (num, ("," + ext) if ext else "")) if num else ""
    _dep_amt = V.get("DepositAmount") or "0"; _ds = (d.get("deposit_status") or "").lower()
    if _ds == "paid":            _dep = 'deposit $%s <b style="color:#1e874b">&#10003; PAID</b>' % html.escape(_dep_amt)
    elif _ds == "not_required":  _dep = 'deposit <b>not required</b>'
    elif _ds == "pending":       _dep = 'deposit $%s <b style="color:#b8860b">(pending)</b>' % html.escape(_dep_amt)
    else:                        _dep = 'deposit $%s <b style="color:#c0392b">(NOT paid yet)</b>' % html.escape(_dep_amt)
    money_html = 'Fee $%s  -  %s  -  balance $%s' % (html.escape(V.get("AppearanceFee") or "?"), _dep, html.escape(V.get("BalanceAmount") or "?"))
    rows = _client_info_rows(V) + [                              # client block (Name/Email/Phone) + Money are HTML; the rest are plain-escaped
            ("When", html.escape((V.get("ShowDate") or "-") + ((" at " + stime) if stime else ""))),
            ("Venue", _maps_link(V.get("Venue"))),   # event address -> Google Maps link
            ("Occasion", html.escape(V.get("Occasion") or "-")),
            ("Event details", html.escape(V.get("EventDetails") or "-")),
            ("Format", html.escape(V.get("FormatDetails") or "-")),
            ("Money", money_html)]
    info_divs = "".join(_line(html.escape(k) + ": " + val) for k, val in rows)
    crm_link = ('<a href="%s/?deal=%s" style="color:#1155cc;font-weight:bold;text-decoration:underline">'
                '&#128187; Open in CRM &#8594;</a>'
                % (html.escape(CRM_BASE), html.escape(str(d.get("id")))))
    # right under the deal link: a car emoji + the venue address, the whole line clickable -> Google Maps.
    venue_raw = (V.get("Venue") or "").strip()
    car_div = ""
    if venue_raw:
        maps_url = "https://www.google.com/maps?q=" + urllib.parse.quote_plus(venue_raw)
        car_div = _line('<a href="%s" style="color:#1155cc;text-decoration:underline">&#128663; %s</a>'
                        % (html.escape(maps_url), html.escape(venue_raw)))
    # crm_link + car_div anchors are intentionally unescaped (clickable links); the info text is escaped.
    car_block = (_line("&nbsp;") + car_div) if car_div else ""   # blank line between the CRM link and the venue link
    b.append('<div style="background:#f6f7f9;border:1px solid #dcdcdc;border-radius:8px;'
             'padding:12px 14px;margin:6px 0 0;word-break:break-word">%s%s%s%s</div>'
             % (_line(crm_link), car_block, _line("&nbsp;"), info_divs))
    b.append('</div>')
    return "".join(b)

def selfcheckin_html(d, V, label):
    rows=[("When", html.escape((V["ShowDate"] or "-") + (f' at {V["ShowTime"]}' if V["ShowTime"] else ""))),
          ("Venue", _maps_link(V["Venue"])),   # event address -> Google Maps link
          ("Occasion", html.escape(V["Occasion"] or "-")),
          ("Event details", html.escape(V["EventDetails"] or "-")),
          ("Format", html.escape(V["FormatDetails"] or "-")),
          ("Money", html.escape(f'Fee ${V["AppearanceFee"] or "?"}  -  deposit ${V["DepositAmount"] or "0"}  -  balance ${V["BalanceAmount"] or "?"} ({d.get("balance_status") or "?"})'))]
    b=['<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">']
    _dcb=_double_check_btn(d)                                     # DOUBLE CHECK DATE button - goes first
    _cal=gcal_link(d,V)
    if _dcb or _cal:
        _cal_btn=""
        if _cal:
            # deal already has a saved calendar event -> one button straight to that appointment; else "add to calendar"
            _cal_lbl = "&#128197; Open gig in calendar" if d.get("gcal_url") else "&#128197; Add to calendar"
            _cal_btn=(f'<a href="{html.escape(_cal)}" '
                      f'style="display:inline-block;background:#1155cc;color:#fff;text-decoration:none;'
                      f'font-weight:bold;padding:10px 20px;border-radius:8px;margin:0 8px 10px 0">{_cal_lbl}</a>')
        b.append(f'<p style="margin:0 0 14px">{_dcb}{_cal_btn}</p>')
    b.append(f'<h2 style="margin:0 0 10px">Gig {html.escape(label)}: {html.escape(V["ClientFullName"] or "gig")}</h2>')
    b.append('<table style="border-collapse:collapse">')
    for k,val in _client_info_rows(V):   # client block: Name / Email (mailto) / Phone (tel) - values are ready HTML
        b.append(f'<tr><td style="padding:3px 14px 3px 0;color:#5f6368;vertical-align:top"><b>{html.escape(k)}</b></td>'
                 f'<td style="padding:3px 0">{val}</td></tr>')
    for k,val in rows:                   # values are pre-escaped HTML (Venue is a maps link)
        b.append(f'<tr><td style="padding:3px 14px 3px 0;color:#5f6368;vertical-align:top"><b>{html.escape(k)}</b></td>'
                 f'<td style="padding:3px 0">{val}</td></tr>')
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
    qs = pick_trivia_set(d)                 # same set the client saw (GOH precedence, then event type)
    shown = set()

    def _row(q, a):
        a = ("" if a is None else str(a)).strip()
        val = html.escape(a) if a else '<em style="color:#9aa0a6">(not answered)</em>'
        b.append(f'<tr><td style="padding:9px 0;border-top:1px solid #eee">'
                 f'<div style="color:#5f6368;font-size:12px;margin-bottom:3px"><b>{html.escape(q)}</b></div>'
                 f'<div>{val}</div></td></tr>')

    # Grouped exactly as the form presented it. An entry with no section (a per-deal override saved
    # before sections existed) falls into the untitled group at the end rather than vanishing.
    seen_sec = set()
    for sec_id, sec_title in TRIVIA_SECTIONS:
        grp = [t for t in qs if (t[2] if len(t) > 2 else "") == sec_id]
        if not grp:
            continue
        seen_sec.add(sec_id)
        b.append(f'<tr><td style="padding:16px 0 2px"><div style="font-size:11px;letter-spacing:1.2px;'
                 f'text-transform:uppercase;color:#9aa0a6"><b>{html.escape(sec_title)}</b></div></td></tr>')
        for t in grp:
            shown.add(t[0])
            _row(t[1], answers.get(t[0]))
    for t in qs:
        if t[0] in shown:
            continue
        shown.add(t[0])
        _row(t[1], answers.get(t[0]))
    # safety: surface any answered key that isn't in the chosen set (e.g. the set changed after submit)
    for key,a in (answers or {}).items():
        if key in shown: continue
        a=("" if a is None else str(a)).strip()
        if not a: continue
        q=TRIVIA_LABELS.get(key,key)
        b.append(f'<tr><td style="padding:9px 0;border-top:1px solid #eee">'
                 f'<div style="color:#5f6368;font-size:12px;margin-bottom:3px"><b>{html.escape(q)}</b></div>'
                 f'<div>{html.escape(a)}</div></td></tr>')
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
    _ey=_d(deal.get("created_at"))
    first = (contact.get("first_name") or (contact.get("full_name") or "").split(" ")[0] or "")
    first = re.sub(r"(^|\s)(\S)", lambda m: m.group(1)+m.group(2).upper(), first)   # always capitalize first names
    V={
        "ClientFirstName":first, "ClientFullName":_cap_full(contact),
        "ClientEmail":contact.get("email") or "", "ClientPhone":contact.get("phone_mobile") or contact.get("phone_other") or "",
        # The year they were ENQUIRING, not the year of the would-be event: 13% of closed-lost
        # deals span a new year between the two (enquire in Nov for a January event), and
        # "Back in <event year> you were looking into..." would be wrong for every one of them.
        "EnquiryYear": (str(_ey.year) if _ey else ""),
        # Sentence-initial phrase form. Year 1 can fire in the SAME calendar year as the enquiry
        # (enquire and lose in January, win-back lands in November), where "Back in 2026" during
        # 2026 reads wrong. Years 3-4 use EnquiryYear mid-sentence instead, where the enquiry is
        # always several years back and the bare number reads better.
        "EnquiryWhen": ("" if not _ey else
                        # "A while back", not "Earlier this year" (Simon 2026-10-05): the sentence
                        # reads "<phrase> you were looking into having me perform at your event."
                        # Mirrored in app.js mergeValues -- change BOTH.
                        ("A while back" if _ey.year == TODAY.year else "Back in %d" % _ey.year)),
        # OWNER-ONLY. The dot in the key is the safety mechanism: template tokens are matched by
        # {{\w+}} and a dot is not a word character, so no client template can render this even by
        # accident. Do not rename it to a {{...}}-shaped key. MIRRORED in app.js mergeValues().
        "pron.line": (contact.get("name_pronunciation") or "").strip(),
        "ShowDate": (f"{WD[sd.weekday()]}, {MO[sd.month-1]} {_ord(sd.day)}, {sd.year}" if sd else ""),
        "ShowDateNoYear": (f"{WD[sd.weekday()]}, {MO[sd.month-1]} {_ord(sd.day)}" if sd else ""),
        "ShowDateShort": (f"{MO[sd.month-1][:3]} {sd.day}, {sd.year}" if sd else ""),
        "ShowDay": (WD[sd.weekday()] if sd else ""), "ShowTime": deal.get("show_time") or "",
        "Venue": deal.get("venue_address") or "", "Occasion": _strip_excl(deal.get("occasion")),
        "EventDetails": deal.get("audience_details") or "", "FormatDetails": deal.get("show_format") or "",
        "AppearanceFee": _num(deal.get("amount")), "DepositAmount": _num(deal.get("deposit_amount")),
        "BalanceAmount": _num(deal.get("balance_amount")),
        "Month": (MO[sd.month-1] if sd else MO[TODAY.month-1]), "Year": str(sd.year if sd else TODAY.year),
        "Company": deal.get("company") or "", "GuestOfHonor": deal.get("guest_of_honor") or "",
        "GuestOfHonorMagicLine": (f"{deal.get('guest_of_honor')} has gotten obsessed with magic and is doing the show." if deal.get("guest_of_honor") else "One of our guests has gotten obsessed with magic, and is going to do the show himself."),
        "ProposalLink": deal.get("proposal_link") or "",
        "CustomizeLink": (f"{CUSTOMIZE_BASE}?t={deal.get('customize_token')}" if deal.get("customize_token") else ""),
        "PerformerName": perf_first_name(PERF.get(deal.get("performer_id"))),
        "EventType": (deal.get("event_type","").replace("_"," ").title()+" event") if deal.get("event_type") else "event",
        # ⚠️ deal_name must NEVER reach a client (Simon 2026-09-20). It is an internal label and in
        # practice holds the client's own phone number and email address, a heart emoji and "(copy)",
        # because a copied deal inherits the whole string. These two tokens were its only client-facing
        # use -- "Re: {{ProposalSubject}}" on the four pf_ follow-ups -- so they now start at occasion.
        # Mirrored in app.js mergeValues; do not "restore" the deal_name fallback.
        "ProposalSubject": deal.get("occasion") or "your event",
        "ThreadSubject": deal.get("occasion") or "your event",
        "LastShowYear": "",
    }
    # conditional-section flag: trivia is asked only when required and not already received (see _conditional_blocks)
    V["_cond_trivia"] = (deal.get("require_trivia") is not False) and not deal.get("trivia_received_at")
    # Simon 2026-10-25: "if it is due more than 60 days out, give them a version where they can
    # provide the info now, but let them know that if they don't have it, they will get a reminder
    # 60 days out."
    # ⚠ A CONDITIONAL BLOCK, not a second template and not a second cue key. A gig booked inside
    # the window simply does not render it, so the ask can never promise a reminder that is already
    # due - the requirement falls out of the design instead of needing its own guard. Mirrored by
    # `_cond_far60` in app.js's mergeValues.
    _show_far = _d(deal.get("show_date"))
    V["_cond_far60"] = bool(_show_far and (_show_far - TODAY).days > CUST_FAR_DAYS)
    # photo-request scenario: the customization email picks its body by which photos are requested.
    def _numlim(x):
        try: return float(x)
        except (TypeError, ValueError): return 0.0
    _goh = _numlim(deal.get("photo_goh_limit")); _guest = _numlim(deal.get("photo_guest_limit"))
    V["PhotoLink"] = (f"{PHOTO_BASE}?t={deal.get('customize_token')}" if deal.get("customize_token") else "")
    V["PhotoGohLimit"] = (str(int(_goh)) if _goh > 0 else "")
    V["PhotoGuestLimit"] = (str(int(_guest)) if _guest > 0 else "")
    # Simon 2026-10-24: "only ask for items that are required but have not been sent."
    # These three used to key on the LIMITS alone, so a deal whose photos had already arrived but
    # whose trivia was still outstanding got an email asking for the photos all over again.
    # `_cond_trivia` above has always carried the matching `not trivia_received_at`; this is the
    # photo half of the same rule, and it is what lets the chase templates reuse these blocks.
    _pics_out = not deal.get("photos_received_at")
    V["_cond_photos_goh_only"]     = _pics_out and (_goh > 0 and _guest <= 0)
    V["_cond_photos_both"]         = _pics_out and (_goh > 0 and _guest > 0)
    V["_cond_photos_guests_only"]  = _pics_out and (_goh <= 0 and _guest > 0)
    V["_cond_deposit_unpaid"]      = bool(deal.get("deposit_amount")) and deal.get("deposit_status") != "paid"
    _dep_amt = float(deal.get("deposit_amount") or 0)
    _app_fee = float(deal.get("amount") or 0)
    _dep_status = deal.get("deposit_status") or ""
    # paid_in_full: deposit covered the whole fee and is paid -> deposit/balance lines are replaced
    # by a single "Paid in full, thank you!" line ({{#paid_in_full}} in the templates).
    _paid_full = _dep_status == "paid" and _app_fee > 0 and _dep_amt >= _app_fee
    V["_cond_paid_in_full"]        = _paid_full
    V["_cond_deposit_paid"]        = _dep_status == "paid" and not _paid_full
    V["_cond_balance_due_now"]     = _dep_amt > 0 and _app_fee > 0 and _dep_amt >= _app_fee and _dep_status not in ("paid", "not_required")
    V["_cond_balance_due_arrival"] = not V["_cond_balance_due_now"] and not _paid_full
    return V

# The OCCASION loses its exclamation points when it is merged into email text (Simon 2026-10-24:
# "keep them in the original variable, but get rid of the exclamation points when generating text
# for the email"). He writes occasions like "Skypath Holiday Party!", and rebook / rebook_family
# put the field MID-SENTENCE - "It's been a while since {{Occasion}}, and you popped into my
# head." - which rendered "...since Skypath Holiday Party!, and you popped...".
#
# ⚠⚠ THE COLUMN IS NOT TOUCHED. deals.occasion keeps its exclamation points, and the drawer and
#    the merge-field editor still show and save them - mailFieldOpen prefills from deal[col], not
#    from the rendered span, so editing an Occasion through a document cannot strip them either.
# ⚠ Applied at the MERGE SITE, not per template: the two rebook templates are simply where it
#   reads worst, and any future template that puts the occasion mid-sentence inherits the fix.
# ⚠ ALL of them, not just a trailing one - "Wow! Big Party" reads no better mid-sentence - then
#   whitespace is collapsed so "Party ! Two" cannot leave a double space behind.
# ⚠ _fix_end_punct still exists and is still needed: it guards every OTHER field that might end
#   in "!", and a value Simon has typed straight into a held email body.
# MIRRORED in web/app.js (stripExcl) and vercel-send/api/run-due.js (stripExcl) - all three build
# merge values, so all three need it or the same email differs depending on who sent it.
def _strip_excl(s):
    return re.sub(r"\s+", " ", (s or "").replace("!", "")).strip()

# Merge values can already END in an exclamation mark - Simon writes occasions like
# "Intersession of camp!" - while templates put a period straight after the field
# ("...at {{Occasion}}."). That renders "camp!.". Collapse it.
# Deliberately "!" ONLY, not "?": an occasion is never phrased as a question, so a "?." would be
# real prose (a genuine question followed by a new sentence) and must be left alone.
# Applied to the whole rendered string rather than only at the merge site - "!." is never
# legitimate. Exactly the same shape as the $$ collapse below.
# MIRRORED in web/app.js (fillSubject + _cleanMailHtml) and vercel-send/api/run-due.js - all three
# render templates, so all three need this or the same email differs depending on who sent it.
def _fix_end_punct(s):
    return (s or "").replace("!.", "!")

def fill_subject(s, V):
    return _fix_end_punct(
        re.sub(r"\{\{(\w+)\}\}", lambda m: V[m.group(1)] if V.get(m.group(1)) else m.group(0), s or ""))

# Once the Magic Castle invite has been sent for a deal, drop the "I'll email you the invite in ~10 min"
# heads-up line from any other ladder email (it would be stale / contradictory).
_MC_HEADSUP_RE = re.compile(r"(?im)^.*(?:email you an invite to the magic castle|arrive (?:in the next ten minutes|within an hour)).*(?:\r?\n)?")
def _strip_mc_headsup(raw):
    return re.sub(r"\n{3,}", "\n\n", _MC_HEADSUP_RE.sub("", raw or ""))

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

# Conditional sections: {{#trivia}}...{{/trivia}} keeps its inner content only when V["_cond_trivia"] is
# truthy, else the whole block is dropped. Used so the customization_request email skips the trivia ask when
# a deal has require_trivia OFF. The markers use '#'/'/', which the {{\w+}} merge/field regexes ignore, so
# they never register as merge fields or as "missing required" fields. Mirrored in app.js _conditionalBlocks.
_COND_RE = re.compile(r"\{\{#(\w+)\}\}(.*?)\{\{/\1\}\}", re.S)
def _conditional_blocks(raw, V):
    out = _COND_RE.sub(lambda m: (m.group(2) if V.get("_cond_" + m.group(1)) else ""), raw or "")
    return re.sub(r"\n{3,}", "\n\n", out)   # collapse the blank-line gap a dropped block can leave

def render_html(raw, V, signature):
    raw = _conditional_blocks(raw, V)          # {{#trivia}}...{{/trivia}} -> keep only if trivia is required
    raw = _drop_empty_optional_lines(raw, V)   # blank EventDetails etc. -> drop the whole line
    # Template bodies are TRUSTED HTML authored by Simon — they contain styled tags (<p style=...>,
    # <u>, <a>, <i>, etc.). Do NOT escape them, or the tags render as visible text. Only merge VALUES
    # (client data, substituted below) are escaped. Bare '&' that isn't an entity -> &amp; so "A & B" is valid.
    s = re.sub(r"&(?![#\w]+;)", "&amp;", raw or "")
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"__(.+?)__", r"<u>\1</u>", s)
    # markdown links: target may be a literal https URL OR a {{Field}} that resolves to a URL
    def _link(m):
        text, field = m.group(1), m.group(3)
        url = (V.get(field) or "") if field else m.group(2)
        return f'<a href="{html.escape(url)}" style="color:#1155cc;text-decoration:underline">{text}</a>' if url else text
    s = re.sub(r"\[([^\]]+)\]\((\{\{(\w+)\}\}|https?://[^)]+)\)", _link, s)
    s = re.sub(r"\{\{(\w+)\}\}", lambda m: html.escape(V[m.group(1)]) if V.get(m.group(1)) else m.group(0), s)
    s = re.sub(r"\$\$+", "$", s)   # never render $$ (template's literal $ + a $ in a merge value)
    s = _fix_end_punct(s)          # never render "camp!." (merge value ending in ! + template's period)
    s = _lists_and_breaks(s)
    if signature:
        s += "<br><br>" + signature   # signature is trusted raw HTML (image + links), do NOT escape
    return s

def _archive_sent(cur, deal_id, key, to, subj, body, sent_at):
    """Copy a just-sent email into sent_emails, then the cue entry drops its body/pdf_b64.
    Keeping full HTML bodies in cue_state forever made every deals fetch pay for every email
    ever sent. Archive failure is non-fatal (the send already happened)."""
    try:
        cur.execute("insert into sent_emails(deal_id,cue_key,to_email,subject,body,sent_at) values (%s,%s,%s,%s,%s,%s)",
                    (deal_id, key, to, subj, body, sent_at))
    except Exception as _ae:
        print(f"    (sent_emails archive failed for {key}: {_ae})")

def _strip_heavy(ne):
    """Remove the bulky fields from a cue entry that just went out (body lives in sent_emails now)."""
    for _k in ("body", "pdf_b64"):
        ne.pop(_k, None)
    return ne

def _save_cue(cur, deal_id, st, *keys):
    """Write ONLY the named cue_state keys, instead of re-serializing the whole document.

    WRITE AMPLIFICATION (2026-07-18): every `update deals set cue_state=<entire doc>` rewrote the
    full ~3 KB JSONB blob even when one small entry changed. That cost three ways:
      1. WAL - Postgres logs the whole new row version on every update.
      2. Realtime - web/app.js subscribes to postgres_changes on deals, so each rewrite fans the
         entire row out to every open CRM tab. The realtime WAL processor was by far the highest-
         call-count query in the database.
      3. TOAST bloat - each rewrite orphans the previous out-of-line value; deals' TOAST had grown
         to roughly 3x its live size (1,480 kB on disk vs ~452 kB of real data).
    jsonb_set touches a single key, so the WAL record and the realtime payload shrink to the size
    of the entry that actually changed. A key that is absent from `st` is deleted server-side.
    """
    for k in keys:
        v = st.get(k)
        if v is None:
            cur.execute("update deals set cue_state = coalesce(cue_state, '{}'::jsonb) - %s where id=%s",
                        (k, deal_id))
        else:
            cur.execute("update deals set cue_state = "
                        "jsonb_set(coalesce(cue_state, '{}'::jsonb), %s::text[], %s::jsonb, true) "
                        "where id=%s",
                        ([k], json.dumps(v), deal_id))

def _claim_dispatch(cur, deal_id, key):
    """Bulletproof idempotency shared with the instant send-now endpoint. Returns True if THIS run is the
    first to claim (deal_id, key) - send it - or False if it was already dispatched (skip). Immune to
    cue_state being rewritten. Fails OPEN (returns True) only if the log itself is unavailable, since
    cue_state's own 'sent' marker is still a backstop for the daily one-shot emails."""
    try:
        cur.execute("insert into email_dispatch(deal_id,cue_key) values (%s,%s) on conflict do nothing", (deal_id, key))
        return cur.rowcount == 1
    except Exception:
        return True

def _release_dispatch(cur, deal_id, key):
    """Give back a claim taken by _claim_dispatch when the send did NOT happen, so a later run can
    retry. Mirrors what run-due.js and send-now.js both do on a failed sendMail - without it a
    held-back email is claimed for ever and silently never goes out."""
    try:
        cur.execute("delete from email_dispatch where deal_id=%s and cue_key=%s", (deal_id, key))
    except Exception:
        pass

# "Fun action requested" (guest_excited) goes into the approval queue this many days before the
# show. Simon 2026-10-13: "fun action requested email should enter approval cue 10 days before the
# event date. if the ladder is triggered 10 days or less from the event date, it should enter the
# approval cue immediately."
GE_LEAD_DAYS = 10

def _cue_send_date(d, key, base, off):
    """When a cue step is due. ONE implementation, called by BOTH branches of the sweep.

    ⚠⚠ THIS EXISTS BECAUSE THE TWO BRANCHES DISAGREED, AND IT COST A FEATURE FOR A DAY AND A HALF.
    The sweep handles a key in one of two places: AUTO/approval keys fall through to the main block,
    and FLAG keys are dealt with earlier, in the `eff == "flag"` branch, which `continue`s before
    the main block is ever reached. Both computed `base + off` themselves, and the dynamic-date
    special cases were only ever added to the AUTO one.

    `customization_request` is FLAG mode. So `_cr_send_date` - written on 2026-10-24 to bring the
    ask forward to TODAY while the show is still ahead - sat in a branch that key never takes, and
    was dead code. *Measured 2026-10-25: every ask ever queued still sat at show-21, and ZERO
    queued early across ~45 sweeps, while the function tested green in isolation.* Testing the
    helper instead of the real entry point is what hid it, which this file already warns about.

    Returns None when a key has a dynamic date and is outside its window; every caller must skip.
    """
    sd = base + datetime.timedelta(days=off)
    if key in ("guest_excited", "guest_excited_2"):
        return _ge_send_date(d, key)          # the forward-to-guests split; may be None
    if key == "customization_request":
        return _cr_send_date(d, sd)
    return sd


def _cr_send_date(d, anchored):
    """When the customization ASK goes into the approval queue. Mirror of the app's _crDate.

    Simon 2026-10-24: "if I require customizations for a job, I want the request for customizations
    to enter the approval cue for approval immediately." It used to wait until show - 21, so a job
    booked months out sat on the ask until three weeks before the date.

    Returns TODAY while the show is still ahead, so the moment a deal is closed_won with trivia or
    pictures outstanding the ask is queued on the next sweep. Otherwise the original show-21 date is
    returned unchanged.

    WARNING: the show-is-still-ahead guard is load-bearing, not tidiness. Without it a closed_won
    show that has already HAPPENED, and whose trivia never arrived, would be handed a TODAY date and
    queue "please send me your trivia" AFTER the event. show - 21 is in the past for those, which is
    the behaviour they already had.

    It is recomputed every sweep rather than stored, like _ge_send_date, so nothing has to be
    migrated and moving the show date cannot strand it."""
    show = _d(d.get("show_date"))
    if show and show >= TODAY and anchored > TODAY:
        return TODAY
    return anchored


def _ge_send_date(d, key):
    """Dynamic send date for the guest_excited split (mirror of the app's _geDates). Returns a date or None.

    guest_excited   -> show - GE_LEAD_DAYS, or TODAY when the ladder starts inside that window.
    guest_excited_2 -> show - 5, only while the show is more than 21 days away.

    The date is RECOMPUTED from show_date on every sweep rather than stored, which is what makes
    "move the show, the email moves with it" work for an entry that has not been queued yet. An
    entry ALREADY sitting in the approval queue is handled by the re-date pass in main().

    WARNING: guest_excited_2 is UNREACHABLE and always has been. It is only given a date while the
    show is >21 days away, but the sweep only acts on a cue once its date has arrived (show-5, i.e.
    5 days away) - the two conditions cannot both hold. Measured 2026-10-13: 0 sent, 0 pending
    across 27 deals carrying the key. Left exactly as it was: making it fire would start sending a
    second client email nobody has asked for. Decide that deliberately, not as a side effect."""
    show = _d(d.get("show_date"))
    if not show:
        return TODAY if key == "guest_excited" else None
    if key == "guest_excited":
        first = show - datetime.timedelta(days=GE_LEAD_DAYS)
        dt = first if first > TODAY else TODAY            # inside the window -> queue immediately
    elif (show - TODAY).days > 21:
        dt = show - datetime.timedelta(days=5)
    else:
        return None                                       # near show -> no second send
    if dt > show:
        return None                                       # hard rule: never cue a send date AFTER the show date
    return dt

def anchor_date(deal, anchor, stages):
    if stages and deal.get("stage") not in stages: return None
    if anchor=="show":    return _d(deal.get("show_date"))
    if anchor=="stage":   return _d(deal.get("stage_changed_at"))
    if anchor=="proposal_sent": return _d(deal.get("proposal_sent_at"))   # follow-ups start when the proposal was sent
    if anchor=="created": return _d(deal.get("created_at"))
    if anchor.startswith("after_sent:"):
        # One year after the named cue was SENT. Returns None while it is unsent, so the chain
        # simply waits -- and a cancelled step stops every later one, which is intended: if Simon
        # killed last year's note he does not want this year's either.
        prev = (deal.get("cue_state") or {}).get(anchor.split(":", 1)[1]) or {}
        return _d(prev.get("sent"))
    if anchor=="after_thank_you":
        # review_request follows the ACTUAL outcome of thank_you, not the show date:
        #   thank_you SENT      -> due the DAY AFTER it was sent
        #   thank_you CANCELLED -> due now (returns TODAY; this CUE entry's offset is 0)
        #   neither yet         -> not due (wait for Simon to send or cancel thank_you)
        ty = (deal.get("cue_state") or {}).get("thank_you") or {}
        if ty.get("cancelled"): return TODAY
        s = ty.get("sent")
        if s:
            sd = _d(s)
            return sd + datetime.timedelta(days=1) if sd else None
        return None
    return None


# ─── PIE CONTROL STEP 3: hold the authorize email back while the phone bubble is unseen ──────────
# briefs/pie-control-queue.md step 3. Simon's choice: approvals get ONE HOUR, because an approval
# costs a send window (the 9am-9pm client cadence means a late one can slip a whole day).
#
# ⚠⚠ CHANGED 2026-10-25 (Simon: "stop sending email approval requests to email, only to pie
#    control"). While the switch is ON the authorize email is **NEVER** sent - not after an hour,
#    not when the feed is unreadable, not when the stamp is missing. The phone owns approvals
#    outright, and PIE_APPROVAL_HOLD_MIN below no longer gates anything.
# ⚠⚠ SO THE OLD RULE HERE - "every uncertainty resolves to send it anyway" - IS NO LONGER TRUE,
#    and that is the whole risk of this step: with no email fallback, a phone that stops polling
#    means an approval is announced NOWHERE. Three things make that tolerable and they are the
#    things to check if an approval is ever missed:
#      1. the switch itself: private.config 'pie_suppress_approvals' -> anything but on/true/1/yes
#         restores the old behaviour on the very next sweep, with no deploy;
#      2. the queue is still visible in three places that do not involve the phone - the CRM's own
#         approval queue, the shield badge, and the AHK watcher's ghosts;
#      3. pie_shadow.py alarms (error_log -> phone push + AHK dialog, never email) when an approval
#         has sat UNSEEN for PIE_STALE_ALERT_H. That alarm is the replacement for this fallback;
#         if it is ever removed, put the fallback back first.
# ⚠⚠ IT SHIPS SWITCHED OFF. private.config 'pie_suppress_approvals' must be set to 'on' by hand,
#    and it should NOT be until Pie Control actually acks - with nothing acking, turning it on only
#    delays every approval email by an hour and suppresses nothing. The row is also the kill switch:
#    setting it to anything else takes effect on the next sweep with no deploy.
PIE_APPROVAL_HOLD_MIN = 60

# ⚠⚠ THESE TWO KEYS DESCRIBE ONE QUEUEING EPISODE AND MUST BE STRIPPED WHEREVER THE APPROVAL KEYS
# ARE. An entry can be unqueued and re-queued under the same key (guest_excited re-dates on a moved
# show; date_override pushes one out), and today that re-queueing earns a FRESH authorize email. If
# approval_email_sent survived the unqueue, the catch-up pass below would see the new episode as
# already announced and the email would never go out at all.
_PIE_EPISODE_KEYS = ("approval_queued_at", "approval_email_sent")


def _pie_suppress_on(cur):
    """Is suppression switched on? Delegates to pie.suppress_on - ONE reader of that config row,
    shared with the four owner-nags (step 4), so the switch cannot come to mean two things.
    Defaults OFF and fails OFF."""
    import pie
    return pie.suppress_on(cur)


def _pie_seen_map(cur):
    """{item_id: seen_at} straight from public.pie_approvals().

    ⚠⚠ IT CALLS THE FEED'S OWN FUNCTION RATHER THAN RE-DERIVING ANYTHING. A second
       "does a pie_ack row exist" test here is the one mistake that matters: seen_at is
       EPISODE-SCOPED (an entry can be unqueued and re-queued under the same key - guest_excited
       re-dates on a moved show), so a bare existence test would let the ack from a previous
       queueing suppress this one's email permanently and silently.
    ⚠ Returns None when the feed cannot be read, and every caller must treat None as
      "suppress nothing". That is also what makes the service_role-only grant a non-issue: if this
      connection's role cannot execute the function, approvals simply email as before."""
    try:
        cur.execute("select public.pie_approvals()")
        feed = cur.fetchone()[0] or []
        if isinstance(feed, str):
            feed = json.loads(feed)
        return {i["id"]: i.get("seen_at") for i in feed if isinstance(i, dict) and i.get("id")}
    except Exception as e:
        print("pie: could not read the approvals feed - suppressing nothing this sweep:", e)
        return None


def _pie_item_id(deal_id, key):
    """Must match pie_approvals()' own id expression exactly, or nothing can ever be matched."""
    return "appr_%s_%s" % (deal_id, key)


def _pie_hold_decision(deal_id, key, entry, pie_on, seen_map, now_utc):
    """Should the authorize email for this held entry be SENT right now?

    Returns (send_now: bool, why: str). Called both at queue time and by the catch-up pass, so the
    two can never disagree about an item."""
    if not pie_on:
        return True, "suppression off"
    # ⚠⚠ FROM HERE EVERY PATH RETURNS False. The phone owns approvals while the switch is on, so
    #    there is no timeout and no "we cannot tell, so email it" branch left. The reasons below
    #    differ only so the Actions log still says WHY an email did not go - which matters more
    #    now, not less, because the log is the only place this is visible.
    item = _pie_item_id(deal_id, key)
    if seen_map is None:
        # The feed could not be read THIS SWEEP. The entry is still in the database and will
        # appear in the feed as soon as it recovers, so emailing here would announce something
        # that is going to reach the phone anyway.
        return False, "suppression on - feed unreadable this sweep, it will bubble when it recovers"
    if item not in seen_map:
        # Not in the feed => not pending any more, or snoozed. Nothing to announce either way.
        return False, "not pending (approved, cancelled or snoozed)"
    if seen_map.get(item):
        return False, "seen on the phone at %s" % seen_map[item]
    q = entry.get("approval_queued_at")
    age = ""
    if q:
        try:
            qt = datetime.datetime.strptime(str(q)[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc)
            age = " (unseen for %.0f min)" % ((now_utc - qt).total_seconds() / 60.0)
        except Exception:
            age = " (unparseable approval_queued_at %r)" % q
    return False, "suppression on - waiting for the phone%s" % age


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
            try:
                cur.execute("insert into error_log(source,message,detail) values(%s,%s,%s)",
                            ("mailer", ("send failed to %s: %s" % (to, str(_e)))[:500], str(subj)[:2000]))
            except Exception:
                pass
            return None
    mailer.send_email = _safe_send   # covers every send site in this run without touching each call
    cols_d=["id","stage","show_date","show_time","venue_address","occasion","company","guest_of_honor",
            "proposal_link","audience_details","show_format","amount","deposit_amount","balance_amount",
            "event_type","is_repeat","customize_token","trivia","trivia_received_at","trivia_notified_at",
            "performer_id","commission_amount","proposal_sent_at","photos_received_at","photos_notified_at",
            "deal_name","cue_state","stage_changed_at","created_at","primary_contact_id","gcal_url",
            "deposit_status","deposit_paid_at","require_trivia","send_stage_intro","photo_goh_limit","photo_guest_limit",
            "last_client_reply_at"]
    # EGRESS: fetch only deals this run can act on (was: all 512 rows incl. every cue_state, ~1MB/run).
    # Kept sets (mirror every pass below — update BOTH when adding a pass):
    #   * CUE stages (incl. gcal/w9/gig check-ins/magic-castle-cw subsets of these)
    #   * closed_lost: within 60 days of the show (daybefore/_after, show-anchored) OR ~10.5 months
    #     after it was MARKED lost (closed_lost_reengage, anchor=stage_changed_at, off:319)
    #   * un-notified trivia/photo submissions (any stage)
    #   * paid deposits whose magic_castle invite hasn't been handled (any stage)
    #   * any live cue entry with send_now / blocked / pending_approval (any stage - the
    #     send-now sweep, blocked re-check, approval refresh and _depreceipt_ PDF pregen)
    # GROWTH BOUND (2026-07-18): closed_won / refer_won are TERMINAL - they accumulate forever, so an
    # unbounded "stage in (...)" made every sweep re-read every gig ever played. Since the sweep runs
    # ~35x/day, that is the one term in this system that grows without limit as Simon books more work.
    # The bound is 300 days because the LAST show-anchored ladder step is ("rebook","show",240) - a
    # rebook nudge 240 days after the show. 300 leaves ~2 months of slack past it. Deals with no
    # show_date are always kept (they can't be date-judged), as are future dates.
    # !! If you ever add a CUE entry anchored to "show" with an offset above 240, RAISE THIS NUMBER
    #    or that email will silently never fire for older deals.
    _sweep_where = """
        stage in ('booked','proposal_sent','schedule_call','refer')
        or (stage in ('closed_won','refer_won')
            and (show_date is null or show_date >= current_date - 300))
        or (stage = 'closed_lost' and (
                (show_date is not null and show_date >= current_date - 60)                     -- daybefore(-1)/after(+2), show-anchored
                or (stage_changed_at is not null                                               -- reengage(+319 from stage_changed_at): 10.5mo win-back
                    and stage_changed_at >= current_date - 380 and stage_changed_at <= current_date - 300)
                -- ...and years 2-4, each due a year after the PREVIOUS step was sent. Bounded to a
                -- 95-day window per step so this stays a handful of rows and never grows unbounded.
                -- Once a step is queued for approval the pending_approval clause below keeps the
                -- deal in the sweep, so the window only has to be wide enough to catch the queueing.
                -- Dates are compared as ISO TEXT (no ::date cast) so a malformed value cannot error.
                or exists (select 1 from (values ('closed_lost_reengage'),('closed_lost_reengage_y2'),
                                                 ('closed_lost_reengage_y3')) _p(k)
                           where coalesce(cue_state->_p.k->>'sent','') <> ''
                             and cue_state->_p.k->>'sent' <= to_char(current_date - 365,'YYYY-MM-DD')
                             and cue_state->_p.k->>'sent' >= to_char(current_date - 460,'YYYY-MM-DD'))))
        or (trivia_received_at is not null and (trivia_notified_at is null or trivia_notified_at < trivia_received_at))
        or (photos_received_at is not null and (photos_notified_at is null or photos_notified_at < photos_received_at))
        or (deposit_status = 'paid'
            and (cue_state->'magic_castle'->>'sent') is null
            and coalesce(cue_state->'magic_castle'->>'cancelled','false') = 'false')
        or exists (select 1 from jsonb_each(coalesce(cue_state,'{}'::jsonb)) _e(k,v)
                   where jsonb_typeof(_e.v) = 'object'
                     and coalesce(_e.v->>'sent','') = ''
                     and coalesce(_e.v->>'cancelled','') in ('','false')
                     and ((_e.v->>'send_now') = 'true' or _e.v ? 'blocked' or (_e.v->>'pending_approval') = 'true'))
    """
    cur.execute("select "+",".join(cols_d)+" from deals where "+_sweep_where)
    deals=[dict(zip(cols_d,r)) for r in cur.fetchall()]
    # EGRESS: fetch only the contacts on the actionable deals (was: all ~2,700 contacts every run).
    # Every CB lookup below is CB.get(deal.primary_contact_id) for a deal in `deals`, so this is complete.
    _cids=list({str(d.get("primary_contact_id")) for d in deals if d.get("primary_contact_id")})
    if _cids:
        cur.execute("select id,first_name,last_name,full_name,name_pronunciation,email,phone_mobile,phone_other from contacts where id::text = any(%s)", (_cids,))
        CB={r[0]:dict(zip(["id","first_name","last_name","full_name","name_pronunciation","email","phone_mobile","phone_other"],r)) for r in cur.fetchall()}
    else:
        CB={}
    global PERF, DEP_CLAIMED, REENGAGE_BLOCKED
    cur.execute("select id, first_name, full_name from performers")
    PERF={r[0]:{"first_name":r[1],"full_name":r[2]} for r in cur.fetchall()}
    # One small query, not a per-deal lookup: the set is normally 1-3 rows.
    cur.execute("""select distinct dd.deal_id from deposit_docs dd join deals d on d.id=dd.deal_id
                   where dd.confirmed_at is not null and coalesce(dd.confirmed_method,'') <> 'ACH'
                     and coalesce(d.deposit_status::text,'') not in ('paid','not_required')""")
    DEP_CLAIMED=set(r[0] for r in cur.fetchall())
    if DEP_CLAIMED: print("deposit chase held for %d deal(s) - client says payment is on the way" % len(DEP_CLAIMED))
    # One query per run: lost deals whose client has since come back with a NEW deal. Years 2-4 of
    # the win-back are suppressed for these (see _flag_suppressed).
    cur.execute("""select d.id from deals d
                   where d.stage='closed_lost' and d.primary_contact_id is not null
                     and d.stage_changed_at is not null
                     and exists (select 1 from deals n
                                 where n.primary_contact_id = d.primary_contact_id
                                   and n.id <> d.id and n.created_at > d.stage_changed_at)""")
    REENGAGE_BLOCKED=set(r[0] for r in cur.fetchall())
    if REENGAGE_BLOCKED: print("win-back chain held for %d deal(s) - client already came back" % len(REENGAGE_BLOCKED))
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

    # ---- RETRACT deposit chases that are queued but no longer needed (Simon 2026-10-05) ----------
    # `_flag_suppressed` stops a NEW chase being queued once the deposit is paid, and app.js's showIf
    # hides it from the ladder display -- but NEITHER retracts one that was ALREADY queued before the
    # money arrived. That entry just sits in the approval queue, and approving it chases a client who
    # has already paid. (Measured 2026-10-05: 4 such entries, one pending approval since 09-13 on a
    # deal marked paid.)
    # ⚠️ CANCELLED, not deleted. `cancelled` is the shape the rest of the system already understands --
    # approvalPending(), hasPendingApproval() and approve_counts() all ignore it -- so the entry leaves
    # the queue, stops counting toward the badge, and still leaves an audit trail of what was dropped.
    # ⚠️ Covers BOTH payment routes: the Stripe webhook and a manual "mark paid" both only set
    # deals.deposit_status, so keying on that column is what makes this catch every case.
    _retracted = 0
    for d in deals:
        if (d.get("deposit_status") or "") not in ("paid", "not_required"): continue
        _st = d.get("cue_state") or {}
        if isinstance(_st, str):
            try: _st = json.loads(_st or "{}")
            except ValueError: continue
        for _k in ("deposit_chase_1", "deposit_chase_2"):
            _e = _st.get(_k)
            if not isinstance(_e, dict): continue
            if _e.get("sent") or _e.get("cancelled"): continue
            _st[_k] = {**_e, "cancelled": True,
                       "cancelled_reason": "deposit %s" % (d.get("deposit_status") or "paid")}
            d["cue_state"] = _st
            _retracted += 1
            if SEND: _save_cue(cur, d["id"], _st, _k)
    if _retracted:
        print("retracted %d queued deposit chase(s) - deposit already paid / not required" % _retracted)

    # ---- RE-DATE a queued "fun action" when the SHOW DATE has moved (Simon 2026-10-13) --------
    # "if the event date changes, the fun action email should be scheduled for the same intervals
    #  based on the new date. if it has already been sent it does not have to enter the cue again."
    #
    # _ge_send_date recomputes from show_date every sweep, so a date change already reschedules an
    # entry that has NOT been queued yet. The gap is the one already sitting in the approval queue:
    # pending_since is stored, so it just stays there on the old timing.
    #
    # The test is simply "is this entry's correct date still in the future?" - no need to record the
    # show_date it was computed against:
    #   * show pushed LATER  -> the new show-10 is in the future -> unqueue, it returns on the day
    #   * show pulled EARLIER -> the new date is today or past    -> leave it, it is genuinely due
    #   * second send no longer applicable (want is None)         -> unqueue
    # WARNING: this UNQUEUES, it does not cancel. `cancelled` is permanent - the main loop skips a
    # cancelled entry forever - so cancelling here would mean a moved show silently kills the email
    # instead of moving it. Only the approval flags come off; body/subject/to are kept so nothing
    # hand-edited is lost.
    # WARNING: `sent` is checked FIRST and never touched - an email already out does not come back.
    _redated = 0
    for d in deals:
        _st = d.get("cue_state") or {}
        if isinstance(_st, str):
            try: _st = json.loads(_st or "{}")
            except ValueError: continue
        for _k in ("guest_excited", "guest_excited_2"):
            _e = _st.get(_k)
            if not isinstance(_e, dict): continue
            if _e.get("sent") or _e.get("cancelled"): continue
            if not _e.get("pending_approval"): continue
            _want = _ge_send_date(d, _k)
            if _want is not None and _want <= TODAY: continue     # still genuinely due today
            _st[_k] = {k: v for k, v in _e.items()
                       if k not in ("pending_approval", "approve_token", "pending_since",
                                    "flag_approval_sent") + _PIE_EPISODE_KEYS}
            d["cue_state"] = _st
            _redated += 1
            if SEND: _save_cue(cur, d["id"], _st, _k)
    if _redated:
        print("re-dated %d queued fun-action email(s) - show date moved" % _redated)

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
            eff = "flag" if (d.get("is_repeat") and (anchor=="after_thank_you" or (anchor=="show" and off>0))) else mode
            # R4: the client replied -> a pre-call chase / proposal follow-up becomes FLAG (never auto-sent)
            _replied = _client_replied(key, d, st)
            if _replied: eff = "flag"
            if eff not in AUTO_MODES:
                # FLAG emails are never auto-sent. But from 7am ET on their due date we send Simon ONE approval
                # request (Approve / Edit / Cancel) so flagged emails ALSO route through his inbox. This is
                # ALWAYS ON - independent of APPROVAL_MODE - so it keeps happening even when fully live.
                # Catch-up: if the 7am sweep is missed, the next sweep after 7am sends it; if the due date has
                # already passed with no request, the next sweep sends it. pending_approval/flag_approval_sent
                # guard it to exactly once. (He can still send/edit/cancel from Focus; both paths are
                # double-send-safe via the 'sent' guards in send-now.js and approve-email.js.)
                if eff == "flag":
                    tf = TPL.get(key)
                    if tf and not _flag_suppressed(key, d):
                        basef = anchor_date(d, anchor, stages)
                        if basef:
                            # The SAME function the auto branch uses. `customization_request` is
                            # flag mode, so this is the branch that actually decides its date.
                            sdatef = _cue_send_date(d, key, basef, off)
                            if sdatef is None:
                                continue                        # dynamic date, outside its window
                            ef = st.get(key) or {}
                            _efov = ef.get("date_override")
                            if _efov:
                                try: sdatef = datetime.date.fromisoformat(_efov)
                                except (ValueError, TypeError): pass
                            if (SEQ_START <= sdatef <= TODAY and contact.get("email")
                                    and not ef.get("sent") and not ef.get("cancelled")
                                    and not ef.get("pending_approval") and not ef.get("flag_approval_sent")
                                    and not (sdatef == TODAY and tz.hour() < 7)):   # on the due day, hold until 7am ET
                                Vf = merge_values(d, contact)
                                subjf = ef["subject"] if ef.get("subject") is not None else fill_subject(tf["subject"], Vf)
                                hbodyf = ef["body"] if ef.get("body") is not None else render_html(tf["body"], Vf, "")
                                tokf = ef.get("approve_token") or secrets.token_urlsafe(24)
                                st[key] = {**ef, "pending_approval": True, "approve_token": tokf,
                                           "to": contact["email"], "subject": subjf, "body": hbodyf,
                                           "flag_approval_sent": TODAY.isoformat(),
                                           # step 3: the clock the one-hour phone hold is measured from
                                           "approval_queued_at": NOW_UTC.isoformat(timespec="seconds").replace("+00:00", "Z"),
                                           "pending_since": ef.get("pending_since") or TODAY.isoformat()}
                                held_new.append((contact.get("full_name") or d.get("deal_name") or "deal", key,
                                                 contact["email"], subjf, hbodyf, tokf, d["id"],
                                                 ("The client replied (%s) - this follow-up is paused instead of sending itself. Send it only if it still fits, or Cancel to drop it." % str(d.get("last_client_reply_at"))[:10])
                                                 if _replied else
                                                 "Flagged email - you normally send these by hand. Use Preview & Send to review and send it, or Cancel to drop it."))
                                if SEND:
                                    _save_cue(cur, d["id"], st, key)
                continue
            t=TPL.get(key)
            if not t: continue
            base=anchor_date(d,anchor,stages)
            if not base: continue
            send_date = _cue_send_date(d, key, base, off)
            if send_date is None:
                continue                                        # dynamic date, outside its window
            e = st.get(key) or {}
            if e.get("sent") or e.get("cancelled"): continue
            # HARD EXIT: if date_override is a future date, skip immediately before computing send_date,
            # merge values, or anything else. This is the authoritative guard — no downstream code path
            # can accidentally re-queue an email that Simon has pushed to the future.
            _ov_pre = e.get("date_override")
            if _ov_pre:
                try:
                    _ov_pre_date = datetime.date.fromisoformat(_ov_pre)
                    if _ov_pre_date > TODAY:
                        if e.get("pending_approval") or e.get("pending_since") or e.get("approve_token"):
                            st[key] = {k: v for k, v in e.items()
                                       if k not in ("pending_approval", "pending_since", "approve_token") + _PIE_EPISODE_KEYS}
                            if SEND:
                                _save_cue(cur, d["id"], st, key)
                                print(f"  [hard-exit] {d['id'][:8]} {key}: cleared stale approval fields (date_override={_ov_pre})")
                        continue
                except (ValueError, TypeError):
                    pass
            # date_override: Simon can reschedule an email via the CRM; use that date instead of the ladder default.
            _ov = e.get("date_override")
            _ov_date = None
            if _ov:
                try:
                    _ov_date = datetime.date.fromisoformat(_ov)
                    send_date = _ov_date
                except (ValueError, TypeError):
                    pass
            print(f"  [cue] {d['id'][:8]} {key}: base={base} send_date={send_date} ov={_ov!r} pa={e.get('pending_approval')}")
            # Migration cutover: SEQ_START is the send-date FLOOR (the day the new CRM took over from Zoho).
            # Skip anything scheduled BEFORE it (Zoho's backlog - permanently). Send today's, and CATCH UP any
            # scheduled on/after the floor that slipped (missed). Never re-send the pre-cutover backlog.
            if send_date < SEQ_START or send_date > TODAY:
                # date_override pushed this into the future (or past before cutover): clean any stale
                # pending_approval from in-memory st AND from the DB so it never shows in the approval queue.
                if _ov_date and _ov_date > TODAY:
                    _needs_clean = e.get("pending_approval") or e.get("pending_since")
                    if _needs_clean:
                        st[key] = {k: v for k, v in e.items()
                                   if k not in ("pending_approval", "pending_since") + _PIE_EPISODE_KEYS}
                        if SEND:
                            _save_cue(cur, d["id"], st, key)
                            print(f"    -> cleared stale pending_approval for {key} (date_override={_ov})")
                continue
            V=merge_values(d,contact)
            blanks = missing_fields(t, e, V)
            _email_ok = bool(contact.get("email"))
            if not _email_ok and not APPROVAL_MODE: continue
            if blanks and not APPROVAL_MODE:
                # PAUSE: a required merge field is blank -> do NOT send. Flag it so blocked_digest.py
                # nags Simon daily and the re-check pass below auto-sends it once the field is filled.
                was_blocked = bool(e.get("blocked"))
                _blocked_entry = {**e, "blocked": {"since": (e.get("blocked") or {}).get("since", TODAY.isoformat()), "fields": blanks}}
                st[key] = _blocked_entry
                blocked_today.append((d, key, contact["email"], blanks))
                if not was_blocked:   # first time this email is paused -> immediate alert to Simon (below)
                    new_blocks.append((contact.get("full_name") or d.get("deal_name") or "deal", key, blanks, d["id"]))
                if SEND and _blocked_entry != e:   # unchanged block -> skip the pointless rewrite
                    _save_cue(cur, d["id"], st, key)
                continue
            if e.get("blocked"):                          # was paused, now complete -> clear the flag before sending
                e = {k: v for k, v in e.items() if k != "blocked"}; st[key] = e
            subj = e["subject"] if e.get("subject") is not None else fill_subject(t["subject"],V)
            tbody = t["body"]
            if (st.get("magic_castle") or {}).get("sent"):   # invite already went out -> drop the "I'll email you the invite" heads-up
                tbody = _strip_mc_headsup(tbody)
            body = e["body"] if e.get("body") is not None else render_html(tbody,V,signature)
            if APPROVAL_MODE:
                # Safety valve: if date_override puts this email in the future, never queue for approval.
                # The primary guard is the send_date check above; this is a belt-and-suspenders fallback
                # in case send_date was somehow stale when the check ran.
                if _ov_date and _ov_date > TODAY:
                    _needs_clean = e.get("pending_approval") or e.get("pending_since")
                    if _needs_clean:
                        st[key] = {k: v for k, v in e.items()
                                   if k not in ("pending_approval", "pending_since") + _PIE_EPISODE_KEYS}
                        if SEND:
                            _save_cue(cur, d["id"], st, key)
                    continue
                # HOLD for approval: never auto-send. Store the email WITHOUT the signature - the authorize
                # email shows a clean preview, and the signature is added inline (CID image) on Approve so it
                # renders without "display images". Store a token for the Approve/Cancel links.
                # If email or merge fields are missing, queue it anyway with missing_fields so the
                # approval queue UI can surface them and let Simon fill them inline.
                was_pending = bool(e.get("pending_approval"))
                token = e.get("approve_token") or secrets.token_urlsafe(24)
                hbody = e["body"] if e.get("body") is not None else render_html(tbody, V, "")   # no signature
                _missing = ([] if _email_ok else ["contact_email"]) + blanks
                new_entry = {**e, "pending_approval": True, "approve_token": token,
                             "to": contact.get("email") or "",
                             "subject": subj, "body": hbody,
                             # step 3: the clock the one-hour phone hold is measured from. `or` and
                             # not a plain set: this block re-runs every sweep for an entry that is
                             # ALREADY pending, and restamping it would restart the hold each time.
                             "approval_queued_at": e.get("approval_queued_at")
                                                   or NOW_UTC.isoformat(timespec="seconds").replace("+00:00", "Z"),
                             "pending_since": e.get("pending_since") or TODAY.isoformat()}
                if _missing:
                    new_entry["missing_fields"] = _missing
                elif "missing_fields" in new_entry:
                    del new_entry["missing_fields"]
                st[key] = new_entry
                if not was_pending:
                    _note = ("Cannot send yet — missing: " + ", ".join(_missing)) if _missing else None
                    held_new.append((contact.get("full_name") or d.get("deal_name") or "deal", key,
                                     contact.get("email") or "(no email)", subj, hbody, token, d["id"],
                                     *([_note] if _note else [])))
                # Write ONLY when the entry actually changed. Rewriting an identical entry every run
                # was pure churn: pointless UPDATE egress + a realtime event making every open CRM
                # tab re-sync, every sweep, for every pending email.
                if SEND and new_entry != e:
                    _save_cue(cur, d["id"], st, key)
                continue
            due.append((d,key,contact["email"],subj,body,st))

    # CLIENT CADENCE WINDOW (Simon 2026-07-04): only send the due-today client emails between
    # 9am and 9pm Eastern. Send-now, the trivia/photo notifications, self gig check-ins and the
    # self-nags are NOT gated - they run whenever the sweep runs. Pass --anytime to bypass the
    # window (manual / one-off runs).
    _cad_ok = ('--anytime' in sys.argv) or (9 <= tz.hour() <= 21)
    print(f"{TODAY}  -  {len(due)} email(s) due today  (mode: {'SEND' if SEND else 'DRY-RUN'})"
          + ('' if _cad_ok else f'  [holding: outside 9am-9pm ET window, now {tz.hour()}:00 ET]'))
    for (d,key,to,subj,body,st) in due:
        print(f"  -> {to}  |  [{key}]  {subj[:70]}")
        if SEND and _cad_ok:
            if _claim_dispatch(cur, d["id"], (st.get(key) or {}).get("dispatch_id") or key):
                mailer.send_email(to, subj, body)
            else:
                print("     (already dispatched - skipping)")
            ne={**(st.get(key) or {}), "sent":TODAY.isoformat(), "sent_at":_now_iso()}; ne.pop("blocked",None)
            _archive_sent(cur, d["id"], key, to, subj, body, ne["sent_at"]); st[key]=_strip_heavy(ne)
            _save_cue(cur, d["id"], st, key)
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
    # PIE CONTROL STEP 3: read the switch and the feed ONCE for the whole sweep.
    # ⚠⚠ AN UNREADABLE FEED NO LONGER TURNS SUPPRESSION OFF. It used to (`_pie_on = False` ->
    #    announce everything), which was right while email was the fallback. Now that the phone
    #    owns approvals outright, a feed that happens to be unreadable on one sweep is a transient
    #    on OUR side - the entry is in the database and will bubble when the feed recovers - so
    #    re-enabling the email here would send exactly the approval emails Simon asked to stop.
    #    _pie_hold_decision handles seen_map=None itself, and returns False.
    _pie_on = _pie_suppress_on(cur)
    _pie_seen = _pie_seen_map(cur) if _pie_on else None
    if _pie_on:
        if _pie_seen is None:
            print("pie: suppression ON; the approvals feed could not be read this sweep - "
                  "nothing will be emailed, and it will bubble when the feed recovers")
        else:
            print("pie: suppression ON (the phone owns approvals; no authorize email is sent); "
                  "%d item(s) in the feed, %d already seen"
                  % (len(_pie_seen), sum(1 for v in _pie_seen.values() if v)))

    # ⚠⚠ THE AUTHORIZE EMAILS ARE NO LONGER SENT HERE (step 3). This loop only logs what was newly
    #    held and carries each item's optional plain-language note across to the single pass below,
    #    which is now the ONE place an authorize email is sent and the ONE place
    #    approval_email_sent is stamped. Sending from two places is how "already announced?" and
    #    "announce it" start to disagree, and the failure mode is a client email nobody is told
    #    about. With suppression OFF the pass runs in this same sweep, so the timing is unchanged.
    _pie_notes = {}
    if held_new:
        print(f"{TODAY}  -  {len(held_new)} email(s) newly HELD for approval")
        for row in held_new:
            nm,key,to,subj,body,token,did = row[:7]
            print(f"  -> [held] {to}  |  [{key}]  {subj[:60]}")
            if len(row) > 7 and row[7]:
                _pie_notes[(str(did), key)] = row[7]
        # PIE CONTROL DOORBELL (step 2, 2026-10-24, briefs/pie-control-queue.md). Pie Control polls
        # the ?a=piefeed feed every 30 min; this says "poll NOW" so a new approval reaches the phone
        # in seconds instead of up to half an hour.
        #
        # ⚠⚠ THE PUSH CARRIES NO ITEM DATA, AND THAT IS THE WHOLE DESIGN - the same contract as the
        #    CRM page's doorbell to the AHK watcher. The phone still asks the feed for the real
        #    list, so a DUPLICATE push costs one cheap poll and a MISSED one costs nothing but the
        #    normal 30-minute tick. Nothing parses the text; the count is there to be read by a
        #    human, because Join renders it as an ordinary notification.
        # ⚠ ONE push per sweep, not one per email: a sweep that holds three approvals rings once.
        # ⚠ It is best-effort and deliberately swallowed. join.push() never raises, and an approval
        #    must never fail to be HELD because a phone notification did not go out.
        # ⚠ It fires only for approvals queued BY THIS SWEEP. Ones queued in the browser ("Send
        #    later", a stage popup, _ctupd_) or by run-due.js (magic_castle) ring no bell and arrive
        #    on the next poll - acceptable, because the feed is the source of truth and the bell is
        #    only ever an optimisation.
        if SEND:
            # !! NO LONGER A JOIN PUSH (2026-10-25). Simon: "i would like you to figure out an
            #    instant option that does not need a notification ... silent would be better".
            #    public.pie_ring() writes a tiny Firestore document that Pie Control has an open
            #    snapshot listener on - instant, and nothing appears on his phone at all. It is
            #    the ONE place that decides how to ring, shared with approve-email.js, so this
            #    caller did not have to know that the transport changed.
            # !! Still best-effort and swallowed: the push carries no data (the phone fetches the
            #    real list itself), so a failed ring costs the normal 30-minute poll and nothing
            #    more. An approval must never fail to be HELD because a doorbell did not go.
            try:
                _n = len(held_new)
                cur.execute("select public.pie_ring(%s)",
                            ("%d approval%s queued" % (_n, "" if _n == 1 else "s"),))
            except Exception as _jx:
                print("pie doorbell failed (harmless):", _jx)

    # ---- AUTHORIZE EMAILS: the one place they are sent (Pie Control step 3) -------------------
    # Walks every entry this script has queued for approval and not yet announced, and sends the
    # authorize email unless the phone has it covered. With suppression OFF this runs in the same
    # sweep that queued the item, so Simon's inbox behaves exactly as it did before step 3.
    #
    # ⚠⚠ SCOPE IS `approval_queued_at`, AND THAT IS WHAT KEEPS THIS NARROW. Only entries THIS
    #    script queued carry it, so approvals created in the browser ("Send later", a stage popup,
    #    _ctupd_) and by run-due.js are untouched - they never got an authorize email before and
    #    they must not start getting one now. Entries queued before step 3 shipped have no stamp
    #    either, and were already announced at the time.
    # ⚠ magic_castle and w9_email send their own authorize emails at their own sites and are
    #   deliberately NOT routed through here. Both are rare and individually consequential, so an
    #   hour's delay costs more than the quieter inbox buys. If Simon wants them held too, they
    #   each need an approval_queued_at stamp and their inline send removed - not a second copy of
    #   this decision.
    _auth_sent = _auth_held = 0
    for d in deals:
        st = d.get("cue_state") or {}
        if isinstance(st, str): st = json.loads(st or "{}")
        _changed = []
        for key, e in list(st.items()):
            if not isinstance(e, dict): continue
            if not e.get("pending_approval"): continue
            if e.get("sent") or e.get("cancelled"): continue
            if not e.get("approval_queued_at"): continue        # not ours to announce
            if e.get("approval_email_sent"): continue           # already announced this episode
            contact = CB.get(d.get("primary_contact_id")) or {}
            # ⚠ The client name, not deal_name - that column holds the client's phone, their email
            #   and "(copy)" in practice, so it makes a log line unreadable. Resolved once here so
            #   the hold line and the authorize line below name the deal the same way.
            nm = contact.get("full_name") or d.get("deal_name") or "deal"
            _send_now, _why = _pie_hold_decision(d["id"], key, e, _pie_on, _pie_seen, NOW_UTC)
            if not _send_now:
                _auth_held += 1
                print(f"  -> [pie-hold] [{key}] {nm} - {_why}")
                continue
            subj = e.get("subject") or "(no subject)"
            row = (nm, key, e.get("to") or "(no email)", subj, e.get("body") or "",
                   e.get("approve_token") or "", d["id"])
            _note = _pie_notes.get((str(d["id"]), key))
            if _note:
                row = row + (_note,)
            print(f"  -> [authorize] [{key}] {nm}  ({_why})")
            if SEND:
                # ⚠ The stamp is written only AFTER the send returns. mailer.send_email goes through
                #   the phase-1 resilience wrapper and records a failure rather than raising, so
                #   stamping first would mark an email announced that never arrived - and nothing
                #   would ever retry it.
                mailer.send_email("simon@thesimonshow.com",
                                  f"Approval for {nm}: {subj}",
                                  _authorize_email_html([row]), owner=True)
                st[key] = {**e, "approval_email_sent":
                           NOW_UTC.isoformat(timespec="seconds").replace("+00:00", "Z")}
                _changed.append(key)
            _auth_sent += 1
        if SEND and _changed:
            _save_cue(cur, d["id"], st, *_changed)
    if _auth_sent or _auth_held:
        print("%s  -  authorize emails: %d sent, %d held back for the phone"
              % (TODAY, _auth_sent, _auth_held))
        if _pie_on and _auth_sent:
            print("  !! %d authorize email(s) went out WITH SUPPRESSION ON - that should be "
                  "impossible; _pie_hold_decision returns False on every path while it is on."
                  % _auth_sent)

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
            # Scheduled-for-later (approve-email.js sets send_after when approving a delayed send).
            # MIRRORS the identical guard in BOTH of run-due.js's send_now loops. Without it this
            # engine sends a deferred email on its very next sweep and the delay means nothing.
            if e.get("send_after"):
                try:
                    _sa = datetime.datetime.fromisoformat(str(e["send_after"]).replace("Z", "+00:00"))
                    if _sa.tzinfo is None: _sa = _sa.replace(tzinfo=datetime.timezone.utc)
                    if datetime.datetime.now(datetime.timezone.utc) < _sa: continue
                except Exception:
                    pass        # unparseable -> treat as due rather than stranding the email forever
            if e.get("sent") or e.get("cancelled"): continue
            # Respect an in-flight claim from the instant send-now endpoint (avoid a sweep-vs-instant double-send).
            # A claim older than 5 min is stale (a crashed sender) and fair game again.
            cl = e.get("claimed")
            if cl:
                try:
                    _dt = datetime.datetime.fromisoformat(str(cl))
                    if _dt.tzinfo is None: _dt = _dt.replace(tzinfo=datetime.timezone.utc)
                    if (datetime.datetime.now(datetime.timezone.utc) - _dt).total_seconds() < 300: continue
                except Exception:
                    continue   # unparseable claim -> let the instant channel own it (never risk a double-send)
            t=TPL.get(key)
            to = (e.get("to") or contact.get("email") or "").strip()   # app can override the recipient
            if not to: continue
            V=merge_values(d,contact)
            # use the app-provided subject/body when present, else fall back to the template.
            subj = e["subject"] if e.get("subject") is not None else (fill_subject(t["subject"],V) if t else None)
            body = e["body"]    if e.get("body")    is not None else (render_html(t["body"],V,signature) if t else None)
            if subj is None or body is None: continue   # no template and no override -> nothing to send
            # exempt from safe mode: e.no_safe (owner SMS/notifications) + the new-lead inquiry confirmation
            # (_newlead_client_*) - a transactional auto-reply that must reach the lead, not reroute to Simon.
            no_safe = bool(e.get("no_safe")) or key.startswith("_newlead_client_")
            # optional cc on a send-now entry (mailer.send_email has always accepted it; the cue
            # just never passed one through). MIRRORED in vercel-send/api/run-due.js.
            now_due.append((d,key,to,subj,body,st,no_safe,contact,e.get("cc")))
    print(f"{TODAY}  -  {len(now_due)} send-now email(s)  (mode: {'SEND' if SEND else 'DRY-RUN'})")
    for (d,key,to,subj,body,st,no_safe,contact,cc) in now_due:
        print(f"  -> [now] {to}{('  cc:'+str(cc)) if cc else ''}  |  [{key}]  {subj[:70]}{'  [no-safe]' if no_safe else ''}")
        # ⚠⚠ THE ENTRY COMES FROM `st`, NEVER FROM `e`. The collection loop above binds `e` and
        #    Python leaks that binding past the end of the loop - so in HERE `e` is still the LAST
        #    cue entry of the LAST deal examined, not the one being sent. now_due deliberately
        #    carries no `e`. A stale `e.get("attach")` sent David Ganz and Christina Tell a
        #    "W9 Attached" / "Magic Castle Invite" with NOTHING ATTACHED on 2026-10-28, and would
        #    just as readily staple the W9 onto an unrelated email whenever that leftover entry
        #    happened to carry one. `st` + `key` are in the tuple precisely so this is unambiguous.
        _ent = st.get(key) or {}
        _claimkey = _ent.get("dispatch_id") or key
        if SEND:
            # PIE CONTROL (step 5): "✓ Sent", "Agreement signed" and "Deposit paid" become phone
            # bubbles instead of email. The decision AND the write are public.pie_absorb_owner_note,
            # so this and run-due.js cannot disagree - it returns false unless suppression is on,
            # the key is one of those event kinds, and the recipient is Simon himself.
            # ⚠ The entry is still marked SENT below, exactly as if it had gone out: the
            #   notification HAS been delivered, just somewhere else. Leaving it unsent would make
            #   every later sweep try again for ever.
            # ⚠ Any failure here emails as normal - a bubble is never worth losing a notification.
            _absorbed = False
            try:
                cur.execute("select public.pie_absorb_owner_note(%s,%s,%s,%s)", (d["id"], key, to, subj))
                _absorbed = bool(cur.fetchone()[0])
            except Exception as _ax:
                print("     pie absorb failed (emailing as normal):", _ax)
            if _absorbed:
                print("     -> Pie Control bubble, not emailed")
            elif _claim_dispatch(cur, d["id"], _claimkey):
                # The entry's own `attach` (set when it was held for approval) decides the PDF, so a
                # send_now routed through THIS engine keeps it. Was receipts-only, which would have
                # sent the Magic Castle invite with no invitation attached the moment that email
                # started going through send_now. attachments_for() already knows 'magic_castle'.
                _att_key = "deposit_receipt" if key.startswith("_depreceipt_") else (_ent.get("attach") or "")
                atts, _att_err = [], None
                if _att_key:
                    try:
                        atts = attachments.attachments_for(_att_key, d, contact) or []
                    except Exception as _ae:
                        _att_err = "%s: %s" % (type(_ae).__name__, _ae)
                    if not atts and not _att_err:
                        _att_err = "attachments_for(%r) returned nothing" % (_att_key,)
                # ⚠⚠ AN EMAIL WHOSE WHOLE POINT IS ITS PDF MUST NOT GO OUT WITHOUT IT. Give the claim
                #    back and leave the entry unsent, so the next sweep retries, rather than mailing a
                #    "W9 Attached" with no W9 - the exact failure this guard exists for. Loud, because
                #    mailer.send_email is wrapped to swallow errors and this one must not be swallowed.
                if _att_err:
                    print("     !! ATTACHMENT MISSING for [%s] - NOT sending: %s" % (key, _att_err[:200]))
                    _release_dispatch(cur, d["id"], _claimkey)
                    try:
                        cur.execute("insert into error_log(source,message,detail) values(%s,%s,%s)",
                                    ("mailer", ("attachment missing for %s - email HELD, not sent" % key)[:500],
                                     ("to %s | %s" % (to, _att_err))[:2000]))
                    except Exception:
                        pass
                    continue
                mailer.send_email(to, subj, body, owner=no_safe, attachments=atts, cc=cc)
            else:
                print("     (already dispatched by the instant channel - skipping)")
            ne={**_ent, "sent":TODAY.isoformat(), "sent_at":_now_iso()}; ne.pop("send_now",None); ne.pop("claimed",None)
            _archive_sent(cur, d["id"], key, to, subj, body, ne["sent_at"]); st[key]=_strip_heavy(ne)
            _save_cue(cur, d["id"], st, key)
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
                if SEND: _save_cue(cur, d["id"], st, key)
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
                    if SEND: _save_cue(cur, d["id"], st, key)
                continue
            subj = e["subject"] if e.get("subject") is not None else fill_subject(t["subject"],V)
            body = e["body"] if e.get("body") is not None else render_html(t["body"],V,signature)
            unblock.append((d,key,to,subj,body,st))
    print(f"{TODAY}  -  {len(unblock)} paused email(s) now complete -> sending")
    for (d,key,to,subj,body,st) in unblock:
        print(f"  -> [unblocked] {to}  |  [{key}]  {subj[:60]}")
        if SEND:
            if _claim_dispatch(cur, d["id"], (st.get(key) or {}).get("dispatch_id") or key):
                mailer.send_email(to,subj,body)
            else:
                print("     (already dispatched - skipping)")
            ne={**(st.get(key) or {}), "sent":TODAY.isoformat(), "sent_at":_now_iso()}; ne.pop("blocked",None); ne.pop("send_now",None)
            _archive_sent(cur, d["id"], key, to, subj, body, ne["sent_at"]); st[key]=_strip_heavy(ne)
            _save_cue(cur, d["id"], st, key)
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
        subj = ("‼️📅 GCal! - %s - %s" % (who, when)) if first else ("‼️📅 Reminder: add to calendar - %s - %s" % (who, when))
        print("  -> [gcal] %s" % subj[:70])
        if SEND:
            mailer.send_email("simon@thesimonshow.com", subj, _gcal_email_html(d, V, token, reminder=not first), owner=True)
            g = {**(st.get("_gcal") or {}), "token": token, "last": TODAY.isoformat()}
            if first: g["first_sent"] = TODAY.isoformat()
            st["_gcal"] = g
            _save_cue(cur, d["id"], st, "_gcal")
    if SEND and gcal_due: print("gcal reminders sent.")

    # ---- Magic Castle invite: ONE per deal (shared marker cue_state['magic_castle']). Closed-Won version
    #      fires at Closed Won (priority); deposit version the moment the deposit is marked paid. Whichever
    #      fires first claims the marker; the other is suppressed. The PDF rides on attach:'magic_castle'.
    #
    #      ⚠⚠ IT AUTO-SENDS as of 2026-10-27 and no longer enters the approval queue at all
    #         (Simon: "have them skip the approval cue and just send"). So there is no approve_token,
    #         no pending_approval and no authorize email from here - only a send_now entry and an
    #         owner note. The Closed-Won twin in run-due.js's mc_init block was changed the same day
    #         and must stay in step; it is the only other place this key is queued.
    #      ⚠ The deposit version still waits MC_SEND_DELAY_MIN. That buffer has moved twice: it was
    #        a delay before entering the queue, then (2026-10-13) a delay after approval, and it is
    #        now a delay before an unattended send - which makes it the ONLY way to stop one, by
    #        cancelling the entry in the deal's Email cue inside those ten minutes.
    #      ⚠ The approval queue is therefore no longer what makes this once-per-deal. The guard on
    #        the entry is, so it tests send_now as well as sent/cancelled. ----
    # ⚠⚠ MIRRORS MC_SEND_DELAY_MIN in approve-email.js and app.js. THIRD COPY - keep all three equal.
    #    Simon 2026-10-27 kept the 10 minutes when these stopped needing approval: with no approval
    #    click, this buffer is the ONLY window in which an invite can still be pulled back.
    MC_SEND_DELAY_MIN = 10
    _now = datetime.datetime.now(datetime.timezone.utc)

    # ⚠⚠ STRIPPED when an entry is rewritten as an auto-send. Both blocks build the new entry as
    #    {**e, ...}, and `e` is an EXISTING entry - so without this an invite that had been held
    #    would be written back carrying pending_approval and approve_token ALONGSIDE send_now:
    #    sent to the client AND still sitting in the approval queue, which is the "approval
    #    resurrected" failure the app.js _cueWrite audit (2026-10-24) exists to prevent.
    #    `snoozed_until` goes too, or a Send-later snooze would keep hiding an entry that has
    #    already gone out. Measured in a dry run before this was added: a real entry came through
    #    with pending_approval, approve_token, pending_since AND snoozed_until all intact.
    #    `send_after` is in the list for the same reason: it is a timing field from a previous life
    #    of this entry, and a stale FUTURE one would silently delay a send we meant to be immediate.
    #    Both blocks set it themselves after the spread when they want one, so nothing is lost.
    _APPROVAL_KEYS = ("pending_approval", "approve_token", "pending_since", "snoozed_until",
                      "flag_approval_sent", "approval_queued_at", "approval_email_sent",
                      "send_after")

    def _owner_note(st_, slug, label, client_email, when_iso=None):
        """Queue the "it went out" note for Simon as an ordinary _sentconf_ entry.

        ⚠ Deliberately NOT a bespoke mechanism: the _sentconf_ prefix is what pie_event_kind()
          maps to kind 'sent', so public.pie_absorb_owner_note turns it into a Pie Control bubble
          while suppression is on and emails it if Simon ever switches suppression off. One path,
          the same as every other owner notification.
        ⚠ It carries the SAME send_after as the email it reports, so the bubble appears when the
          invite actually goes rather than ten minutes early.
        """
        # ⚠ SLUG, not the label - a cue_state key must have no spaces. The first dry run
        #   produced "_sentconf_auto_Magic Castle invite_<ts>", which is legal JSON and a
        #   trap everywhere a key is put in a URL (?fixemail=) or split on whitespace.
        k = "_sentconf_auto_%s_%d" % (slug, int(_now.timestamp()))
        st_[k] = {"send_now": True, "to": "simon@thesimonshow.com",
                  "subject": "✓ Sent: %s to %s" % (label, client_email),
                  "body": "<p>%s went out to <b>%s</b>.</p><p>It sent on its own - these no longer "
                          "wait for your approval.</p>" % (label, client_email),
                  **({"send_after": when_iso} if when_iso else {})}
        return k

    mc_held = 0
    for d in deals:
        st = d.get("cue_state") or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        e = st.get("magic_castle") or {}
        if e.get("sent") or e.get("cancelled") or e.get("pending_approval") or e.get("send_now"): continue   # done, cancelled, awaiting approval, or already approved+queued
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
            if d.get("deposit_status")=="paid" and paid and paid.date() >= SEQ_START:
                version = "magic_castle"                           # deposit version, the moment the deposit is paid
        if not version: continue
        t = TPL.get(version)
        if not t: continue
        V = merge_values(d, contact)
        subj = fill_subject(t["subject"], V)
        # ⚠ Rendered WITH the signature, unlike the held version which left it off for approve-email.js
        #   to add. That makes it independent of which sender wins the race: run-due.js only appends a
        #   signature when the body does not already carry the card URL, and send_due.py appends none
        #   at all. Either way it goes out signed exactly once.
        hbody = render_html(t["body"], V, signature)
        nm = contact.get("full_name") or d.get("deal_name") or "deal"
        # ⚠⚠ AUTO-SEND, NO APPROVAL (Simon 2026-10-27: "have them skip the approval cue and just send").
        #    The deposit version keeps its 10-minute buffer, which is now the only way to stop one:
        #    cancel the entry in the deal's Email cue before it fires.
        #    ⚠ `attach` stays - it is what gets the invitation PDF on, and send-now.js now declines
        #      any entry carrying it so run-due.js (which has the map) does the send.
        when = ((_now + datetime.timedelta(minutes=MC_SEND_DELAY_MIN)).isoformat().replace("+00:00", "Z")
                if version == "magic_castle" else None)
        st["magic_castle"] = {**{k2: v2 for k2, v2 in e.items() if k2 not in _APPROVAL_KEYS},
                              "send_now": True,
                              "to": contact["email"], "subject": subj, "body": hbody,
                              "attach": "magic_castle", "via": version,
                              **({"send_after": when} if when else {})}
        note_key = _owner_note(st, "mc", "Magic Castle invite", contact["email"], when)
        print("  -> [magic:%s AUTO%s] %s  |  %s"
              % (version, (" +%dm" % MC_SEND_DELAY_MIN) if when else "", contact["email"], subj[:46]))
        if SEND:
            _save_cue(cur, d["id"], st, "magic_castle")
            _save_cue(cur, d["id"], st, note_key)
            mc_held += 1
    if SEND and mc_held: print(f"magic castle: {mc_held} queued to auto-send (no approval).")

    # ---- W9: on Closed Won for ORGANIZATION deals (corporate / school / non_profit / municipal - not a
    #      private family/individual). The W9 PDF rides on attach:'w9_email'. One per deal
    #      (cue_state['w9_email']). Keep W9_EVENT_TYPES in sync with app.js.
    #
    #      ⚠⚠ IT AUTO-SENDS as of 2026-10-27 (Simon: "have them skip the approval cue and just
    #         send"), with no buffer - see the Magic Castle block above for why that one keeps its
    #         ten minutes and this one does not.
    #      ⚠ pending_approval used to be what stopped it re-queueing every sweep. With nothing
    #        holding, send_now is: the guard below tests it, and WITHOUT that test every sweep
    #        would rewrite the entry and mint ANOTHER owner note, so Simon would get a bubble
    #        about the same W9 every half hour until it sent. ----
    W9_EVENT_TYPES = ("corporate", "school", "non_profit", "municipal")
    w9_held = 0
    for d in deals:
        if d.get("event_type") not in W9_EVENT_TYPES: continue
        if d.get("stage") != "closed_won": continue
        mv = _d(d.get("stage_changed_at"))
        if not mv or mv < SEQ_START: continue                 # only deals moved to Closed Won since go-live
        st = d.get("cue_state") or {}
        if isinstance(st,str): st=json.loads(st or "{}")
        e = st.get("w9_email") or {}
        if e.get("sent") or e.get("cancelled") or e.get("pending_approval") or e.get("send_now"): continue
        contact = CB.get(d.get("primary_contact_id")) or {}
        if not contact.get("email"): continue
        t = TPL.get("w9_email")
        if not t: continue
        V = merge_values(d, contact)
        subj = fill_subject(t["subject"], V)
        # Signed at queue time - see the note on the Magic Castle body above for why.
        hbody = render_html(t["body"], V, signature)
        nm = contact.get("full_name") or d.get("deal_name") or "deal"
        # ⚠⚠ AUTO-SEND, NO APPROVAL (Simon 2026-10-27). The held version's own note had already
        #    offered exactly this - "once you've seen a few go out and you're happy, just tell me
        #    and I'll let it send on its own" - so this is that switch being thrown.
        #    ⚠ No buffer here, unlike the deposit Magic Castle: a W9 goes to an organisation that
        #      has just booked and asked for it, so there is nothing to reconsider. It still only
        #      ever fires once per deal, on Closed Won, for the four organisation event types.
        #    ⚠ `attach` stays - it is what puts the W9 PDF on, and send-now.js declines it so
        #      run-due.js does the send.
        st["w9_email"] = {**{k2: v2 for k2, v2 in e.items() if k2 not in _APPROVAL_KEYS},
                          "send_now": True,
                          "to": contact["email"], "subject": subj, "body": hbody,
                          "attach": "w9_email"}
        note_key = _owner_note(st, "w9", "W9", contact["email"])
        print("  -> [w9 AUTO] %s  |  %s" % (contact["email"], subj[:46]))
        if SEND:
            _save_cue(cur, d["id"], st, "w9_email")
            _save_cue(cur, d["id"], st, note_key)
            w9_held += 1
    if SEND and w9_held: print(f"w9: {w9_held} queued to auto-send (no approval).")

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
            subj=f"‼️📅 GCal! [GIG {label}] {who} - {V['ShowDateShort']}"
            self_due.append((d,key,subj,selfcheckin_html(d,V,label)))
    print(f"{TODAY}  -  {len(self_due)} self gig check-in(s) due")
    for (d,key,subj,body) in self_due:
        print(f"  -> [self] {subj[:70]}")
        if SEND:
            mailer.send_email(self_to, subj, body)
            cur.execute("select cue_state from deals where id=%s",(d["id"],))   # read-modify-write (avoid clobber)
            cs=cur.fetchone()[0] or {}
            if isinstance(cs,str): cs=json.loads(cs or "{}")
            cs[key]={"sent":TODAY.isoformat(), "sent_at":_now_iso()}
            _save_cue(cur, d["id"], cs, key)
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
        photos_url = f"{crm_url}&photos=1"                     # opens the deal focused on the Pictures section
        body = ('<div style="font-family:Verdana,Arial,sans-serif;font-size:14px;color:#202124">'
                f'<p style="margin:0 0 10px">Download pictures at <a href="{html.escape(photos_url)}" '
                f'style="color:#1155cc;font-weight:bold;text-decoration:underline">{html.escape(photos_url)}</a></p>'
                f'<p style="margin:0">Open the deal at <a href="{html.escape(crm_url)}" '
                f'style="color:#1155cc;text-decoration:underline">{html.escape(crm_url)}</a></p></div>')
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
            _orig_send("simon@thesimonshow.com", "%d email send(s) FAILED" % len(_send_fails), _body, owner=True)
            print("failure-summary email sent.")
        except Exception as _ex:
            print("failure-summary email ALSO failed:", _ex)
        try:                                                # out-of-band: also ping Simon's phone (roadmap #5)
            import join
            join.push("CRM emails FAILED", "%d email send(s) failed in the last sweep" % len(_send_fails), c)
        except Exception:
            pass

    # ---- RECEIPT PDF PRE-GENERATION: for _depreceipt_* items pending approval that have no
    #      pdf_b64 yet, generate the PDF now and store it as base64 so approve-email.js can
    #      attach it without needing to run Python. ----
    _pdf_gen = 0
    for d in deals:
        st = d.get("cue_state") or {}
        if isinstance(st, str): st = json.loads(st or "{}")
        _changed = []                      # keys actually rewritten (was a bool)
        for key, e in list(st.items()):
            if not key.startswith("_depreceipt_"): continue
            if not e.get("pending_approval"): continue
            if e.get("sent") or e.get("cancelled"): continue
            if e.get("pdf_b64"): continue
            contact = CB.get(d.get("primary_contact_id")) or {}
            try:
                paid_in_full = (float(d.get("deposit_amount") or 0) > 0 and
                                float(d.get("deposit_amount") or 0) == float(d.get("amount") or 0))
            except Exception:
                paid_in_full = False
            try:
                import receipt as _rcpt
                # card_fee rides on the cue entry (app.js _receiptCueEntry) so this needs no extra
                # query per pending receipt. Absent -> None -> the PDF prints no fee line.
                fname, pdf_bytes, _mime = _rcpt.make_receipt(d, contact, paid_in_full=paid_in_full,
                                                             card_fee=e.get('card_fee'))
                st[key] = {**e, "pdf_b64": base64.b64encode(pdf_bytes).decode(), "pdf_filename": fname}
                _changed.append(key); _pdf_gen += 1
                print(f"  -> [receipt-pdf] generated {fname} for {key}")
            except Exception as _pdf_ex:
                print(f"  !! receipt PDF gen failed for {key}: {_pdf_ex}")
        if SEND and _changed:
            _save_cue(cur, d["id"], st, *_changed)
    if _pdf_gen: print(f"{TODAY}  -  {_pdf_gen} receipt PDF(s) pre-generated for pending-approval items.")

    # HEARTBEAT (Phase 2): record that the mailer ran, and cross-check the backup stamp so a dead
    # nightly backup is caught within a sweep (not just by the dedicated heartbeat cron). Best-effort.
    if SEND:
        try:
            heartbeat.stamp(cur, "mailer")
            # DISPATCH MONITOR (2026-07-18): stamp a SEPARATE key, but only when cron-job.org
            # triggered this run. cron-job.org is the authoritative scheduler (~30 dispatches/day);
            # mailer.yml's own cron is just a 5x/day backstop. Without this second stamp, cron-job.org
            # dying would silently degrade the mailer from 30 runs/day to 5 while the "mailer" stamp
            # stayed fresh - because the backstop keeps stamping it. GITHUB_EVENT_NAME is a default
            # Actions variable; locally it is unset, so this no-ops outside CI.
            if os.environ.get("GITHUB_EVENT_NAME") == "repository_dispatch":
                heartbeat.stamp(cur, "dispatch")
            heartbeat.check(cur, "dispatch")
            heartbeat.check(cur, "backup")
        except Exception as _hx:
            print("heartbeat (mailer) failed:", _hx)
    c.close()

if __name__=="__main__":
    main()
