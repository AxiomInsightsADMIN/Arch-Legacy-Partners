# Runbook: API Key Rotation

Six credentials are in active use across the project. This runbook is
the authoritative source for **how each is generated, where it gets
installed, how to verify it works, and how to revoke the old version
during rotation.**

Six credentials to track:

| # | Credential | Used by |
|---|---|---|
| 1 | `ANTHROPIC_API_KEY` | Phase 4 Haiku enrichment + Phase 4.5 discovery extraction |
| 2 | `BRAVE_API_KEY` | Phase 4.5 discovery crawl (Brave Search API) |
| 3 | `SUPABASE_SERVICE_ROLE_KEY` | Supabase admin operations (currently env-set but not load-bearing in v1) |
| 4 | `SUPABASE_PUBLISHABLE_KEY` | Supabase anon read access (env-set, not load-bearing in v1) |
| 5 | `SUPABASE_DB_PASSWORD` | Every loader, resolver, geocoder backfill, drift detector, exporter (psycopg2 connection) |
| 6 | `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | Monthly refresh workflow failure alert |

Two install surfaces:

- **`.env`** at the project root (local development; gitignored). Loaded
  by `scrapers/_loader_utils.load_dotenv(ROOT / ".env")`. Operators
  copy `.env.example` to `.env` and fill in the values.
- **GitHub Actions secrets** at the repo's
  Settings → Secrets and variables → Actions surface. Loaded by the
  monthly cron workflow's `env:` block.

The DB password and the four SMTP secrets are the only ones that
must be set in both surfaces for end-to-end behavior. The
Anthropic / Brave keys only need to be in CI for the cron; if the
operator runs Phase 4 enrichment locally, they go in `.env` too.

This document supersedes the Phase 6 SMTP design pin in
`docs/build_log.md`. From here forward, this runbook is the source
of truth on SMTP setup.

---

## 1. Anthropic API key (`ANTHROPIC_API_KEY`)

**Format:** `sk-ant-api03-...` (alphanumeric + dashes; ~100 chars).
**Used by:** `enrichment/` (Phase 4 Haiku acceptance-flag enrichment;
the directory is empty in v1 but lands in Phase 4) and
`scrapers/discovery/` (Phase 4.5 extraction; not yet built).
**Cost basis:** pay-per-token at the published rate for the chosen
Haiku model.
**Default model used:** `claude-haiku-4-5-20251001` (latest Haiku;
the cheapest model that ships structured extraction acceptably).

### Generate

1. Open <https://console.anthropic.com/> and sign in.
2. **Settings → API Keys** in the left nav.
3. Click **Create Key**. Name it
   `arch-legacy-<owner-initials>-<YYYY-MM>` (e.g.
   `arch-legacy-rh-2026-05`) so the next rotation knows which key
   is which.
4. **Copy the key immediately** — Anthropic shows it once at
   creation and never again. Paste into a password manager
   (1Password / Bitwarden / Apple Keychain — anywhere except a chat
   transcript).

### Install

Two surfaces:

- **`.env` (local)**:
  ```
  ANTHROPIC_API_KEY=sk-ant-api03-...
  ```
- **GitHub Actions secrets (CI)**:
  - Settings → Secrets and variables → Actions → New repository secret
  - Name: `ANTHROPIC_API_KEY`
  - Value: paste the key
  - Click **Add secret**

### Verify

Run this from the project root (.venv activated):

```bash
.venv/Scripts/python.exe -c "
import os, anthropic
from pathlib import Path
for line in (Path.cwd() / '.env').read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

client = anthropic.Anthropic()
resp = client.messages.create(
    model='claude-haiku-4-5-20251001',
    max_tokens=20,
    messages=[{'role': 'user', 'content': 'Reply with the literal word OK.'}],
)
print('Response:', resp.content[0].text)
print('Usage:', resp.usage)
"
```

Expected: `Response: OK` plus a tiny usage block. If you get an
`anthropic.AuthenticationError`, the key is wrong or revoked.

### Revoke

1. Console → Settings → API Keys.
2. Find the **old** key (named with the prior month's date).
3. Click the **Delete** / **Revoke** action.
4. Anthropic blocks the revoked key immediately; any in-flight
   monthly cron will fail at the Haiku step on the next refresh
   until the new key is installed.

---

## 2. Brave Search API key (`BRAVE_API_KEY`)

**Format:** alphanumeric, ~30 chars (no specific prefix).
**Used by:** `enrichment/_brave.py` (Phase 4 acceptance-flag enrichment,
~1,970 queries per monthly refresh against typed canonicals) and
`scrapers/discovery/brave_search.py` (Phase 4.5 discovery crawl; budget
TBD, currently estimated at 500–2,000 queries per refresh depending on
discovery scope).
**Cost basis:** **paid tier required.** Brave's free tier (~1,000
queries/month) is exhausted within a single monthly refresh's Phase 4
enrichment pass alone (~1,970 queries against the v1 typed-canonical
set, plus Phase 4.5 discovery on top). The Phase 4 stop-4 calibration
on 2026-05-13 burned through the Axiom-side free tier mid-run and the
remainder of the pass returned `HTTP 402 Payment Required` until the
account was upgraded.
**Paid pricing model:** Brave Data for Search bills at ~$5 per 1,000
queries with prepaid top-up tiers at $5 / $10 / $15 increments (verify
current pricing at <https://api.search.brave.com/app/subscriptions>
before each rotation, as Brave revises tiers). At current v1 data
volume the monthly refresh budget is approximately **$5–15 per
refresh** (Phase 4 enrichment ~$10 + Phase 4.5 discovery $0–5
depending on scope).

**Austin handoff note.** Brave free tier is **not** sufficient for this
project's monthly cadence. Austin's day-1 Brave setup must subscribe to
the paid tier (any prepaid top-up amount works; $15 covers ~3 months of
refresh volume with headroom). Skipping this step will trip the same
HTTP 402 wall Axiom hit during v1 build, with partial-completion
results and an out-of-budget alert in the monthly-refresh runbook.

### Generate

1. Open <https://api.search.brave.com/> and sign in (or sign up).
2. **Account → API Keys** (or similar — Brave's dashboard layout
   evolves; the link is on the post-login landing page).
3. Click **Create API Key**. Label it the same way as Anthropic:
   `arch-legacy-<owner-initials>-<YYYY-MM>`.
4. Copy the key. Brave usually lets you view it later in the
   dashboard, but treat it as one-time-shown to be safe.

### Install

- **`.env` (local)**:
  ```
  BRAVE_API_KEY=...
  ```
- **GitHub Actions secrets (CI)**: same surface as Anthropic; name
  `BRAVE_API_KEY`.

### Verify

```bash
.venv/Scripts/python.exe -c "
import os, requests
from pathlib import Path
for line in (Path.cwd() / '.env').read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

r = requests.get(
    'https://api.search.brave.com/res/v1/web/search',
    params={'q': 'septage hauler texas', 'count': 1},
    headers={
        'X-Subscription-Token': os.environ['BRAVE_API_KEY'],
        'Accept': 'application/json',
    },
    timeout=10,
)
print('Status:', r.status_code)
print('First result title:', r.json().get('web', {}).get('results', [{}])[0].get('title'))
"
```

Expected: status 200 and a real search-result title. Status 401 →
key invalid. Status 429 → rate limit (typically retry after a
second).

### Revoke

1. Brave dashboard → API Keys.
2. Locate the old key by label.
3. **Delete** action.
4. Brave invalidates the key immediately; Phase 4.5 discovery
   stops working until the new key is installed.

---

## 3. Supabase service role key (`SUPABASE_SERVICE_ROLE_KEY`)

**Format:** JWT (starts with `eyJ...`; ~200 chars).
**Used by:** the workflow `env:` block exports it but no v1 module
loads it actively. It's present for forward compatibility (Supabase
JS SDK admin operations, server-side RLS bypass).
**Permission:** full admin access to the Supabase project — treat
as the most-sensitive of the six credentials.

### Generate (rotate)

Supabase doesn't let you arbitrarily generate new service role keys
— there's exactly one, and **regenerating it auto-invalidates the
old one**. The implicit behavior matters: any out-of-band consumer
holding the old key gets a 401 the moment you click regenerate.

1. Open <https://supabase.com/dashboard> and select the Arch Legacy
   project.
2. **Project Settings → API** (gear icon, sidebar).
3. Scroll to **Project API keys → `service_role`**. Click the
   small "Reveal" eye icon to see the current key (or **Reset**
   to regenerate).
4. Click **Reset service_role key**. Confirm.
5. Copy the **new** key immediately — Supabase shows it once and
   you'd have to reset again to see it.

### Install

- **`.env` (local)**:
  ```
  SUPABASE_SERVICE_ROLE_KEY=eyJ...
  ```
- **GitHub Actions secrets (CI)**: name `SUPABASE_SERVICE_ROLE_KEY`.

### Verify

For v1, no code path actively exercises the service-role key, so the
canonical verification is "does Supabase Table Editor still load
with this key as the admin auth?" — which is implicit when you
reset it from the dashboard.

For a stricter check, use the Supabase Python SDK (which IS in
`requirements.txt`):

```bash
.venv/Scripts/python.exe -c "
import os
from pathlib import Path
for line in (Path.cwd() / '.env').read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

from supabase import create_client
sb = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'])
resp = sb.table('source').select('slug').limit(3).execute()
print('rows:', resp.data)
"
```

Expected: a list of 3 source slugs. `401 Invalid JWT` → key is
wrong.

### Revoke

Revocation IS the regenerate action — there's no separate revoke
button. The old key is dead the moment **Reset service_role key**
completes.

---

## 4. Supabase publishable (anon) key (`SUPABASE_PUBLISHABLE_KEY`)

**Format:** JWT (starts with `eyJ...`; ~200 chars).
**Used by:** read-only client surface gated by Row-Level Security
(RLS). v1 has no public-read surface; this key is env-set for
forward compatibility (e.g., if a public-read web frontend ever
needs to fetch CSVs).

### Generate (rotate)

Same dashboard surface as the service-role key, same auto-invalidate
behavior on regenerate.

1. Project Settings → API.
2. **Project API keys → `anon` / `public`**. Reset.
3. Copy the new key.

### Install

- **`.env` (local)**:
  ```
  SUPABASE_PUBLISHABLE_KEY=eyJ...
  ```
- **GitHub Actions secrets (CI)**: name `SUPABASE_PUBLISHABLE_KEY`.

### Verify

```bash
.venv/Scripts/python.exe -c "
import os
from pathlib import Path
for line in (Path.cwd() / '.env').read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

from supabase import create_client
sb = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_PUBLISHABLE_KEY'])
# A read of a public-readable view. v_all_in_scope is the lowest-friction.
resp = sb.table('v_all_in_scope').select('id').limit(3).execute()
print('rows:', len(resp.data))
"
```

Expected: 3 rows. The anon key respects RLS — if no RLS policy
exposes `canonical_facility` or `v_all_in_scope`, you'll get an
empty list. That's not a key failure; that's RLS doing its job.

### Revoke

Same as service-role: regenerate = revoke.

---

## 5. Supabase database password (`SUPABASE_DB_PASSWORD`)

**Format:** alphanumeric string set at project creation, rotatable.
**Used by:** every loader, resolver, geocoder backfill, drift
detector, exporter — anything that calls
`scrapers/_loader_utils.db_connect()` and connects via psycopg2.
**Connection target:** the Tokyo pooler host
`aws-1-ap-northeast-1.pooler.supabase.com` (NOT the direct
host — that DNS no longer resolves; documented in
`docs/build_log.md` from the Phase 1 Day 2 finding). User format is
`postgres.<project-ref>`.

This is the most load-bearing credential in v1.

### Generate (rotate)

1. Supabase dashboard → Project Settings → **Database**.
2. **Database Password** section. Click **Reset password**.
3. Supabase shows you a new password once. Copy it.
4. Note: the project's connection string updates everywhere
   automatically (the pooler host is constant; only the password
   changes).

### Install

- **`.env` (local)**:
  ```
  SUPABASE_DB_PASSWORD=<new-password>
  ```
  Other DB vars stay the same:
  ```
  SUPABASE_DB_HOST=aws-1-ap-northeast-1.pooler.supabase.com
  SUPABASE_DB_PORT=5432
  SUPABASE_DB_NAME=postgres
  SUPABASE_DB_USER=postgres.<project-ref>
  ```
- **GitHub Actions secrets (CI)**: name `SUPABASE_DB_PASSWORD`. The
  other DB vars are also in secrets (`SUPABASE_DB_HOST`,
  `SUPABASE_DB_PORT`, `SUPABASE_DB_NAME`, `SUPABASE_DB_USER`).

### Verify

```bash
.venv/Scripts/python.exe -c "
import os, psycopg2
from pathlib import Path
for line in (Path.cwd() / '.env').read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

conn = psycopg2.connect(
    host=os.environ['SUPABASE_DB_HOST'],
    port=int(os.environ['SUPABASE_DB_PORT']),
    user=os.environ['SUPABASE_DB_USER'],
    password=os.environ['SUPABASE_DB_PASSWORD'],
    dbname=os.environ['SUPABASE_DB_NAME'],
    sslmode='require',
    connect_timeout=15,
)
cur = conn.cursor()
cur.execute('SELECT COUNT(*) FROM source')
print('source rows:', cur.fetchone()[0])
cur.close(); conn.close()
"
```

Expected: `source rows: 16` (or whatever count is current). Wrong
password → `psycopg2.OperationalError: FATAL: password
authentication failed`.

**Pooler-host pitfall.** If the connection fails with
`could not translate host name`, you're using the direct host
(`db.<project-ref>.supabase.co`) which no longer resolves. Switch
to `aws-1-ap-northeast-1.pooler.supabase.com` per the Phase 1 Day
2 finding pinned in `docs/build_log.md` (and in memory at
`memory/project_supabase_pooler.md`).

### Revoke

Same as the JWT keys: regenerate = revoke. The old password is
dead the moment **Reset password** completes.

After rotation, run a small load-test to confirm nothing is still
running with the old password (rare; only if a long-lived process
is hanging on stale credentials):

```bash
.venv/Scripts/python.exe -m orchestration.drift_detector
```

Should exit 0 if everything is healthy.

---

## 6. SMTP credentials (`SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD`)

**Used by:** the monthly refresh workflow's email-alert-on-failure
step. Only consumed in CI; **NOT used in `.env` locally** because the
monthly refresh runs in GitHub Actions, not on the operator
workstation.

### Provider choice: cheapest path is Gmail with an app password

Recommended for v1: **Gmail SMTP via an app password.**

- Free; works out of the box with `dawidd6/action-send-mail@v3`
  (the action the workflow uses).
- 5-minute setup.
- 500 sends per day cap (Google's free SMTP limit) is ~500× the
  monthly cron's ~1 alert per month.
- Deliverability is good against typical corporate inboxes (Gmail
  is a known-good sender on most spam filters).

### Generate (Gmail SMTP app password)

1. Pick the Gmail account that will send alerts. Recommended: a
   dedicated account like `arch-legacy-monthly-refresh@<your-domain>`
   so a future rotation doesn't lock out personal email. If you don't
   have a domain, a free Gmail address like
   `archlegacymonthly@gmail.com` works.

2. Enable **2-Step Verification** on that Google account:
   <https://myaccount.google.com/security>. App passwords require 2FA.

3. Generate an app password at <https://myaccount.google.com/apppasswords>:
   - App name: `arch-legacy-monthly-refresh`
   - Click **Generate**.
   - Copy the 16-character app password (Google shows it once).

4. Configuration values:
   - `SMTP_HOST=smtp.gmail.com`
   - `SMTP_PORT=465` (SSL — implicit TLS)
   - `SMTP_USER=<the Gmail address>`
   - `SMTP_PASSWORD=<the 16-char app password>`
   - `ALERT_EMAIL=<where the alerts go — operator's monitoring inbox>`

### Paid alternatives (higher deliverability)

If the client requires SPF/DKIM/DMARC alignment with a vanity domain
(e.g. alerts come from `monthly-refresh@axiominsights.example` and
must pass strict spam checks at the client's inbox):

| Provider | Pricing | Config |
|---|---|---|
| **Postmark** | $10/mo transactional plan + $1.25/k after | `SMTP_HOST=smtp.postmarkapp.com`, port 587, STARTTLS |
| **SendGrid** | Free tier 100/day; Essentials $19.95/mo | `SMTP_HOST=smtp.sendgrid.net`, port 587, STARTTLS |

Both drop in with the same four secrets — the workflow YAML is
provider-agnostic. Choice is operator-domain (vanity-domain
alignment) and budget, not technical.

### Install

GitHub Actions secrets ONLY (no `.env`):

- Settings → Secrets and variables → Actions → New repository secret
- Four secrets to add:
  - `SMTP_HOST`
  - `SMTP_PORT`
  - `SMTP_USER`
  - `SMTP_PASSWORD`
- `ALERT_EMAIL` is also in this surface (already set during v1
  setup; verify it points to the operator's monitoring inbox).

### Verify

The email alert step only fires on workflow failure. To test:

1. Trigger a `workflow_dispatch` run of the monthly refresh.
2. Watch step 1 (checkout) — it should succeed.
3. To force a failure on a controlled step, temporarily prepend an
   invalid env var to step 6 (EPA ECHO scraper), e.g. modify the
   workflow YAML temporarily to `run: false || python -m
   scrapers.federal.epa_echo`. The step fails.
4. The failure cascade reaches step 21 (email alert).
5. Check the `ALERT_EMAIL` inbox within 60 seconds — an email
   should arrive with subject
   `[Arch Legacy] Monthly refresh FAILED: <YYYY-MM-DD>`.
6. Revert the workflow YAML edit.

If no email arrives:

- Check the workflow log for step 21. The `dawidd6/action-send-mail@v3`
  step prints diagnostic info on failure.
- Common failures: wrong SMTP_PORT (465 vs 587), wrong app password
  (Gmail rejects with `Username and Password not accepted`), missing
  2FA on the Gmail account (Gmail rejects with `Application-specific
  password required`).

### Revoke

- **Gmail app password**: Google Account → Security → 2-Step
  Verification → App passwords → click the trash icon next to the
  app password row labeled `arch-legacy-monthly-refresh`.
- **Postmark**: API Tokens page → revoke the token.
- **SendGrid**: API Keys page → delete the key.

After revoking, install the new credential before the next monthly
refresh cycle (1st of month). If you miss the window, the workflow
fires but the alert step short-circuits (the `if:` condition checks
that SMTP_HOST and ALERT_EMAIL are non-empty) — failures still
surface in the GitHub Actions log, just not via email.

---

## 7. Austin handoff sequence

When the Arch Legacy project transfers from Axiom Insights to
Austin's team, all six credentials get re-issued under accounts
Austin owns. The sequence ensures no production gap.

### Pre-transfer (Ryan, ~1 day before)

- [ ] Note current credential expiration / rotation dates for all six.
- [ ] Confirm Austin has accounts at: Anthropic, Brave Search,
      Supabase (project ownership will transfer), an SMTP provider.
- [ ] Schedule the transfer window (typically a weekday morning).

### Day-of transfer

**Step A — Austin generates his own six credentials**

In any order, but typically grouped by provider for efficiency:

- [ ] Anthropic console → API Keys → Create Key (§1)
- [ ] Brave dashboard → API Keys → Create (§2)
- [ ] Supabase project (after ownership transfer below) →
      Project Settings → API → Reset service_role + anon keys (§3, §4)
- [ ] Supabase project → Project Settings → Database → Reset
      password (§5)
- [ ] SMTP provider account → generate app password / API key (§6)

**Step B — Supabase project ownership transfer**

This step is unique to Supabase because the credentials are tied to
the project, not to the issuing account. Both sides need to be
online during this:

1. Ryan: Supabase dashboard → Project Settings → General →
   Transfer ownership. Enter Austin's email.
2. Austin: accepts the transfer invitation in his inbox.
3. Project now belongs to Austin's organization. The DB password,
   service role, and anon keys can now be rotated by him (and only
   him).

**Step C — Austin installs the six credentials in his GitHub Actions secrets**

After Ryan transfers the repo (Settings → General → Transfer
ownership, similar one-step transfer for GitHub):

- [ ] Settings → Secrets and variables → Actions → add the new
      values for all six secrets (and the four config values
      `SUPABASE_DB_HOST` / `SUPABASE_DB_PORT` / `SUPABASE_DB_NAME` /
      `SUPABASE_DB_USER` / `SUPABASE_URL` / `ALERT_EMAIL`).

**Step D — Austin runs a test monthly refresh**

- [ ] `gh workflow run "Monthly Refresh"` (or the **Run workflow**
      button on the Actions tab).
- [ ] Wait for the run to complete (~12–18 min on the green path,
      see `docs/runbook_monthly_refresh.md`).
- [ ] Confirm the refresh branch `refresh/<YYYY-MM-DD>` appears
      and the PR opens against `main`.
- [ ] CI on the PR is green.
- [ ] `exports/drift_report.json` `overall_status: pass`.

If the dispatch fails at the NC scraper gate (§5 of the monthly
refresh runbook), that's expected — manual-drop the NC XLSX files
first, then re-dispatch.

**Step E — Anthropic + Brave smoke test (optional but recommended)**

These keys aren't exercised by the monthly refresh, so test them
separately:

- [ ] Run the verification snippet in §1 above (Anthropic Haiku).
- [ ] Run the verification snippet in §2 above (Brave Search).

Both should return 200 / OK.

**Step F — Ryan revokes the Axiom-side keys**

Once Austin's test dispatch lands clean, Ryan revokes the old
credentials with timestamp logging. Recommended log location: a new
section at the bottom of this file.

| Credential | Old value (last-4 chars only) | Revoked at (UTC) | Revoked by |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | `...xxxx` | `<timestamp>` | Ryan |
| `BRAVE_API_KEY` | `...xxxx` | `<timestamp>` | Ryan |
| `SUPABASE_SERVICE_ROLE_KEY` | `...xxxx` (auto-invalidated by Austin's reset) | `<timestamp>` | Austin (implicit at reset time) |
| `SUPABASE_PUBLISHABLE_KEY` | `...xxxx` (auto-invalidated) | `<timestamp>` | Austin (implicit) |
| `SUPABASE_DB_PASSWORD` | `n/a` (auto-invalidated) | `<timestamp>` | Austin (implicit at reset time) |
| `SMTP_PASSWORD` (Gmail app pw) | `...xxxx` | `<timestamp>` | Ryan (revoked in Google account he controlled) |

Anthropic, Brave, and the SMTP provider are the only revoke actions
Ryan executes — the three Supabase credentials are auto-invalidated
by Austin's reset actions in step A. Document the timestamps so a
forensic auditor can later see when each Axiom-side credential lost
access.

### Post-transfer verification

- [ ] One full monthly refresh runs end-to-end under Austin's
      ownership (the §D test counts; or wait for the natural
      1st-of-month cron).
- [ ] No 401 / 403 / authentication errors anywhere in the workflow
      log.
- [ ] No bounce emails on the ALERT_EMAIL inbox indicating SMTP
      misconfiguration.

---

## 8. Rotation cadence (after handoff)

**Recommended cadence: every 6–12 months for all six credentials,
rotated together.**

Why "together":

- Partial rotation drifts the credential set — three out of six
  rotated means the next rotation has to track which ones are
  young and which are old.
- All six get used by the same workflow, so a single test dispatch
  exercises four of them at once (DB password, service role,
  publishable key, SMTP). The other two (Anthropic, Brave) take 30
  seconds each to smoke-test.
- A single rotation event is easier to log, audit, and add to a
  calendar.

**Calendar setup recommended:** create a recurring 6-month event
titled "Rotate Arch Legacy credentials" on the operator's calendar.
First occurrence ~6 months after the handoff completes.

**Quarterly forced rotations** (more frequent than 6 months) are
optional — typical reasons:

- **Anthropic / Brave model upgrade** — when Anthropic publishes a
  new Haiku model that's cheaper or better, swap the model ID in
  the loader code and rotate the API key at the same time for
  hygiene.
- **Compliance / audit requirement** — if Austin's organization has
  a SOC 2 / ISO 27001 cadence that says 90-day rotation, follow
  that schedule.

**Emergency rotations** (immediate, not scheduled):

- Credential leak in a git commit (rare; pre-commit hooks catch
  most).
- Credential pasted into a chat tool / Slack DM / Notion page that
  later turns out to be more permissive than expected.
- A team member with credential access leaves the organization.

For emergency rotation, the order matters: install the new
credential everywhere first, verify the system still works, THEN
revoke the old credential. The reverse order causes a brief
production outage.

---

## 9. Cross-references

- `docs/runbook_monthly_refresh.md` §4 (email alert interpretation)
  — operators who hit a credential-related failure during
  diagnosis land here for the fix procedure.
- `docs/build_log.md` Phase 6 SMTP design pin — this runbook
  supersedes that pin going forward; the build_log entry remains as
  historical context.
- `memory/project_supabase_pooler.md` — locked pooler-host
  correction referenced in §5.
- `scrapers/_loader_utils.db_connect()` — the actual code path that
  consumes `SUPABASE_DB_PASSWORD` + the four DB config values.

---

## Rotation log

| Date (UTC) | Credential(s) rotated | Operator | Notes |
|---|---|---|---|
| 2026-05-12 | initial v1 install | Ryan (Axiom Insights) | First operational set; Phase 6 documentation drafted |

Append rows to this table on every rotation. Keep at least 24
months of history for audit traceability.
