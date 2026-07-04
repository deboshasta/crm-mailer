# Cloud mailer setup (GitHub Actions)

Goal: run the CRM email sweep in the cloud so your PC no longer has to be on.
Everything in this folder is already cloud-ready. The steps below are the parts
only you can do (they need your GitHub account and your Supabase password).

Time needed: ~15 minutes. You do this once.

Safe mode stays ON the whole time - every email still routes to
simon@thesimonshow.com until you explicitly flip it off (last section).

---

## What the cloud runner does each time it wakes up

- Sends any client emails that are due today
- Sends any email you hit **Send now** on
- Sends your self gig check-ins and the trivia/photo "received" notices
- Runs your three self-nags (inquiry leads, likely-no, proposal-not-sent)

Every script is idempotent and guarded, so running it often never double-sends.

**Cadence** (set in `.github/workflows/mailer.yml`):
- ~9am-6pm ET: every 20 minutes
- ~6pm-9:30pm ET: every 30 minutes
- overnight: every 2 hours (backstop)

That is ~40 runs/day, each ~30-60 seconds. Free GitHub gives you 2,000
minutes/month; this uses roughly 700, so there is plenty of headroom.

---

## Step 1 - Create a private GitHub repo and push this folder

1. Go to https://github.com/new
2. Name it e.g. `crm-mailer`, set it to **Private**, click **Create repository**.
3. On your PC, open a terminal **in this folder**
   (`...\claude version\CRM\build\mailer`) and run:

   ```
   git init
   git add .
   git commit -m "CRM mailer"
   git branch -M main
   git remote add origin https://github.com/<your-username>/crm-mailer.git
   git push -u origin main
   ```

   The `.gitignore` here already keeps `.env` and caches out of the repo, so no
   secrets get pushed. (SMTP login lives in your database, not in any file.)

---

## Step 2 - Get your Supabase pooler connection values

GitHub's runners can only reach IPv4, so we use Supabase's IPv4 **pooler**
(not the direct `db.<ref>.supabase.co` host you use locally).

1. In Supabase, open your project -> **Project Settings** (gear) -> **Database**.
2. Find **Connection pooling** (Transaction mode). Note these three values:
   - **Host** - looks like `aws-0-us-east-1.pooler.supabase.com`
   - **Port** - `6543`
   - **User** - looks like `postgres.tkethdmsaadhpjjjhxcj`

   (Your password is the same database password you already use.)

---

## Step 3 - Add the secret + variables to GitHub

In your repo: **Settings** -> **Secrets and variables** -> **Actions**.

Under the **Secrets** tab, click **New repository secret**:
| Name          | Value                          |
|---------------|--------------------------------|
| `DB_PASSWORD` | your Supabase database password |

Under the **Variables** tab, click **New repository variable** (x4):
| Name           | Value                                             |
|----------------|---------------------------------------------------|
| `SUPABASE_URL` | `https://tkethdmsaadhpjjjhxcj.supabase.co`        |
| `DB_HOST`      | the pooler host from Step 2                        |
| `DB_USER`      | the pooler user from Step 2 (`postgres.<ref>`)    |
| `DB_PORT`      | `6543`                                             |

Only `DB_PASSWORD` is secret; the other four are fine as plain variables.

---

## Step 4 - Test it by hand

1. In your repo, open the **Actions** tab. If it asks, click
   **"I understand my workflows, enable them"**.
2. Click **CRM mailer** in the left list -> **Run workflow** -> **Run workflow**.
3. Watch it run (~1 min). Open the run -> the `sweep` job -> expand the steps.
   You should see lines like `... email(s) due today (mode: SEND)` and
   `... send-now email(s)`. Any email it sends goes to simon@thesimonshow.com
   because safe mode is on.

If the DB step errors with a connection/auth message, re-check the pooler
values and the password in Step 2-3.

Once the manual run works, the schedule takes over automatically - nothing else
to start.

---

## Step 5 - Retire the three Windows scheduled tasks

Those three tasks (inquiry 7am, likely-no 8am, proposal every 4h) now run in the
cloud, so turn off the local copies to avoid duplicate nags. In PowerShell:

```
Get-ScheduledTask -TaskName "CRM *" | Disable-ScheduledTask
```

(or open Task Scheduler and disable the three "CRM ..." tasks). Leave them
disabled; delete them later once you're happy the cloud is handling it.

---

## Step 6 - Make "Send now" instant (~30-60s)

The **Send now** button in the app already works without this: it flags the
email and the next cloud sweep (within ~20 min during the day) sends it. This
step makes it fire in ~30-60s by having the database poke GitHub the instant an
email is flagged. The GitHub token lives in the database, never in the browser.

**6a. Make a GitHub token**
GitHub -> Settings -> Developer settings -> **Fine-grained tokens** ->
Generate new token. Repository access: **Only select repositories -> crm-mailer**.
Permissions -> Repository permissions -> set **Contents = Read and write** (this
is what the dispatch endpoint checks). Generate and copy the token.

**6b. Run this SQL** in Supabase (SQL Editor). Fill in your token and username:

```sql
create extension if not exists pg_net;

-- store the token as a DB setting (only admins can read it)
alter database postgres set app.github_token = 'YOUR_GITHUB_TOKEN';

-- when a deal gets an ACTIVE send_now flag, tell GitHub to run the mailer now
create or replace function public.fire_send_now()
returns trigger language plpgsql security definer as $$
declare has_now boolean;
begin
  select exists (
    select 1 from jsonb_each(coalesce(new.cue_state,'{}'::jsonb)) e
    where (e.value->>'send_now')::boolean is true
      and coalesce(e.value->>'sent','')     = ''
      and coalesce(e.value->>'cancelled','')= ''
  ) into has_now;
  if has_now then
    perform net.http_post(
      url := 'https://api.github.com/repos/YOUR_USERNAME/crm-mailer/dispatches',
      headers := jsonb_build_object(
        'Authorization',        'Bearer '||current_setting('app.github_token', true),
        'Accept',               'application/vnd.github+json',
        'X-GitHub-Api-Version', '2022-11-28',
        'Content-Type',         'application/json',
        'User-Agent',           'crm-mailer'
      ),
      body := jsonb_build_object('event_type','send-now')
    );
  end if;
  return new;
end $$;

drop trigger if exists trg_send_now on public.deals;
create trigger trg_send_now
  after update of cue_state on public.deals
  for each row
  when (old.cue_state is distinct from new.cue_state)
  execute function public.fire_send_now();
```

The workflow already listens for this (`repository_dispatch: [send-now]` in
`mailer.yml`). It fires only when a *new* send_now flag appears, and the mailer
clears the flag after sending, so there is no loop and no spam.

> Note (Claude): I wired the app's **Send now** button to open the draft with an
> **editable To field** and send for real through this pipeline (the old
> copy-paste is gone). Firing the closed-won confirmation *automatically* on
> stage change is a small follow-up I can add once you confirm you want it fully
> automatic (vs. you clicking Send now on it) - say the word.

---

## Step 7 - When you're ready to send for real

Turn safe mode off (routes emails to the actual clients instead of you):

```
UPDATE settings SET mail_safe_mode = false WHERE id = 1;
```

Do this only when you've watched a few cloud runs and you're happy. To pause
everything again (route everything back to yourself), set it to `true`.
