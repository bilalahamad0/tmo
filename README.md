# T-Mobile Bill → Zelle End-to-End Automation

Hands-off monthly handler for the T-Mobile bill: detects when a new bill is posted, downloads the PDF, parses the per-line charges, emails a styled breakdown to recipients, sends the special-pool amount via Zelle from Bank of America, and emails a payment confirmation. Failures at any stage email an alert.

## Pipeline

```
[1] T-Mobile portal: is a new bill posted?  →  [2] Download PDF
                                                      ↓
[7] Confirmation email ← [6] Zelle live send ← [5] Safety gate ← [4] Bill summary email ← [3] Parse PDF
                                                                                                ↓
                                                                          Failure at any stage → [Alert email]
```

State for the current month lives at `~/.tmo_state/bill_<YYYY-MM>.json`. The orchestrator reads this on every run, so it's safe to fire as many times as the LaunchAgent wants — once a stage is done, it's not re-done.

The hard guard against double-paying: once `zelle_confirmed_at` is set in state, the pipeline refuses to send Zelle again for that month. If Zelle was attempted but never confirmed (e.g. crash mid-flow), it emails an alert instead of retrying — manual review required.

## Setup

### 1. Python environment
```bash
python3 -m venv tmobile_env
./tmobile_env/bin/pip install -r requirements.txt
./tmobile_env/bin/playwright install chromium
```

### 2. Credentials in macOS Keychain (sensitive)

By default the code uses your current macOS username (`$USER`) as the Keychain account name. Override with `KEYCHAIN_ACCOUNT` env var if you prefer a different identifier.

```bash
security add-generic-password -s "TMobile_User"           -a "$USER" -w "your_tmo_user"
security add-generic-password -s "TMobile_Pass"           -a "$USER" -w "your_tmo_pass"
security add-generic-password -s "BoA_Username"           -a "$USER" -w "your_boa_id"
security add-generic-password -s "BoA_Password"           -a "$USER" -w "your_boa_pass"
security add-generic-password -s "ZELLE_RECIPIENT_NAME"   -a "$USER" -w "Recipient Name"
security add-generic-password -s "ZELLE_AMOUNT_CAP"       -a "$USER" -w "300"

# Optional: last-4 of the phone number BoA texts the SMS OTP to.
# If your BoA login presents multiple SMS-target options, this is used to
# click the right one. Leave unset if BoA only offers one option.
security add-generic-password -s "BoA_MFA_Phone_Last4"    -a "$USER" -w "1234"
```

`ZELLE_AMOUNT_CAP` is the maximum dollar amount the automation will auto-send. If a bill's special-pool amount exceeds the cap, the pipeline aborts and emails a failure alert — manual review required.

### 3. Configuration in `.env` (non-sensitive)
```bash
cp .env.example .env
```
Then edit:
```env
SMTP_EMAIL=your_email@gmail.com
SMTP_PASSWORD=your_gmail_app_password
RECIPIENT_EMAILS=person1@example.com,person2@example.com

# Optional: send confirmation/alert emails to a different audience
CONFIRMATION_RECIPIENTS=you@example.com           # defaults to RECIPIENT_EMAILS
FAILURE_ALERT_EMAIL=you@example.com               # defaults to first RECIPIENT_EMAILS
MFA_ALERT_EMAIL=you@example.com                   # defaults to FAILURE_ALERT_EMAIL

USER_MAPPING='{"(123)456-7890": "Alice", "(098)765-4321": "Bob"}'

SPECIAL_POOL_NAMES=Alice
SPECIAL_POOL_TITLE=Premium Pool
SPECIAL_POOL_DESC=Coverage for Alice

# Live Zelle send is OFF by default. Set to 1 to enable real money transfer.
ZELLE_LIVE_SEND=0
```

### 4. macOS LaunchAgent (scheduled run)

The plist is shipped as a template — substitute your absolute repo path and install:

```bash
sed "s|__REPO_PATH__|$PWD|g" com.example.tmobile_automation.plist \
  > ~/Library/LaunchAgents/com.example.tmobile_automation.plist
launchctl load ~/Library/LaunchAgents/com.example.tmobile_automation.plist
```

Schedule: 9:00 AM on days 6, 7, 8, 9, 10 of each month. Each run re-checks the portal — if no new bill is posted yet, it exits silently and tries again the next scheduled day.

### 5. Sleep-aware wake (one-time)
`StartCalendarInterval` does NOT wake a sleeping Mac. Schedule a daily wake just before the agent fires:
```bash
sudo pmset repeat wakeorpoweron MTWRFSU 08:55:00
```

### 6. Full Disk Access for SMS auto-read (one-time)
The Zelle automation reads bank OTP codes directly from `~/Library/Messages/chat.db` (AppleScript-based reads are unreliable on modern macOS). For this to work, the python binary needs Full Disk Access:

1. Open **System Settings → Privacy & Security → Full Disk Access**
2. Click the `+` button
3. Add the python binary that runs the script. Resolve the actual path first (the venv may be a symlink):
   ```bash
   readlink -f ./tmobile_env/bin/python
   ```
   Use that resolved path. For Terminal-launched runs, granting FDA to **Terminal.app** (or iTerm) is sufficient.
4. For the LaunchAgent (production schedule), grant FDA to **launchd** so the scheduled run inherits it.
5. Restart Terminal (the permission only takes effect for newly launched processes).

If FDA isn't granted, the script will report "PERMISSION DENIED" and OTP auto-fill will fail (you'd have to enter it manually in the open browser window).

### 7. Persistent browser profile
On first run, both T-Mobile and Bank of America will trigger MFA. **Tick "Save this device" / "Trust this device"** when those checkboxes appear — the persistent profile at `~/.tmo_browser_profile` will retain those cookies and skip MFA on subsequent runs (typically for ~30 days). The Zelle script attempts to click the trust prompt automatically, but it's worth verifying on the first manual run.

## Running manually

```bash
# Full pipeline (will hit T-Mobile + BoA, respects state file)
./tmobile_env/bin/python app.py

# Skip download stage by passing an existing PDF
./tmobile_env/bin/python app.py /path/to/SummaryBill_20260504.pdf

# Re-run even if state shows summary already emailed / Zelle attempted
./tmobile_env/bin/python app.py --force

# Just download (no parse/email/Zelle)
./tmobile_env/bin/python download_bill.py

# Just Zelle (testing - reads ZELLE_LIVE_SEND from .env)
./tmobile_env/bin/python zelle_pay.py 75.00
```

## $1 test workflow (highly recommended before live)

To validate the BoA login → MFA → recipient → amount → review/send chain without touching the bill pipeline or sending real bill amounts, set the recipient and run a small live transfer to your own account:

```bash
# 1. Point the keychain entry at your own Zelle-registered account
security add-generic-password -U -s "ZELLE_RECIPIENT_NAME" -a "$USER" -w "Your Own Name"

# 2. Dry-run first - check zelle_review_dryrun.png shows correct amount/recipient
ZELLE_LIVE_SEND=0 ./tmobile_env/bin/python zelle_pay.py 1.00

# 3. Live $1 test - confirms the Send + confirmation capture work
ZELLE_LIVE_SEND=1 ./tmobile_env/bin/python zelle_pay.py 1.00

# 4. After success, point the keychain back to the real recipient
security add-generic-password -U -s "ZELLE_RECIPIENT_NAME" -a "$USER" -w "Real Recipient"
```

## Going live with Zelle

The Zelle send is **gated by `ZELLE_LIVE_SEND=1`**. With the flag off (default), the BoA flow logs in, navigates to the Review screen, takes a screenshot, and stops without clicking Send. This lets you validate the entire chain — bill detection, download, parse, email, BoA login, recipient match, amount entry — against real services without moving money.

Recommended cutover:
1. Run with `ZELLE_LIVE_SEND=0` for a full month. Verify the bill summary email looks right and the dry-run screenshot at `zelle_review_dryrun.png` shows the correct amount and recipient.
2. Set `ZELLE_LIVE_SEND=1` in `.env`.
3. Watch the next scheduled run. Expect a confirmation email within ~5 minutes of the LaunchAgent firing.

## State file

Located at `~/.tmo_state/bill_<YYYY-MM>.json`. Fields:

| Field | Set when |
|---|---|
| `bill_posted_date` | After portal detects a new bill |
| `pdf_path`, `pdf_sha256` | After successful download |
| `parsed_total`, `special_amount` | After parse |
| `summary_emailed_at` | After bill summary email sent |
| `zelle_attempted_at` | Right before clicking Send |
| `zelle_confirmed_at`, `zelle_confirmation_id`, `zelle_screenshot` | After confirmation page captured |

To force a re-run for a given month (e.g. after fixing a bug), delete the relevant state file:
```bash
rm ~/.tmo_state/bill_2026-05.json
```

## Tests

```bash
./tmobile_env/bin/python -m pytest tests/ -v
```

Covers: state I/O (atomic writes, idempotency), notify (SMTP mocked, all three email types), parser (page-shift resilience, month detection, posted-date regex), keychain.

Live Zelle/download stay manual — unsafe to mock against real banking.

## Logs

- `automation.log` — LaunchAgent stdout/stderr from every run
- `download_automation.log` — historical (no longer written; preserved for reference)
- `~/.tmo_state/bill_<YYYY-MM>.json` — per-bill stage progression
- `zelle_confirmation_*.png` — confirmation page screenshots
- `*_error.png` — debug screenshots from failures (login_error.png, zelle_error.png, etc.)
