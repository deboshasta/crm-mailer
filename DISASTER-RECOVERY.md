# CRM Disaster Recovery Runbook

How to restore or rebuild the entire CRM (crm.thesimonshow.com) from scratch. The whole business lives
in one Supabase Postgres database; everything else (web app, serverless, mailer, forms) is code in GitHub
and can be redeployed. **Keep a copy of this file and the backup passphrase somewhere offline (password
manager), because if the machine or GitHub is unreachable you still need them.**

---

## 1. System map (what runs where)

| Component | What it is | Hosted on | Repo / dir |
|---|---|---|---|
| **Database** | Supabase Postgres 17 - ALL data, RPCs, triggers, RLS policies | Supabase project ref `tkethdmsaadhpjjjhxcj` | backups live in crm-mailer Releases |
| **CRM web app** | crm.thesimonshow.com - `index.html`, `app.js`, `sign.html`, `deposit.html`, `book.html` | Vercel (project "crm-app") | `deboshasta/crm-app`  dir `CRM/build/web` |
| **Serverless (send + pay)** | `send-now`, `signed-pdf`, `deposit-checkout`, `deposit-webhook` at crm-send-the-simon-show.vercel.app | Vercel (project "crm-send") | `deboshasta/crm-send`  dir `CRM/build/vercel-send` |
| **Scheduled mailer + backups** | `send_due.py` etc. + `backup.py` | GitHub Actions | `deboshasta/crm-mailer`  dir `CRM/build/mailer` |
| **Public website + form host** | thesimonshow.com (WordPress / OptimizePress). Embeds `book.html` via iframe; has the `/thanks` page | WordPress host | WP admin |
| **DNS** | crm.thesimonshow.com -> Vercel; thesimonshow.com -> WP host | IONOS | - |
| **Payments** | Stripe checkout + webhook | Stripe (Simon's account) | - |

---

## 2. Where every secret lives (VALUES are NOT in this file)

| Secret | Where it lives | Public? |
|---|---|---|
| Supabase URL + **anon/publishable** key | Hardcoded in the web HTML (`book.html`/`sign.html`/`deposit.html` `CFG`, and `window.CRM_CONFIG` in `index.html`) | Yes - safe, RLS-gated |
| Supabase **service_role** key | crm-send Vercel env `SUPABASE_SERVICE_KEY` + password manager | NO - never expose |
| DB password | GitHub secret `DB_PASSWORD` (crm-mailer) + password manager | NO |
| SMTP (Zoho) user/password | **Inside the DB** (`private.config`: `smtp_user`, `smtp_password`) - a DB restore brings them back | NO |
| reCAPTCHA **secret** | **Inside the DB** (`private.config`: `recaptcha_secret`). Site key is public in `book.html` | NO |
| Stripe secret + webhook secret | crm-send Vercel env `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` + Stripe dashboard | NO |
| **Backup GPG passphrase** | GitHub secret `BACKUP_GPG_PASSPHRASE` (crm-mailer) + password manager. **Required to decrypt any backup** | NO |
| `CRM_SEND_SECRET` | crm-send Vercel env + the DB send-now trigger (`fire_send_now`) | NO |
| rclone Google Drive token | GitHub secret `RCLONE_CONF` (crm-mailer) | NO |
| Logins: GitHub, Vercel, Supabase, IONOS, WordPress, Stripe, Zoho, Google | Password manager | NO |

---

## 3. Backups: what exists and how to read one

- **Nightly encrypted `pg_dump`** runs in GitHub Actions (crm-mailer, `.github/workflows/backup.yml` + `backup.py`), stored as **Release assets** in `deboshasta/crm-mailer` (tag `backup-<YYYY-MM-DD_HHMMZ>`), 30-day retention, plus a success/failure email. Each backup is auto-verified restorable (`pg_restore --list`).
- **To decrypt + inspect a backup:**
  ```
  gh release download backup-<stamp> --repo deboshasta/crm-mailer
  gpg --batch --pinentry-mode loopback --passphrase '<BACKUP_GPG_PASSPHRASE>' -o crm.dump crm-backup-<stamp>.dump.gpg
  pg_restore --list crm.dump          # sanity check the contents
  ```

---

## 4. Scenario A - bad data / accidental delete (database itself is fine)

1. Grab the most recent backup and decrypt it (section 3).
2. Restore over the current DB (this REPLACES data - be sure):
   ```
   pg_restore --clean --if-exists --no-owner \
     -d 'postgresql://postgres.<ref>:<DB_PASSWORD>@<session-pooler-host>:5432/postgres' crm.dump
   ```
   Use the **session pooler host on port 5432** (Supabase Dashboard -> Project Settings -> Database -> "Session pooler"). The transaction pooler (6543) will not work for a restore.
3. If only a few rows were lost, restore into a *scratch* database first, then copy just the affected rows back, rather than clobbering everything.

---

## 5. Scenario B - Supabase project lost (build a new database)

1. **Create a new Supabase project.** Note the new project ref, the new anon (publishable) key, and the new service_role key.
2. **Restore schema + data** from the latest backup into the new DB (`pg_restore --clean --if-exists --no-owner -d '<new session pooler conn>' crm.dump`). This brings back all tables, RPCs, triggers, RLS, AND the `private.config` rows (SMTP + reCAPTCHA secrets).
3. **Re-wire every place that points at the old project** (the new URL + keys are different):
   - **Web HTML** (crm-app repo): `index.html` `window.CRM_CONFIG`, and the `CFG` block in `book.html`, `sign.html`, `deposit.html` (URL + anon key). Commit (author `simon@thesimonshow.com`) and push -> Vercel redeploys.
   - **crm-send Vercel env**: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`.
   - **crm-mailer GitHub**: repo variables `SUPABASE_URL`, `DB_HOST`, `DB_USER`, `DB_PORT` (the new pooler host/user), and secret `DB_PASSWORD`.
   - **DB send-now trigger** (`fire_send_now`): it calls the crm-send URL with `CRM_SEND_SECRET`; those are baked into the function definition, which came back with the restore - confirm the URL + secret still match crm-send's env.
4. **Storage bucket** (intake photos) is NOT in a Postgres dump. Recreate the bucket if needed; historical photos are acceptable to lose (Simon's workflow is download-then-delete-per-deal).
5. Re-run one **backup** manually and confirm the OK email, so the new project is protected immediately.

---

## 6. Scenario C - Vercel / web app or serverless down

1. Both are just GitHub repos. In Vercel, re-import `deboshasta/crm-app` (root = `CRM/build/web`) and `deboshasta/crm-send` (root = `CRM/build/vercel-send`), or redeploy the existing projects.
2. **Re-add env vars** (they do not live in git):
   - crm-send needs: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SMTP_HOST/PORT/USER/PASS`, `CRM_SEND_SECRET`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`.
3. **Domain:** re-attach `crm.thesimonshow.com` to the crm-app project; Vercel shows the DNS record to set at IONOS (a CNAME/A). Confirm the record in IONOS.
4. **Deploy gotcha:** Vercel only auto-deploys crm-app/crm-send when the commit author is `simon@thesimonshow.com`. Any other author is silently ignored.

---

## 7. Scenario D - booking form broken on the website

1. The form is `crm.thesimonshow.com/book.html`, embedded on the WordPress `#contact` section via an iframe (Custom HTML block). Re-paste the embed snippet if it was lost:
   ```html
   <iframe id="simon-book" src="https://crm.thesimonshow.com/book.html?embed=1" title="Contact Simon"
     style="display:block;width:100%;border:0;margin:0;background:#0d0b06;min-height:400px" scrolling="no" loading="lazy"></iframe>
   <script>window.addEventListener('message',function(e){if(e.origin==='https://crm.thesimonshow.com'&&e.data&&e.data.simonBookHeight){var f=document.getElementById('simon-book');if(f)f.style.height=(e.data.simonBookHeight+2)+'px';}});</script>
   ```
2. **reCAPTCHA:** the site key is domain-locked to `crm.thesimonshow.com`. If submissions fail "could not verify," the key's allowed domains (Google reCAPTCHA admin) or the `recaptcha_secret` in `private.config` is wrong.
3. On success the form redirects the top window to `www.thesimonshow.com/thanks` (Google Ads conversion). That WordPress page must exist. The in-form card is only a fallback.
4. New submissions call the `submit_booking` RPC, which creates the inquiry deal + fires the new-lead email, the client acknowledgment, and the SMS (`7324926071@vtext.com`).

---

## 8. Scenario E - DNS (IONOS)

- `crm.thesimonshow.com` -> Vercel (the record Vercel gives you when you attach the domain).
- `thesimonshow.com` / `www` -> the WordPress host.
- Email (Zoho) MX/SPF/DKIM records must stay intact or outbound mail deliverability drops.

---

## 9. FULL rebuild from zero (everything gone) - ordered

1. **Supabase:** new project -> restore latest backup (section 5). This is the foundation; do it first.
2. **Re-wire keys/URLs** everywhere (section 5, step 3).
3. **Vercel:** redeploy crm-app + crm-send from GitHub, re-add env vars (section 6).
4. **DNS:** point crm.thesimonshow.com at Vercel (section 8).
5. **GitHub Actions (crm-mailer):** confirm repo vars/secrets, run the mailer + a backup manually.
6. **Stripe:** confirm the secret key + webhook (-> `.../api/deposit-webhook`, event `checkout.session.completed`) in crm-send env.
7. **WordPress:** re-embed the booking form, confirm `/thanks` exists (section 7).
8. **Smoke test:** submit a test booking, send a test email, run a test $1 Stripe deposit, take a manual backup.

---

## 10. Non-obvious gotchas (the things that bite you)

- **Safe mode:** `settings.mail_safe_mode = true` routes ALL client email to Simon. It is the go-live switch; keep it ON until the cutover.
- **Anon key is public** in the web HTML - that is fine (RLS = authenticated-only, sign-ups disabled). The service_role key must NEVER be in client code.
- **GitHub Actions is IPv4-only:** it must reach Supabase via the **pooler** host (not the direct `db.<ref>.supabase.co`, which is IPv6). `pg_dump`/`pg_restore` need the **session** pooler (port 5432); pg8000 (mailer) works on either.
- **Vercel author gate:** commits to crm-app/crm-send must be authored `simon@thesimonshow.com` or they do not deploy.
- **Backups are useless without the GPG passphrase** - it is the single most important thing to keep safe.
- **SMTP + reCAPTCHA secrets live in the DB** (`private.config`), so a DB restore recovers them - but the Supabase service/anon keys are per-project and must be re-copied on a new project.
- **Sign-ups are disabled** in Supabase Auth on purpose (shared-access RLS = any logged-in user sees all data). Do not re-enable without role-scoped RLS.
