# -*- coding: utf-8 -*-
"""Nightly encrypted database backup for the CRM (runs in GitHub Actions).

Steps: pg_dump the whole Postgres DB (custom format = compressed + best for pg_restore) ->
GPG-encrypt it with a passphrase -> upload as a GitHub Release asset -> prune backups older
than KEEP_DAYS -> email Simon success or failure (via the existing mailer / Zoho SMTP).

Google Drive upload is wired separately (needs an OAuth token); if RCLONE_REMOTE is set it also
copies the encrypted file to that rclone remote.

Restore later:  gh release download backup-<stamp> --repo <repo>
                gpg --batch --passphrase '<pass>' -o crm.dump crm-backup-<stamp>.dump.gpg
                pg_restore --clean --if-exists -d '<target connection>' crm.dump

Env (provided by the workflow): SUPABASE_URL, DB_PASSWORD, DB_HOST, DB_USER, DB_PORT,
GPG_PASSPHRASE, GH_TOKEN, GITHUB_REPOSITORY, [BACKUP_KEEP_DAYS], [RCLONE_REMOTE].
"""
import os, sys, json, subprocess, datetime, traceback

def _cfg(k, default=""):
    return os.environ.get(k, default) or default

REF   = _cfg("SUPABASE_URL").split("//")[-1].split(".")[0]
PW    = _cfg("DB_PASSWORD")
HOST  = _cfg("DB_HOST") or ("db.%s.supabase.co" % REF)
USER  = _cfg("DB_USER") or "postgres"
# pg_dump needs a SESSION connection; Supabase's session pooler is on 5432 (transaction pooler = 6543)
PORT  = "5432"
GPG_PASS   = _cfg("GPG_PASSPHRASE")
REPO       = _cfg("GITHUB_REPOSITORY")
KEEP_DAYS  = int(_cfg("BACKUP_KEEP_DAYS") or "30")
RCLONE_REMOTE = _cfg("RCLONE_REMOTE")   # e.g. "gdrive:CRM-Backups" (optional; set once Drive is configured)

STAMP = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H%MZ")
DUMP  = "crm-backup-%s.dump" % STAMP
ENC   = DUMP + ".gpg"
TAG   = "backup-%s" % STAMP

def run(cmd, env=None, quiet=False):
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if p.returncode != 0:
        head = cmd[0] if cmd else "cmd"
        raise RuntimeError("%s failed: %s" % (head, (p.stderr or p.stdout).strip()[:900]))
    return p.stdout

def do_backup():
    if not (REF and PW and GPG_PASS and REPO):
        raise RuntimeError("missing one of SUPABASE_URL / DB_PASSWORD / GPG_PASSPHRASE / GITHUB_REPOSITORY")
    # 1) dump (custom format, no owner/privs so it restores cleanly into any DB)
    env = dict(os.environ, PGPASSWORD=PW)
    run(["pg_dump", "-h", HOST, "-p", PORT, "-U", USER, "-d", "postgres",
         "--no-owner", "--no-privileges", "-Fc", "-f", DUMP], env=env)
    size = os.path.getsize(DUMP)
    if size < 10000:
        raise RuntimeError("dump suspiciously small (%d bytes) - aborting" % size)
    # 2) encrypt (symmetric AES256 with the passphrase)
    run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
         "--passphrase", GPG_PASS, "--symmetric", "--cipher-algo", "AES256",
         "-o", ENC, DUMP])
    enc_size = os.path.getsize(ENC)
    # 3) upload as a GitHub Release asset (gh is preinstalled on runners; GH_TOKEN in env)
    run(["gh", "release", "create", TAG, ENC, "--repo", REPO,
         "--title", "DB backup %s" % STAMP,
         "--notes", "Encrypted pg_dump (custom format). %d bytes." % enc_size])
    # 4) optional Google Drive copy via rclone
    drive = ""
    if RCLONE_REMOTE:
        try:
            run(["rclone", "copy", ENC, RCLONE_REMOTE, "--no-traverse"])
            drive = " Also copied to Google Drive (%s)." % RCLONE_REMOTE
        except Exception as e:
            drive = " (Google Drive copy FAILED: %s)" % str(e)[:200]
    # 5) prune old releases
    pruned = prune_old()
    return "Encrypted backup %s uploaded (%d bytes).%s Pruned %d old backup(s)." % (ENC, enc_size, drive, pruned)

def prune_old():
    out = run(["gh", "release", "list", "--repo", REPO, "--limit", "300", "--json", "tagName,createdAt"])
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=KEEP_DAYS)
    n = 0
    for r in json.loads(out or "[]"):
        tag = r.get("tagName", "")
        if not tag.startswith("backup-"):
            continue
        try:
            dt = datetime.datetime.strptime((r.get("createdAt", "") or "")[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            continue
        if dt < cutoff:
            try:
                run(["gh", "release", "delete", tag, "--repo", REPO, "--yes", "--cleanup-tag"]); n += 1
            except Exception:
                pass
    return n

def notify(ok, detail):
    try:
        import mailer
        if ok:
            subj = "CRM backup OK - %s" % STAMP
            body = ("<div style=\"font-family:sans-serif;font-size:15px;color:#111\">"
                    "<p>&#9989; <b>Database backup succeeded.</b></p><p>%s</p>"
                    "<p style=\"color:#666;font-size:13px\">GitHub release: <b>%s</b> in %s</p></div>" % (detail, TAG, REPO))
        else:
            subj = "CRM backup FAILED - %s" % STAMP
            body = ("<div style=\"font-family:sans-serif;font-size:15px;color:#111\">"
                    "<p>&#10060; <b>Database backup FAILED.</b> Details below.</p>"
                    "<pre style=\"white-space:pre-wrap;font-size:12px\">%s</pre></div>" % detail)
        mailer.send_email("simon@thesimonshow.com", subj, body)
    except Exception as e:
        print("notify failed:", e)

if __name__ == "__main__":
    try:
        detail = do_backup()
        notify(True, detail)
        print("backup OK:", detail)
    except Exception:
        tb = traceback.format_exc()
        print(tb)
        notify(False, tb[-1500:])
        sys.exit(1)
