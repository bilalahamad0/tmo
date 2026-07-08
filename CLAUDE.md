# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Hands-off macOS pipeline that reacts to a T-Mobile billing SMS, downloads the bill PDF from the T-Mobile portal (Playwright), parses per-line charges, emails a styled HTML summary, and pays the "special pool" amount via Bank of America Zelle (Playwright). Designed to run unattended from a LaunchAgent on days 6–10 of each month.

End-to-end stages, all orchestrated by `app.py::_run_pipeline()`:

```
Stage 0   SMS gate         sms_utils.find_tmobile_bill_sms()  (reads ~/Library/Messages/chat.db)
Stage 1+2 Download PDF     download_bill.download_tmobile_bill()  (Playwright → T-Mobile)
Stage 3   Parse            app.parse_bill()  (pypdf)
Stage 4   Summary email    notify.send_bill_summary_email()
Stage 5   Safety gate      app._zelle_safety_gate()  (amount cap, recipient match, idempotency)
Stage 6   Zelle pay        zelle_pay.handle_boa_zelle()  (Playwright → BoA, subprocess)
Stage 7   Confirm / alert  notify.send_confirmation_email() | send_failure_alert()
```

Per-bill state lives at `~/.tmo_state/bill_<YYYY-MM>.json` and is the source of truth for which stages have completed.

## Common commands

```bash
# Setup (one-time)
python3 -m venv tmobile_env
./tmobile_env/bin/pip install -r requirements.txt
./tmobile_env/bin/playwright install chromium

# Full pipeline
./tmobile_env/bin/python app.py

# Skip download — feed an existing PDF
./tmobile_env/bin/python app.py /path/to/bill.pdf

# Bypass soft idempotency gates (re-email, re-attempt Zelle).
# Does NOT bypass the hard zelle_confirmed_at lock.
./tmobile_env/bin/python app.py --force

# Reconcile a month that ACTUALLY paid but got stuck 'attempted/unconfirmed'
# (writes zelle_confirmed_at + clears the limbo flags; does NOT re-pay).
# The safe alternative to deleting the state file, which would pay again.
./tmobile_env/bin/python app.py --mark-paid <confirmation_id>
./tmobile_env/bin/python app.py --mark-paid <confirmation_id> --month 2026-07
./tmobile_env/bin/python app.py --mark-paid            # paid, but no id to record

# Run a sub-stage in isolation
./tmobile_env/bin/python download_bill.py
./tmobile_env/bin/python zelle_pay.py 75.00     # respects ZELLE_LIVE_SEND

# Tests
./tmobile_env/bin/python -m pytest tests/ -v
./tmobile_env/bin/python -m pytest tests/test_parse_bill.py::test_specific -v

# Security / style (used by CI but runnable locally)
./tmobile_env/bin/bandit -r .
./tmobile_env/bin/pip-audit
./tmobile_env/bin/flake8 app.py download_bill.py zelle_pay.py notify.py sms_utils.py
```

## Architecture notes

**`app.py`** is the stage-aware orchestrator and the only entrypoint that touches all subsystems. It owns the PDF parser, the Zelle safety gate (amount cap, recipient name match, idempotency check), and spawns `zelle_pay.py` as a subprocess so the BoA browser session is isolated from the T-Mobile session.

**`zelle_pay.py`** drives BoA's Zelle multi-step flow with Playwright + `playwright-stealth` (BoA blocks vanilla Playwright). Recipient lookup uses **exact accessible-name matching** on the Pay button (filters Edit/Delete/Manage). OTP is auto-pulled from `sms_utils.find_otp_code()`. `ZELLE_LIVE_SEND=0` (default) is a **dry-run that stops at the Review screen** and saves `zelle_review_dryrun.png`; only `ZELLE_LIVE_SEND=1` clicks Send.

**`download_bill.py`** uses a **persistent Playwright profile at `~/.tmo_browser_profile/`** so the "Trust this device" cookie survives ~30 days and most runs skip 2FA. `_dismiss_overlays()` handles OneTrust, the T-Mobile Privacy notice, and the MoEngage popup, in that order.

**`sms_utils.py`** reads `chat.db` via sqlite3 directly (AppleScript is unreliable on Big Sur+). On Big Sur+ some messages have a NULL `text` column and the body lives in a binary `attributedBody` blob — `_decode_attributed_body()` extracts the longest printable-ASCII run as a heuristic.

**`state.py`** writes atomically (tempfile + rename) to `~/.tmo_state/bill_<YYYY-MM>.json`. `zelle_confirmed_at` is the **hard lock against re-payment** — even `--force` won't bypass it. To re-run a fully completed bill, `rm` the state file.

**`security_utils.py`** wraps macOS `security find-generic-password`. Credentials are read env-var first, Keychain second. Stored services: `TMobile_User`, `TMobile_Pass`, `BoA_Username`, `BoA_Password`, `ZELLE_RECIPIENT_NAME`, `ZELLE_AMOUNT_CAP`, optional `BoA_MFA_Phone_Last4`.

**`auto_process.sh` + `com.example.tmobile_automation.plist`** — the LaunchAgent fires at 9 AM on days 6–10. `__REPO_PATH__` in the plist must be sed-substituted at install. `StartCalendarInterval` **does not wake the Mac** — pair with `pmset repeat wakeorpoweron MTWRFSU 08:55:00` for reliable scheduled runs.

## Gotchas a fresh Claude instance will hit

- **Full Disk Access (FDA)** must be granted to whatever process spawns Python — `Terminal.app` for manual runs, `/sbin/launchd` for scheduled runs. Without FDA, `chat.db` raises "unable to open database file" and Stage 0 silently reports "no SMS found", which looks like the pipeline correctly skipping.
- **The hard `zelle_confirmed_at` lock** is intentional. If you need to test a re-run after a successful pay, delete `~/.tmo_state/bill_<YYYY-MM>.json` rather than editing it, unless you know which fields the next stage will read.
- **A crash mid-Zelle** (after `zelle_attempted_at` but before `zelle_confirmed_at`) blocks subsequent runs and alerts **once** (the `zelle_gate` alert is deduped per month). Verify in the BoA UI, then reconcile — don't `--force`: if it **did** pay, `app.py --mark-paid <id>` (records it, keeps the re-payment lock); if it **didn't**, `rm ~/.tmo_state/bill_<YYYY-MM>.json` to allow a fresh attempt. Deleting a month that *did* pay causes a double payment.
- **Confirmation screenshots are not emailed** even on success — they're saved to the repo root only. This is deliberate so BoA UI state never leaves the machine.
- **`.env` and `~/.tmo_browser_profile/`** are secret-bearing — the profile holds live BoA session cookies. Both are gitignored. Don't put the profile in a syncing folder.
- The persistent profile occasionally needs to be deleted (`rm -rf ~/.tmo_browser_profile`) if BoA invalidates the "trust this device" cookie — the next run will require fresh MFA and re-establish the cookie.

## Testing

`pytest` with `unittest.mock`. Tests cover the deterministic pieces (state I/O, parsers, regex, SMTP composition, Keychain) and stub the SMTP/Keychain boundaries. Live T-Mobile portal and live BoA Zelle flows are intentionally **not** in the test suite — verify those with manual dry-runs (`ZELLE_LIVE_SEND=0`).

## CI

- `.github/workflows/codeql.yml` — Python CodeQL, push/PR to main + weekly.
- `.github/workflows/update-ai-metrics.yml` — refreshes `ai-metrics.json` (commit count, LOC) on `repository_dispatch` / `workflow_dispatch`. The commits seen on `main` from `bilalahamad-bot` are this workflow.
