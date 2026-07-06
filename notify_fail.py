# -*- coding: utf-8 -*-
"""Failure alert. Called from a workflow's `if: failure()` step so Simon gets a clear email (to his
business inbox, with a link to the failed run) whenever a CRM automation job dies for ANY reason -
a crash, a bad dependency install, a DB blip. Env: FAIL_LABEL (what failed), RUN_URL (the run link).
Best-effort: never raises, so it can't add noise to an already-failed run."""
import os
try:
    import mailer
    label = os.environ.get("FAIL_LABEL", "a CRM mailer run")
    url = os.environ.get("RUN_URL", "")
    body = ('<div style="font-family:sans-serif;font-size:15px;color:#111">'
            '<p>&#10060; <b>%s FAILED.</b></p>'
            '<p><a href="%s">Open the failed run</a> to see which step broke.</p></div>' % (label, url))
    mailer.send_email("simon@thesimonshow.com", "[CRM] %s FAILED" % label, body, owner=True)
    print("failure alert sent for:", label)
except Exception as e:
    print("failure alert could NOT be sent:", e)
