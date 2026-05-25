# T-Mobile Bill → Zelle End-to-End Automation

Hands-off monthly handler for the T-Mobile bill on macOS:

- Detects the new bill via T-Mobile's "Your bill is ready" SMS (read from Messages chat.db)
- Logs into the T-Mobile portal, downloads the PDF (Playwright + persistent profile)
- Parses per-line charges and the special-pool amount the bill-payer owes (or is owed)
- Emails a styled HTML breakdown with the original PDF attached
- Pays the special-pool amount via Zelle from Bank of America (multi-step flow, Pay button, OTP auto-fill from Messages)
- Emails a payment confirmation
- Emails a failure alert + screenshots if any stage breaks

A single confirmed payment per month is enforced by an idempotent state file. Once `zelle_confirmed_at` is set, no automated re-payment is possible.

> **Status:** Production-tested. May 2026 bill ($207.38 total → $102.43 special pool) processed end-to-end with no manual intervention.

---

## Pipeline

```
[0] Detect T-Mobile bill SMS in Messages.app          (skip portal login if absent)
        ↓
[1] Log into T-Mobile (push 2FA, persistent profile)
[2] Read 'Bill posted MM/DD/YYYY' / Download PDF
[3] Parse per-line charges + computed totals
[4] Send styled bill summary email (with PDF)
[5] Safety gate: amount cap, recipient match, idempotency
[6] BoA login → Pay {recipient} → Amount → Date → Pay (live or dry-run)
[7] Confirmation email with transaction ID

Failure at any stage → failure alert email + debug screenshot
```

Each stage records its outcome to `~/.tmo_state/bill_<YYYY-MM>.json`. A re-run skips already-completed stages — safe to re-fire any number of times.

---

## Repository layout

| File | Role |
|---|---|
| `app.py` | Stage-aware orchestrator, PDF parser, top-level error handling |
| `download_bill.py` | T-Mobile login + bill-page navigation + PDF download (Playwright) |
| `zelle_pay.py` | BoA login + Zelle multi-step send + confirmation capture (Playwright) |
| `notify.py` | SMTP helpers — bill summary, confirmation, failure-alert, 2FA-prompt emails |
| `sms_utils.py` | macOS Messages chat.db reader — bill SMS detection + OTP auto-fill |
| `state.py` | Atomic JSON state file at `~/.tmo_state/bill_<YYYY-MM>.json` |
| `security_utils.py` | macOS Keychain helpers for sensitive credentials |
| `auto_process.sh` | Thin shell entrypoint invoked by the LaunchAgent |
| `com.example.tmobile_automation.plist` | LaunchAgent template (sed-substituted at install) |
| `tests/` | Unit + feature tests (state, notify, parser, sms_utils, security, safety gate, pipeline orchestration) |
| `pyproject.toml` | Pytest + coverage configuration (95% gate) |

---

## Setup

### 1. Python environment
```bash
python3 -m venv tmobile_env
./tmobile_env/bin/pip install -r requirements.txt
./tmobile_env/bin/playwright install chromium
```

### 2. Sensitive credentials in macOS Keychain

The code uses `$USER` as the Keychain account name by default (override with `KEYCHAIN_ACCOUNT` env var if needed):

```bash
security add-generic-password -s "TMobile_User"           -a "$USER" -w "your_tmo_user"
security add-generic-password -s "TMobile_Pass"           -a "$USER" -w "your_tmo_pass"
security add-generic-password -s "BoA_Username"           -a "$USER" -w "your_boa_id"
security add-generic-password -s "BoA_Password"           -a "$USER" -w "your_boa_pass"
security add-generic-password -s "ZELLE_RECIPIENT_NAME"   -a "$USER" -w "Recipient Name As Shown In BoA"
security add-generic-password -s "ZELLE_AMOUNT_CAP"       -a "$USER" -w "300"

# Optional — last 4 digits of phone for SMS-MFA selection on banks that
# offer multiple OTP-target options. Leave unset if BoA shows only one.
security add-generic-password -s "BoA_MFA_Phone_Last4"    -a "$USER" -w "1234"
```

`ZELLE_AMOUNT_CAP` is a hard upper bound on auto-sends. Bills exceeding it abort the pipeline and email a failure alert for manual review.

`ZELLE_RECIPIENT_NAME` must match the **exact text** Bank of America renders next to the contact in your Zelle list. The recipient finder uses an exact accessible-name match first (excludes "Edit X" / "Delete X" buttons that share the recipient's name).

### 3. Non-sensitive configuration in `.env`

```bash
cp .env.example .env
```

Then edit:

```env
# Gmail SMTP — use an App Password, not your account password.
# https://myaccount.google.com/apppasswords
SMTP_EMAIL=your_email@gmail.com
SMTP_PASSWORD=your_gmail_app_password

# Bill summary email recipients (comma-separated)
RECIPIENT_EMAILS=person1@example.com,person2@example.com

# Optional - narrow the audience for confirmation/alert emails
# CONFIRMATION_RECIPIENTS=you@example.com           # defaults to RECIPIENT_EMAILS
# FAILURE_ALERT_EMAIL=you@example.com               # defaults to first RECIPIENT_EMAILS
# MFA_ALERT_EMAIL=you@example.com                   # defaults to FAILURE_ALERT_EMAIL

# JSON map of phone-number-keys -> display names. Keys must match T-Mobile's
# PDF format (with parentheses around area code).
USER_MAPPING='{"(123)456-7890": "Alice", "(098)765-4321": "Bob"}'

# Names whose share is rolled into a 'special pool' that gets Zelle'd.
SPECIAL_POOL_NAMES=Alice
SPECIAL_POOL_TITLE=Premium Pool
SPECIAL_POOL_DESC=Coverage for Alice

# Live Zelle send is OFF by default. Set to 1 to enable real money transfer.
ZELLE_LIVE_SEND=0
```

`.env` is gitignored — never commit it.

### 4. macOS LaunchAgent (scheduled run)

The plist ships as a **template** — LaunchAgent plists don't expand `$HOME`, so the absolute path must be substituted at install time:

```bash
sed "s|__REPO_PATH__|$PWD|g" com.example.tmobile_automation.plist \
  > ~/Library/LaunchAgents/com.example.tmobile_automation.plist

launchctl load ~/Library/LaunchAgents/com.example.tmobile_automation.plist
launchctl list | grep tmobile   # verify registered
```

Schedule: 9:00 AM on days 6, 7, 8, 9, 10 of each month. Stage 0 makes most of those runs no-ops (returns early when no fresh SMS or already paid).

### 5. Sleep-aware wake (one-time)

`StartCalendarInterval` does **not** wake a sleeping Mac. Schedule a daily wake just before the agent fires:

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 08:55:00
pmset -g sched   # verify
```

### 6. Full Disk Access (one-time)

The Zelle automation reads bank OTP codes and the T-Mobile bill SMS directly from `~/Library/Messages/chat.db`. AppleScript-based Messages reads are unreliable on modern macOS, so direct sqlite is the only robust path. This **requires** Full Disk Access on the launching process:

| Use case | Grant FDA to |
|---|---|
| Manual run from Terminal / iTerm | **Terminal.app** (or your terminal of choice) |
| LaunchAgent-scheduled run | **`/sbin/launchd`** |

Open System Settings → Privacy & Security → Full Disk Access → `+`. For `launchd`, use `Cmd+Shift+G` in the picker and paste `/sbin/launchd`. Restart the terminal (or reboot for launchd) for the permission to take effect on new processes.

Without FDA, Stage 0 will report `chat.db PERMISSION DENIED` and exit thinking no SMS exists — the pipeline silently misses bills.

### 7. Persistent browser profile

Both T-Mobile and BoA cache "trust this device" cookies in `~/.tmo_browser_profile/`. **On the first run from each, tick the "Save this device" / "Trust this device" checkbox** if it appears — subsequent runs skip MFA entirely (typically ~30 days). The Zelle script also attempts to click the trust prompt automatically.

---

## Running manually

```bash
# Full E2E pipeline. Stage 0 gates on SMS presence; respects state file.
./tmobile_env/bin/python app.py

# Skip download stage by passing an existing PDF (also bypasses Stage 0)
./tmobile_env/bin/python app.py /path/to/SummaryBill_20260504.pdf

# Force re-run: bypass 'summary already emailed' and 'Zelle attempted' gates.
# zelle_confirmed_at gate is NEVER bypassed - delete state file to truly reset.
./tmobile_env/bin/python app.py --force

# Just download (no parse/email/Zelle)
./tmobile_env/bin/python download_bill.py

# Just Zelle subsystem (reads ZELLE_LIVE_SEND from .env)
./tmobile_env/bin/python zelle_pay.py 75.00
```

---

## $1 test workflow (highly recommended before going live)

Validate the BoA login → MFA → recipient → amount → date → review → Pay chain without touching the bill pipeline or sending real bill amounts:

```bash
# 1. Point the keychain entry at your OWN Zelle-registered account
security add-generic-password -U -s "ZELLE_RECIPIENT_NAME" -a "$USER" -w "Your Own Name"

# 2. Dry-run first - check zelle_review_dryrun.png shows correct amount + recipient
ZELLE_LIVE_SEND=0 ./tmobile_env/bin/python zelle_pay.py 1.00

# 3. Live $1 test - confirms Send + confirmation capture work
ZELLE_LIVE_SEND=1 ./tmobile_env/bin/python zelle_pay.py 1.00

# 4. After success, point the keychain back to the real recipient
security add-generic-password -U -s "ZELLE_RECIPIENT_NAME" -a "$USER" -w "Real Recipient Name"
```

---

## Going live with Zelle

The live Send click is gated by **`ZELLE_LIVE_SEND=1`**. With the flag off (default), the BoA flow logs in, navigates through Pay → Amount → Date → Review, takes a screenshot at the Review page, and stops without clicking Pay. Lets you validate the entire chain against real services without moving money.

Recommended cutover:
1. Run with `ZELLE_LIVE_SEND=0` against your previous month's PDF (manual `app.py /path/to/SummaryBill_*.pdf`). Confirm the bill summary email looks right and the dry-run review screenshot shows the correct amount + recipient.
2. After your $1 test confirms BoA automation works end-to-end, set `ZELLE_LIVE_SEND=1` in `.env`.
3. Watch the next scheduled run. Confirmation email arrives within ~5 minutes of the LaunchAgent firing.

---

## State file

Located at `~/.tmo_state/bill_<YYYY-MM>.json`. Stage progression:

| Field | Set when |
|---|---|
| `bill_sms_date`, `bill_sms_balance` | Stage 0 — T-Mobile bill SMS detected |
| `bill_posted_date` | Stage 1 — portal shows 'Bill posted MM/DD/YYYY' |
| `pdf_path`, `pdf_sha256` | Stage 2 — PDF downloaded |
| `parsed_total`, `special_amount` | Stage 3 — PDF parsed |
| `summary_emailed_at` | Stage 4 — bill summary email sent |
| `zelle_attempted_at` | Stage 6 — right before clicking Pay (live mode only) |
| `zelle_confirmed_at`, `zelle_confirmation_id`, `zelle_screenshot` | Stage 6 — confirmation page captured |

**Idempotency rules:**

- `zelle_confirmed_at` is the hard lock. Once set, even `--force` cannot trigger another payment for that month. The only way to bypass is `rm ~/.tmo_state/bill_<YYYY-MM>.json`.
- `zelle_attempted_at` set without `zelle_confirmed_at` (e.g. crash mid-flow) blocks future runs and sends an alert. Manual review required — verify in the BoA app whether the payment actually went through, then either delete state (didn't go through) or set `zelle_confirmed_at` manually (did go through).

---

## Tests

```bash
# Full suite + coverage gate (pytest config lives in pyproject.toml)
./tmobile_env/bin/python -m pytest

# Without the coverage gate, verbose
./tmobile_env/bin/python -m pytest -v --no-cov
```

Pytest and coverage are configured in `pyproject.toml`: a bare `pytest` runs every test, prints a `term-missing` coverage report, and **fails if total coverage drops below 95%** (currently ~97% on the gated modules). CI runs the same command on every push/PR to `main` (`.github/workflows/tests.yml`).

**Unit tests** (pure logic, no I/O):
- `_zelle_safety_gate` — the money-protection gate: amount cap (env/Keychain/default/invalid), zero/negative guard, recipient-required, and the two idempotency locks (`zelle_confirmed_at` is never bypassable, even by `--force`).
- `parse_bill` / parser helpers — page-shift resilience, month/abbreviation detection, plan-total extraction, plus malformed-input fallbacks (bad mapping JSON, non-numeric charges, unparseable lines, corrupt PDF).
- `_trigger_zelle` — parsing the JSON result emitted by `zelle_pay.py`, including noise/invalid-line skipping and non-zero-exit handling.
- `state` I/O (atomic writes, idempotency), `notify` (SMTP mocked, all email types + recipient routing), `sms_utils` (chat.db parsing against a real temp SQLite DB, OTP/bill regexes, attributedBody decoding, FDA error paths), `security_utils` (Keychain mocked), and `zelle_pay` / `download_bill` pure helpers (confirmation/review regexes, overlay-button matchers, content hashing, live-send flag).

**Feature tests** (`tests/test_app_pipeline.py`) drive the whole `app._run_pipeline` / `main` orchestration end-to-end with every dangerous boundary stubbed (no browser, SMTP, Keychain, chat.db, or subprocess): Stage-0 SMS gating, dry-run vs. live-send paths, idempotent refusal after a confirmed payment, amount-over-cap aborts, per-stage failure-alert routing, PDF reuse from state, and `--force` semantics — asserting both exit codes and the resulting state file.

Live Zelle/download browser flows stay out of the suite — unsafe to mock against real banking and unrealistic against real T-Mobile — so `zelle_pay.py` and `download_bill.py` are excluded from the coverage gate (their pure helpers are still tested).

---

## Troubleshooting

### "Stage 0: No T-Mobile 'bill is ready' SMS in last 14 days"
- Check the SMS exists in Messages.app (sender `2535`)
- Check FDA — `python -c "import sqlite3, os; sqlite3.connect('file:'+os.path.expanduser('~/Library/Messages/chat.db')+'?mode=ro', uri=True).cursor().execute('SELECT 1').fetchall()"` should not raise. If it does, FDA isn't on the python binary's launching process.
- macOS Big Sur+ stores some message bodies in the binary `attributedBody` blob with NULL `text` column — already handled by `sms_utils._decode_attributed_body`.

### "chat.db PERMISSION DENIED"
FDA isn't granted. See Setup §6.

### Cookie banner / notification modal blocks clicks
T-Mobile shows OneTrust + a "T-Mobile Notice" + a MoEngage "stay up to date with notifications" modal. `download_bill._dismiss_overlays` handles all three. The persistent profile remembers your choice after the first dismissal.

### Zelle recipient finder picks "Edit" instead of "Pay"
BoA renders `Edit Bilal Ahamad`, `Delete Bilal Ahamad`, and `Pay Bilal Ahamad` buttons all matching `:has-text("Bilal Ahamad")`. The finder uses exact accessible-name matching first, then falls back to filtering out edit/delete/manage keywords. If your BoA UI shows different button labels, adjust `EDIT_BUTTON_KEYWORDS` in `zelle_pay.py`.

### Bill amount in email is wrong
The parser computes the bill total as the sum of per-line charges + base account charge, not from a fragile "Totals" string in the PDF (T-Mobile bills have multiple sub-section "Totals" lines). The "Original Carrier Plan Bill" line uses `_extract_plan_total` which scans for `Plan ... $X.XX` patterns — falls back to computed total if absent.

### LaunchAgent fires but does nothing
Check `automation.log`. Common reasons:
- Mac was asleep at 9 AM (set `pmset repeat wakeorpoweron`)
- FDA not granted to `/sbin/launchd` → chat.db read fails → Stage 0 false-negative
- Old plist still loaded — `launchctl list | grep tmobile`; bootout obsolete entries

---

## Logs & artifacts

| File | Contents |
|---|---|
| `automation.log` | LaunchAgent stdout/stderr from every fire |
| `~/.tmo_state/bill_<YYYY-MM>.json` | Per-bill stage progression |
| `zelle_confirmation_*.png` | BoA confirmation page screenshot (saved locally, NOT emailed) |
| `zelle_review_dryrun.png` | Last dry-run Review screen capture |
| `zelle_after_recipient.png`, `zelle_after_amount.png` | Multi-step debug captures |
| `*_error.png`, `mfa_debug.png` | Failure-mode screenshots |

All `*.png` and `*.log` are gitignored.

---

## Security & privacy notes

- **Never commit `.env`.** Use Keychain for everything sensitive.
- The persistent browser profile at `~/.tmo_browser_profile/` contains live BoA session cookies. macOS file permissions (700 by default for `~/`) protect it; do not share or back it up to a syncing folder.
- The state file at `~/.tmo_state/bill_*.json` contains transaction confirmation IDs and bill amounts — sensitive but not credentials.
- Confirmation emails do NOT attach the BoA UI screenshot (the screenshot is saved locally only) to avoid leaking account UI fragments to recipients.
- Keychain entries are scoped to your macOS user; `security add-generic-password -a "$USER"` is the default.
- OTP codes are read from `chat.db` and only logged in summary form (never the full 6-digit value).

---

## Architecture decisions

- **Why SQLite, not AppleScript, for Messages reads?** AppleScript's Messages dictionary is deprecated/locked-down on Big Sur+; `tell chat 1` raises syntax errors on modern macOS. Direct `chat.db` reads are version-stable.
- **Why a persistent browser profile?** First-run MFA fatigue is real. With `~/.tmo_browser_profile/`, BoA's "trust this device" cookie persists ~30 days, making subsequent monthly runs skip MFA entirely.
- **Why SMS-gated Stage 0 vs. blind calendar runs?** T-Mobile's "Bill posted MM/DD/YYYY" portal text isn't always present, and a calendar-based fire wastes MFA pushes when the bill isn't ready. The SMS arrives reliably from short code `2535` once the bill is posted; gating on it makes "bill ready" a precondition, not a guess.
- **Why exact-name matching for the BoA Pay button?** Each BoA Zelle recipient row has a Pay, Edit, and Delete button — all containing the recipient's name. `get_by_role("button", name=name, exact=True)` selects only the Pay button (whose accessible name is just the recipient name); fallback iteration filters out edit/delete/manage keywords.
- **Why store `zelle_confirmed_at` and refuse `--force` to override it?** Banks don't make double-payment trivially reversible. The hard lock requires manual filesystem action (`rm ~/.tmo_state/bill_*.json`), making accidental re-payment essentially impossible.

---

## License & disclaimer

This is personal automation for a personal T-Mobile bill paid via personal Bank of America Zelle. It's published as a reference for similar automations — adapt at your own risk.

The code interacts with T-Mobile and Bank of America websites by clicking through their UI. Both companies may change their UI at any time, breaking selectors. The repo includes debug-screenshot capture and failure alerts so breakage is visible immediately, but you should verify monthly that the confirmation email arrives.

**Do not push your `.env` or `*.png` screenshots to a public repo** — they contain live credentials and account UI respectively.
