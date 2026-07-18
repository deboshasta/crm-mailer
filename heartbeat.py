# -*- coding: utf-8 -*-
"""Dead-man's switch / heartbeat  (Failure alerting - Phase 2).

The worst automation failure is the job NOT RUNNING AT ALL: GitHub can disable scheduled
workflows after 60 days of repo inactivity, and cron/quota hiccups happen. When that occurs NO
failure email fires, because nothing ran - the classic silent failure. notify_fail.py (Phase 1)
only helps when a job runs and crashes; it can't help when a job never starts.

Fix: every successful run stamps private.config -
    last_mailer_run   (send_due.py, every sweep)
    last_backup_run   (backup.py, nightly)
and an INDEPENDENT check compares each stamp to now and emails Simon if it is older than expected.

Three legs check the stamps, so one broken workflow can't hide a failure:
  1) the mailer sweep (every ~20 min) checks the BACKUP stamp   -> a dead backup is caught fast
  2) the nightly backup run checks the MAILER stamp             -> a dead mailer is caught nightly
  3) a dedicated heartbeat cron (heartbeat.yml, every few hours) checks BOTH -> faster + independent
Alerts are de-duped to at most once/~day per subsystem, and a one-time "recovered" note is sent
when a stale subsystem starts running again.

BLIND SPOT (needs Phase 2b): all three legs are GitHub crons, so a TOTAL simultaneous GitHub-cron
blackout silences the heartbeat too. Only an EXTERNAL monitor (e.g. healthchecks.io pinged by each
run) catches that - it requires a free account + a ping URL from Simon, so it is a separate step.

Usage:
    python heartbeat.py --check          # check all subsystems, alert on staleness (the cron)
    heartbeat.stamp(cur, "mailer")       # imported by send_due.py / backup.py to record a run
    heartbeat.check(cur, "backup")       # imported for cross-monitoring
Env: same DB vars as the mailer (via db.connect); GITHUB_REPOSITORY optional (for the Actions link).
"""
import os, sys, datetime

# subsystem -> (max age in hours before it counts as "stopped", human label)
MONITORS = {
    "mailer": (6,  "email sweep (send_due.py)"),   # any trigger (dispatch OR backstop cron) refreshes this
    "backup": (30, "nightly database backup"),      # runs once a day; 30h leaves ~6h of slack
    # Stamped ONLY by cron-job.org-triggered runs (see send_due.py). cron-job.org is the authoritative
    # scheduler at */30 across 9am-9pm ET plus 1am/5am keep-alives, so the largest legitimate gap
    # between DISPATCHED runs is 3.5h (9:30pm -> 1am). 5h gives margin without false alarms.
    # Point: "mailer" alone cannot detect a cron-job.org outage, because mailer.yml's 5x/day backstop
    # keeps that stamp fresh - the sweep silently drops 30/day -> 5/day and nothing complains.
    "dispatch": (5, "cron-job.org dispatch (external scheduler)"),
}
_REALERT_HOURS = 20                                 # re-nag at most ~once a day while a subsystem stays down

def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)

def _get(cur, key):
    cur.execute("select value from private.config where key=%s", (key,))
    r = cur.fetchone()
    return (r[0] if r else None) or None

def _set(cur, key, value):
    # update-then-insert so we don't depend on an ON CONFLICT target (the config table lives in the DB,
    # not in the tracked schema, so its exact constraints aren't guaranteed here).
    cur.execute("update private.config set value=%s where key=%s", (value, key))
    if getattr(cur, "rowcount", 0) in (0, None):
        cur.execute("insert into private.config(key, value) values(%s, %s)", (key, value))

def _del(cur, key):
    cur.execute("delete from private.config where key=%s", (key,))

def _parse(iso):
    """Parse a stored stamp into an aware UTC datetime (treat a naive value as UTC)."""
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt

def stamp(cur, name):
    """Record that `name` ran successfully just now (called at the end of a good run)."""
    _set(cur, "last_%s_run" % name, _utcnow().isoformat())

def _actions_url():
    repo = os.environ.get("GITHUB_REPOSITORY") or "deboshasta/crm-mailer"
    return "https://github.com/%s/actions" % repo

_DEFAULT_REMEDY = ('<p>The automation may have quietly stopped - GitHub can disable scheduled workflows after '
                   '60 days of repo inactivity, or a cron/quota hiccup can skip runs. Nothing else would alert '
                   'you, because nothing ran.</p>'
                   '<p><a href="%s">Open the Actions tab</a> and check the <b>%s</b> workflow - re-enable it if '
                   'it was disabled, or run it by hand to confirm.</p>')

# Per-monitor "what to do about it". The dispatch monitor needs different advice from the rest:
# nothing is broken on GitHub's side, and email has NOT stopped - it has slowed to the backstop.
_REMEDY = {
    "dispatch": ('<p><b>Email is DELAYED, not stopped.</b> mailer.yml\'s own cron is still running the '
                 'sweep about 5x/day as a backstop, so nothing is lost - but the normal ~30x/day cadence '
                 'has stopped, which means a due email can now sit for up to 5 hours.</p>'
                 '<p>Check <a href="https://console.cron-job.org/jobs">cron-job.org</a>: the '
                 '<b>CRM Mailer Sweep</b> job may be disabled, may be failing against the GitHub API '
                 '(expired token?), or the service itself may be down. The GitHub Actions tab will look '
                 'perfectly healthy either way - the runs simply are not being triggered.</p>'),
}

def _alert_html(label, name, last_dt, age_h):
    when = last_dt.strftime("%b %d, %Y %H:%M UTC") if last_dt else "never"
    remedy = _REMEDY.get(name) or (_DEFAULT_REMEDY % (_actions_url(), name))
    return ('<div style="font-family:sans-serif;font-size:15px;color:#111">'
            '<p>&#128721; <b>%s may have STOPPED.</b></p>'
            '<p>No successful run has been recorded in <b>%.0f hours</b> (last: %s).</p>'
            '%s</div>' % (label, age_h, when, remedy))

def _recovered_html(label, last_dt):
    when = last_dt.strftime("%b %d, %Y %H:%M UTC") if last_dt else "just now"
    return ('<div style="font-family:sans-serif;font-size:15px;color:#111">'
            '<p>&#9989; <b>%s is running again.</b></p>'
            '<p>A successful run was recorded at %s. Earlier it looked stalled; it has recovered.</p></div>'
            % (label, when))

def _send(subject, html):
    import mailer
    mailer.send_email("simon@thesimonshow.com", subject, html, owner=True)

def check(cur, name, send=True):
    """Check one subsystem's stamp. Alert if stale (de-duped ~daily); send a recovery note when a
    previously-stale subsystem is fresh again. No-ops quietly if there is no stamp yet (fresh deploy)."""
    if name not in MONITORS:
        return
    max_h, label = MONITORS[name]
    run_iso = _get(cur, "last_%s_run" % name)
    if not run_iso:
        print("heartbeat: no stamp yet for %s (skipping - first run will set it)" % name)
        return
    last_dt = _parse(run_iso)
    if last_dt is None:
        print("heartbeat: unparseable stamp for %s: %r" % (name, run_iso))
        return
    age_h = (_utcnow() - last_dt).total_seconds() / 3600.0
    alert_key = "hb_alert_%s" % name
    prev_alert = _get(cur, alert_key)

    if age_h > max_h:                                   # STALE -> the job looks stopped
        if prev_alert:
            pa = _parse(prev_alert)
            if pa and (_utcnow() - pa).total_seconds() / 3600.0 < _REALERT_HOURS:
                print("heartbeat: %s still stale (%.1fh) - already alerted, holding" % (name, age_h))
                return
        print("heartbeat: %s STALE (%.1fh > %dh) -> alerting" % (name, age_h, max_h))
        if send:
            _send("[CRM] %s may have STOPPED" % label, _alert_html(label, name, last_dt, age_h))
            _set(cur, alert_key, _utcnow().isoformat())
            try:                                            # out-of-band phone push (roadmap #5)
                import join
                join.push("CRM: %s STOPPED" % label, "%s stale %.0fh - no run recorded" % (label, age_h), cur)
            except Exception:
                pass
    else:                                               # FRESH -> healthy
        if prev_alert:                                  # it had been alerted -> it just recovered
            print("heartbeat: %s recovered (%.1fh) -> clearing alert" % (name, age_h))
            if send:
                _send("[CRM] %s is running again" % label, _recovered_html(label, last_dt))
                _del(cur, alert_key)
        else:
            print("heartbeat: %s ok (%.1fh)" % (name, age_h))

def check_all(send=True):
    from db import connect
    c = connect(); c.autocommit = True; cur = c.cursor()
    try:
        for name in MONITORS:
            try:
                check(cur, name, send=send)
            except Exception as e:
                print("heartbeat check failed for %s: %s" % (name, e))
    finally:
        c.close()

if __name__ == "__main__":
    check_all(send=("--check" in sys.argv or "--send" in sys.argv))
