# -*- coding: utf-8 -*-
"""Out-of-band alert: fire a Join (joaoapps) push to Simon's phone so a broken email pipe still reaches
him. Best-effort and silent - a push must NEVER crash or slow the caller.

Credential = the full Join base URL (apikey + deviceId, but NO title/text), the same value stored in
Supabase private.config key 'join_push_base'. The Vercel/SQL legs already push via that config row; the
Python legs (mailer sweep, heartbeat, backup, workflow-failure hook) reuse the same secret via either:
  1. the JOIN_PUSH_BASE env var (set as a GitHub Actions secret), or
  2. a live DB connection/cursor handed in -> read private.config.join_push_base.
If neither yields a real URL, push() no-ops (returns False). Callers append &title=&text= (url-encoded)."""
import os
import urllib.parse
import urllib.request


def _base(db=None):
    """Resolve the Join base URL. Env first (works with no DB), then an optional passed-in
    connection OR cursor (read private.config). Returns None if no real URL is available."""
    env = (os.environ.get("JOIN_PUSH_BASE") or "").strip()
    if env and "REPLACE" not in env:
        return env
    if db is not None:
        try:
            cur = db.cursor() if hasattr(db, "cursor") else db      # accept a connection OR a cursor
            cur.execute("select value from private.config where key='join_push_base'")
            row = cur.fetchone()
            if row and row[0] and "REPLACE" not in row[0]:
                return row[0].strip()
        except Exception:
            pass
    return None


def push(title, text, db=None):
    """Send one Join push. Returns True on a 2xx, False otherwise. Never raises."""
    try:
        base = _base(db)
        if not base:
            return False
        url = (base
               + "&title=" + urllib.parse.quote(str(title)[:80])
               + "&text=" + urllib.parse.quote(str(text)[:180]))
        with urllib.request.urlopen(url, timeout=10) as r:
            return 200 <= getattr(r, "status", 200) < 300
    except Exception as e:
        try:
            print("join push failed:", e)
        except Exception:
            pass
        return False
