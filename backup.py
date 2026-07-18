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
# pg_dump and pg_restore MUST come from the same major version, or verify() fails with
# "unsupported version (x.xx) in file header" (a PG17 dump can't be read by an older pg_restore).
# The bare names on a GitHub runner can resolve to different versions after an image roll, so pin
# both to the version we install (17). Falls back to PATH if that dir is absent (e.g. local Windows).
PG_BIN = _cfg("PG_BIN") or "/usr/lib/postgresql/17/bin"
def _pg(name):
    p = os.path.join(PG_BIN, name)
    return p if os.path.exists(p) else name
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
    run([_pg("pg_dump"), "-h", HOST, "-p", PORT, "-U", USER, "-d", "postgres",
         "--no-owner", "--no-privileges", "-Fc", "-f", DUMP], env=env)
    size = os.path.getsize(DUMP)
    if size < 10000:
        raise RuntimeError("dump suspiciously small (%d bytes) - aborting" % size)
    # 2) encrypt (symmetric AES256 with the passphrase)
    run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
         "--passphrase", GPG_PASS, "--symmetric", "--cipher-algo", "AES256",
         "-o", ENC, DUMP])
    enc_size = os.path.getsize(ENC)
    # 3) VERIFY the encrypted artifact is actually restorable BEFORE we keep it (a backup you can't
    #    restore is not a backup): decrypt it back + pg_restore --list. Raises if it looks empty/corrupt.
    verified = verify()
    # 4) upload as a GitHub Release asset (gh is preinstalled on runners; GH_TOKEN in env)
    run(["gh", "release", "create", TAG, ENC, "--repo", REPO,
         "--title", "DB backup %s" % STAMP,
         "--notes", "Encrypted pg_dump (custom format). %d bytes. Verified: %s." % (enc_size, verified)])
    # 4b) plain-CSV export of every public table (a human-readable snapshot), zipped + GPG-encrypted just like
    #     the dump, and attached to the SAME release. Decrypt + unzip to open the CSVs in a spreadsheet - no
    #     pg_restore / Postgres needed. Encrypted because it's client PII; never leave it plaintext on a release.
    csv_note = ""
    try:
        csv_enc = make_encrypted_csv()
        if csv_enc:
            run(["gh", "release", "upload", TAG, csv_enc, "--repo", REPO])
            csv_note = " CSV export attached (%s)." % csv_enc
            if RCLONE_REMOTE:
                try:
                    run(["rclone", "copy", csv_enc, RCLONE_REMOTE, "--no-traverse"])
                except Exception as _e:
                    csv_note += " (CSV Drive copy failed: %s)" % str(_e)[:120]
    except Exception as _e:
        csv_note = " (CSV export failed: %s)" % str(_e)[:150]
    # 5) optional Google Drive copy via rclone
    drive = ""
    if RCLONE_REMOTE:
        try:
            run(["rclone", "copy", ENC, RCLONE_REMOTE, "--no-traverse"])
            drive = " Also copied to Google Drive (%s)." % RCLONE_REMOTE
        except Exception as e:
            drive = " (Google Drive copy FAILED: %s)" % str(e)[:200]
    # 6) prune old releases
    pruned = prune_old()
    return "Verified restorable (%s). Encrypted backup %s uploaded (%d bytes).%s%s Pruned %d old backup(s)." % (verified, ENC, enc_size, drive, csv_note, pruned)

def make_encrypted_csv():
    """Export every PUBLIC table to CSV (secrets live in the private schema, not here), zip them, and
    GPG-encrypt the zip with the same passphrase as the dump. Returns the encrypted zip filename, or None
    if nothing exported. Best-effort per table - a table that fails to export is skipped, not fatal."""
    import zipfile
    env = dict(os.environ, PGPASSWORD=PW)
    try:
        out = run([_pg("psql"), "-h", HOST, "-p", PORT, "-U", USER, "-d", "postgres", "-Atc",
                   "select tablename from pg_tables where schemaname='public' order by tablename"], env=env)
    except Exception as e:
        print("  csv: could not list public tables:", str(e)[:150]); return None
    tables = [t.strip() for t in out.splitlines() if t.strip()]
    got = []
    for t in tables:
        fn = "%s.csv" % t
        try:
            run([_pg("psql"), "-h", HOST, "-p", PORT, "-U", USER, "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                 "-c", "\\copy (select * from public.\"%s\") to '%s' with (format csv, header true)" % (t, fn)], env=env)
            got.append((t, fn))
        except Exception as e:
            print("  csv skip %s: %s" % (t, str(e)[:120]))
    if not got:
        return None
    zipname = "crm-csv-%s.zip" % STAMP
    with zipfile.ZipFile(zipname, "w", zipfile.ZIP_DEFLATED) as z:
        for t, fn in got:
            z.write(fn, "%s.csv" % t)
    enc = zipname + ".gpg"
    run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase", GPG_PASS,
         "--symmetric", "--cipher-algo", "AES256", "-o", enc, zipname])
    print("  csv export: %d tables -> %s" % (len(got), enc))
    return enc

def verify():
    """Decrypt the just-made encrypted backup and pg_restore --list it, to prove it is a real,
    non-corrupt, restorable dump. Returns a short metric; raises if the table-of-contents looks empty."""
    dec = "verify-%s.dump" % STAMP
    run(["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
         "--passphrase", GPG_PASS, "--decrypt", "-o", dec, ENC])
    out = run([_pg("pg_restore"), "--list", dec])
    try:
        os.remove(dec)
    except Exception:
        pass
    entries = [l for l in out.splitlines() if l.strip() and not l.lstrip().startswith(";")]
    tabledata = sum(1 for l in entries if " TABLE DATA " in l)
    if len(entries) < 10 or tabledata < 1:
        raise RuntimeError("verify FAILED: dump index looks empty (%d objects, %d table-data)" % (len(entries), tabledata))
    return "%d objects, %d tables with data" % (len(entries), tabledata)

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
        if not ok:                                           # out-of-band phone push on backup failure (roadmap #5)
            try:
                import join
                if not join.push("CRM backup FAILED", (detail or "")[:150]):
                    try:                                     # env not set -> fall back to DB config
                        from db import connect
                        _c = connect(); join.push("CRM backup FAILED", (detail or "")[:150], _c); _c.close()
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as e:
        print("notify failed:", e)

def heartbeat_stamp():
    """Record a successful backup for the dead-man's switch, and cross-check the mailer stamp so a
    dead sweep is caught by the nightly backup too. Best-effort - never fails the backup."""
    try:
        import heartbeat
        from db import connect
        c = connect(); cur = c.cursor()
        heartbeat.stamp(cur, "backup")
        heartbeat.check(cur, "mailer")
        c.commit(); c.close()
        print("heartbeat: backup stamped + mailer checked")
    except Exception as e:
        print("heartbeat (backup) failed:", e)

SKIP_IF_NEWER_THAN_H = 20        # a successful backup this recently -> this run is a redundant backstop

def _hours_since_last_backup():
    """Hours since the last SUCCESSFUL backup (private.config.last_backup_run), or None if unknown.
    Best-effort: any failure returns None so the backup still runs - never skip on a broken check."""
    try:
        from db import connect
        c = connect(); cur = c.cursor()
        cur.execute("select value from private.config where key='last_backup_run'")
        row = cur.fetchone()
        c.close()
        if not row or not row[0]:
            return None
        dt = datetime.datetime.fromisoformat(row[0])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600.0
    except Exception as e:
        print("backup: couldn't read last_backup_run (%s) - proceeding anyway" % str(e)[:140])
        return None

if __name__ == "__main__":
    # DOUBLE-RUN GUARD (2026-07-18): the backup now has TWO schedules - a cron-job.org dispatch at
    # 3am ET (primary, because GitHub's own cron proved unreliable) and backup.yml's cron at 7am ET
    # (backstop). Without this gate both would fire daily: two full pg_dump + CSV exports (~16 MB of
    # egress instead of ~8) and two GitHub Releases. With it, the 7am backstop no-ops unless the 3am
    # dispatch actually failed to happen. Pass --force to override (manual runs / testing).
    if "--force" not in sys.argv:
        _age = _hours_since_last_backup()
        if _age is not None and _age < SKIP_IF_NEWER_THAN_H:
            print("backup: last successful backup was %.1fh ago (< %dh) - skipping this backstop run."
                  % (_age, SKIP_IF_NEWER_THAN_H))
            sys.exit(0)
    try:
        detail = do_backup()
        notify(True, detail)
        heartbeat_stamp()
        print("backup OK:", detail)
    except Exception:
        tb = traceback.format_exc()
        print(tb)
        notify(False, tb[-1500:])
        sys.exit(1)
