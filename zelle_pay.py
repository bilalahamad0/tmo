"""BoA Zelle automation. Emits a JSON result on the LAST stdout line:

  {"status": "sent", "confirmation_id": "...", "screenshot": "...", "recipient_name": "..."}
  {"status": "dry_run", "screenshot": "..."}    # ZELLE_LIVE_SEND != "1"
  {"status": "error", "error": "...", "screenshot": "..."}

The orchestrator (app.py) parses that JSON to decide next steps.

Live send is GATED by ZELLE_LIVE_SEND=1 (env or .env). Default is dry-run.

Persistence: uses a persistent Chromium profile at ~/.tmo_browser_profile so
BoA's "trust this device" cookies survive across runs and MFA is only
prompted when the cookie expires (~30 days).
"""
import json
import os
import re
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

load_dotenv()

import sms_utils  # noqa: E402
from security_utils import get_env_or_keychain, get_keychain_password  # noqa: E402


CONFIRMATION_PATTERNS = [
    re.compile(
        r"Confirmation\s*#\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{3,})", re.IGNORECASE
    ),
    re.compile(
        r"Confirmation\s+Number\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{3,})",
        re.IGNORECASE,
    ),
    re.compile(
        r"Reference\s*#\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{3,})", re.IGNORECASE
    ),
    re.compile(
        r"Transaction\s*ID\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{3,})", re.IGNORECASE
    ),
]
BROWSER_PROFILE_DIR = os.path.expanduser("~/.tmo_browser_profile")


def _emit(result: dict) -> None:
    """Print result as JSON on its own line so app.py can parse it."""
    print(json.dumps(result))


def _live_send_enabled() -> bool:
    return os.getenv("ZELLE_LIVE_SEND", "0").strip() == "1"


def get_last_sms_code(retries: int = 6, delay_seconds: int = 5) -> str | None:
    """Wrapper for backwards compatibility - delegates to sms_utils."""
    return sms_utils.find_otp_code(
        retries=retries, delay_seconds=delay_seconds
    )


def _try_trust_device(page) -> None:
    """If BoA prompts to trust this device, click yes so future runs skip MFA."""
    try:
        time.sleep(2)
        trust_btn = (
            page.locator(
                'button:has-text("Save this device"), '
                'button:has-text("Yes, save"), '
                'button:has-text("Save"), '
                'button:has-text("Trust"), '
                'button:has-text("Remember this device"), '
                'input[type="checkbox"][id*="trust"], '
                'input[type="checkbox"][id*="remember"]'
            )
            .filter(visible=True)
            .first
        )
        if trust_btn.is_visible(timeout=3000):
            trust_btn.click()
            print("Clicked 'Trust this device' so future runs skip MFA.")
            time.sleep(2)
    except Exception:
        pass


EDIT_BUTTON_KEYWORDS = (
    "edit", "delete", "remove", "manage", "more", "options",
    "change", "modify",
)


def _find_recipient(page, name: str):
    """Find a clickable Send-money button for the named recipient.

    BoA's Zelle list shows multiple buttons per recipient (Send, Edit, Delete).
    A naive 'button:has-text(name)' matches all of them. Strategy:

    1. Try get_by_role with exact accessible name = `name`. This matches a
       button whose computed name is exactly the recipient (typical for the
       primary send target), excluding 'Edit <name>' or 'Delete <name>'.
    2. Try 'Send money to <name>' / 'Send to <name>' role-name patterns.
    3. Iterate all buttons containing the name and skip ones whose text or
       aria-label contains edit/delete/manage keywords.
    """
    # Strategy 1: exact accessible-name match
    exact_role = page.get_by_role("button", name=name, exact=True).filter(
        visible=True
    )
    if exact_role.count() > 0:
        print(
            f"Recipient '{name}': {exact_role.count()} exact-role matches - "
            f"using first."
        )
        return exact_role.first

    # Strategy 2: 'Send to <name>' pattern
    send_pattern = re.compile(
        rf"send\s+(money\s+)?to\s+{re.escape(name)}", re.IGNORECASE
    )
    send_role = page.get_by_role("button", name=send_pattern).filter(visible=True)
    if send_role.count() > 0:
        print(
            f"Recipient '{name}': {send_role.count()} 'Send to' role matches - "
            f"using first."
        )
        return send_role.first

    # Strategy 3: iterate and exclude edit/delete buttons
    candidates = page.locator(f'button:has-text("{name}")').filter(visible=True)
    total = candidates.count()
    print(
        f"Recipient '{name}': {total} button matches; filtering out "
        f"edit/delete/manage..."
    )
    for i in range(total):
        btn = candidates.nth(i)
        try:
            aria = (btn.get_attribute("aria-label") or "").lower()
            text = (btn.inner_text() or "").lower()
        except Exception:
            continue
        if any(kw in aria or kw in text for kw in EDIT_BUTTON_KEYWORDS):
            print(f"  Skip button {i}: text={text[:50]!r} aria={aria[:50]!r}")
            continue
        print(f"  Use button {i}: text={text[:50]!r} aria={aria[:50]!r}")
        return btn

    # Strategy 4: fall back to bare text node (last resort)
    text_match = page.get_by_text(name, exact=True).filter(visible=True)
    if text_match.count() > 0:
        print(f"Recipient '{name}': falling back to text-node click.")
        return text_match.first
    loose = page.get_by_text(name, exact=False).filter(visible=True)
    if loose.count() > 0:
        print(f"Recipient '{name}': using contains-match fallback.")
        return loose.first
    return None


def _fill_date_if_visible(page) -> bool:
    """If a 'Choose a payment date' field is visible, fill it with today.

    BoA's Zelle Pay flow inserts a date-entry step between amount and Review.
    Without this, clicking Next on the date page is a no-op (date is required).
    """
    today = datetime.now().strftime("%m/%d/%Y")
    date_selectors = (
        'input[placeholder*="mm/dd" i], '
        'input[name*="date" i], '
        'input[id*="date" i], '
        'input[type="date"]'
    )
    try:
        date_field = page.locator(date_selectors).filter(visible=True).first
        if date_field.is_visible(timeout=1500):
            date_field.fill(today)
            page.keyboard.press("Tab")
            time.sleep(1)
            print(f"Filled payment date: {today}")
            return True
    except Exception:
        pass
    return False


REVIEW_ACTION_PATTERN = re.compile(
    r"^(Pay|Send|Send Now)$", re.IGNORECASE
)


def _is_on_review_screen(page) -> bool:
    """Detect the final Review screen.

    BoA labels the final action button 'Pay' on the Zelle review screen
    (alongside Cancel and Edit). Other banks/flows use 'Send' or 'Send Now'.
    Exact-name matching avoids 'Send Money' / 'Pay <name>' on earlier pages.
    """
    try:
        action_btn = (
            page.get_by_role("button", name=REVIEW_ACTION_PATTERN)
            .filter(visible=True)
        )
        if action_btn.count() > 0:
            return True
    except Exception:
        pass
    try:
        confirm_btn = (
            page.locator(
                'button:has-text("Confirm and Send"), '
                'button:has-text("Confirm & Send")'
            )
            .filter(visible=True)
        )
        if confirm_btn.count() > 0:
            return True
    except Exception:
        pass
    # Fallback: 'Review payment details' header text
    try:
        header = (
            page.get_by_text(
                re.compile(
                    r"Review payment details|Review & Send|Review your payment",
                    re.IGNORECASE,
                )
            )
            .filter(visible=True)
        )
        if header.count() > 0:
            return True
    except Exception:
        pass
    return False


def _advance_to_review(page, max_steps: int = 5) -> bool:
    """Click Next/Review/Continue and fill required fields until we land on
    the Review screen (Send button visible)."""
    next_selectors = (
        'button:has-text("Review"), '
        'button:has-text("Review & Send"), '
        'button:has-text("Review and Send"), '
        'button:has-text("Continue"), '
        'button:has-text("Next"), '
        '[role="button"]:has-text("Review"), '
        '[role="button"]:has-text("Continue"), '
        'input[type="submit"][value*="Review" i]'
    )
    for step in range(max_steps):
        if _is_on_review_screen(page):
            print(f"Reached Review screen after {step} step(s).")
            return True

        # Fill any newly-visible required fields before clicking Next
        _fill_date_if_visible(page)

        try:
            btn = page.locator(next_selectors).filter(visible=True).first
            btn.wait_for(state="visible", timeout=10000)
            btn.click()
            print(f"Step {step}: clicked Next/Review/Continue.")
            time.sleep(2)
        except Exception as e:
            print(f"Step {step}: no more Next/Review buttons ({e}).")
            return False

    return _is_on_review_screen(page)


def _fill_amount(page, amount: str) -> bool:
    """Fill the Zelle amount field. Tries broad selectors and handles
    intermediate Send/Continue buttons that some BoA flows insert between
    recipient selection and amount entry."""
    amount_selectors = (
        'input[name="amount"], '
        '.amount-input, '
        'input[id*="amount" i], '
        'input[name*="amount" i], '
        'input[placeholder*="$"], '
        'input[aria-label*="mount"]'
    )

    for attempt in range(3):
        try:
            page.wait_for_selector(amount_selectors, state="visible", timeout=10000)
            page.locator(amount_selectors).filter(visible=True).first.fill(str(amount))
            return True
        except Exception:
            print(f"Amount input not visible (attempt {attempt + 1}); "
                  f"trying intermediate Send/Continue button...")
            try:
                hop = (
                    page.locator(
                        'button:has-text("Send Money"), '
                        'button:has-text("Send money"), '
                        '[role="button"]:has-text("Send money"), '
                        'button:has-text("Continue"), '
                        'button:has-text("Next"), '
                        'a:has-text("Send Money")'
                    )
                    .filter(visible=True)
                    .first
                )
                hop.click(timeout=4000)
                time.sleep(2)
            except Exception:
                break

    return False


def _capture_confirmation(page) -> tuple[str | None, str]:
    """After clicking Pay/Send, wait for the confirmation page to render,
    then capture the confirmation ID and a screenshot.

    Returns (confirmation_id, screenshot_path).

    The previous implementation took the screenshot before the confirmation
    page fully rendered, so the ID was never captured. We now:
    1. Wait for the Pay/Send button to disappear (means we left Review)
    2. Wait for confirmation-text indicators to appear in the DOM
    3. Sleep an extra 4s for any deferred content
    4. Try multiple confirmation-ID patterns
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot = f"zelle_confirmation_{timestamp}.png"
    confirmation_id = None

    print("Waiting for Zelle confirmation page to render...")
    # Step 1: wait for Pay/Send button to be gone
    try:
        page.wait_for_function(
            """() => {
                const btns = Array.from(document.querySelectorAll('button'));
                return !btns.some(b => /^(Pay|Send|Send Now)$/i.test(
                    (b.textContent || '').trim()
                ));
            }""",
            timeout=30000,
        )
    except Exception:
        print("Pay/Send button still visible after 30s; proceeding anyway.")

    # Step 2: wait for confirmation indicators
    try:
        page.wait_for_selector(
            "text=/Confirmation\\s*#|confirmation\\s+number|"
            "payment\\s+sent|successfully\\s+sent|"
            "money\\s+(?:has\\s+been\\s+)?sent|on\\s+its\\s+way/i",
            timeout=20000,
        )
        print("Confirmation page indicators visible.")
    except Exception:
        print("No confirmation text appeared in 20s; capturing anyway.")

    # Step 3: extra settling time
    time.sleep(4)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    # Step 4: screenshot AFTER the page has settled
    try:
        page.screenshot(path=screenshot, full_page=True)
    except Exception as e:
        print(f"Could not screenshot confirmation page: {e}")
        screenshot = ""

    # Step 5: try multiple ID patterns across the main page AND every iframe.
    # BoA renders the Zelle send/confirmation UI - and the "Confirmation #" -
    # inside an iframe, so the main-frame body text alone never contained the
    # ID (the previous capture only read page.locator("body"), which is why a
    # genuinely successful payment was reported as sent_unconfirmed).
    try:
        texts = []
        for fr in page.frames:
            try:
                texts.append(fr.locator("body").inner_text(timeout=5000))
            except Exception:
                continue
        body_text = "\n".join(texts)
        for pat in CONFIRMATION_PATTERNS:
            m = pat.search(body_text)
            if m:
                confirmation_id = m.group(1)
                print(f"Captured confirmation id: {confirmation_id}")
                break
        if not confirmation_id:
            print(
                f"No confirmation ID pattern matched across {len(texts)} "
                f"frame(s). First 400 chars:\n{body_text[:400]}"
            )
    except Exception as e:
        print(f"Could not read confirmation text: {e}")

    return confirmation_id, screenshot


def handle_boa_zelle(amount: str) -> dict:
    user = get_keychain_password("BoA_Username")
    password = get_keychain_password("BoA_Password")
    recipient_name = get_env_or_keychain(
        "ZELLE_RECIPIENT_NAME", "ZELLE_RECIPIENT_NAME"
    )

    if not user or not password:
        return {
            "status": "error",
            "error": (
                "Bank credentials missing from Keychain. "
                "Set BoA_Username and BoA_Password via 'security add-generic-password'."
            ),
        }
    if not recipient_name:
        return {
            "status": "error",
            "error": "ZELLE_RECIPIENT_NAME not configured (env or Keychain).",
        }

    # Defense-in-depth amount validation. app.py's safety gate also enforces
    # this, but a direct `python zelle_pay.py <amount>` invocation must NOT be
    # able to bypass the cap. Mirrors app.py: ZELLE_AMOUNT_CAP via env or
    # Keychain, default 300.
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return {"status": "error", "error": f"Invalid amount: {amount!r}"}
    if amt <= 0:
        return {
            "status": "error",
            "error": "Amount is 0 or negative; nothing to pay.",
        }
    cap_str = get_env_or_keychain("ZELLE_AMOUNT_CAP", "ZELLE_AMOUNT_CAP")
    try:
        cap = float(cap_str) if cap_str else 300.0
    except ValueError:
        cap = 300.0
    if amt > cap:
        return {
            "status": "error",
            "error": (
                f"Amount ${amt:.2f} exceeds cap ${cap:.2f}; refusing to send. "
                "Raise ZELLE_AMOUNT_CAP only after manual review."
            ),
        }

    live = _live_send_enabled()
    print(
        f"Starting Zelle Payment Automation for ${amount} to {recipient_name} "
        f"(live_send={live})..."
    )

    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        # Persistent context: cookies/localStorage survive across runs so
        # BoA's "trust this device" eliminates MFA on subsequent runs.
        context = p.chromium.launch_persistent_context(
            user_data_dir=BROWSER_PROFILE_DIR,
            headless=False,
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = context.pages[0] if context.pages else context.new_page()
        Stealth().apply_stealth_sync(page)

        try:
            print("Navigating to Bank of America...")
            page.goto(
                "https://www.bankofamerica.com/", wait_until="domcontentloaded"
            )
            time.sleep(3)

            print("Entering credentials...")
            id_sel = (
                'input[name="oid"], #oid, input[name="onlineId1"], #onlineId1'
            )
            pass_sel = (
                'input[name="pass"], #pass, input[name="passcode1"], '
                "#passcode1"
            )
            page.locator(id_sel).first.fill(user)
            page.locator(pass_sel).first.fill(password)

            print("Clicking login...")
            login_btn = (
                page.locator(
                    'button:has-text("Log in"), #hp-sign-in-btn, .vip-login-btn'
                )
                .filter(visible=True)
                .first
            )
            login_btn.click()

            # Detect MFA by element presence, NOT page-content keywords.
            # Words like "identity", "security", "verify" appear in BoA's
            # nav/footer on every page and gave constant false positives.
            print("Checking for Identity Verification...")
            time.sleep(8)
            otp_present = (
                page.locator(
                    'input[autocomplete="one-time-code"], '
                    'input[id*="otp" i], '
                    'input[name*="otp" i], '
                    "#tlpvt-otp-input"
                )
                .filter(visible=True)
                .count()
                > 0
            )
            # MFA-specific buttons (pure CSS)
            mfa_btn_present = (
                page.locator(
                    'button:has-text("Send code"), '
                    'button:has-text("Text me"), '
                    'button:has-text("Verify your identity")'
                )
                .filter(visible=True)
                .count()
                > 0
            )
            # MFA-specific page text - use get_by_text (text engine)
            mfa_text_present = False
            try:
                mfa_text_present = (
                    page.get_by_text(
                        re.compile(
                            r"Verify your identity|Enter the code|"
                            r"We sent (a|the) code",
                            re.IGNORECASE,
                        )
                    )
                    .filter(visible=True)
                    .count()
                    > 0
                )
            except Exception:
                pass
            mfa_required = otp_present or mfa_btn_present or mfa_text_present

            if mfa_required:
                print("MFA required. Handling verification...")
                page.screenshot(path="mfa_debug.png")
                # Phone last-4 for SMS option selection comes from Keychain
                # so we don't hardcode personal phone fragments in source.
                phone_last4 = get_env_or_keychain(
                    "BoA_MFA_Phone_Last4", "BoA_MFA_Phone_Last4"
                )
                try:
                    if phone_last4:
                        print("Selecting phone number for SMS code...")
                        page.locator(f"text=/{phone_last4}/").first.click(
                            timeout=10000
                        )
                        time.sleep(1)

                    print("Clicking 'Next'...")
                    time.sleep(2)
                    page.locator('button:has-text("Next")').filter(
                        visible=True
                    ).first.click(timeout=10000)
                except Exception as mfa_nav_err:
                    print(f"Phone selection step skipped: {mfa_nav_err}")

                print("Attempting to auto-read SMS code...")
                code = get_last_sms_code()
                auto_filled = False
                if code:
                    # Don't log the actual OTP - it ends up in automation.log
                    # which has no special access protection.
                    print("Auto-filling OTP (6 digits, value redacted)...")
                    try:
                        otp_input = (
                            page.locator(
                                'input[type="text"], input[name="otp"], '
                                "#tlpvt-otp-input"
                            )
                            .filter(visible=True)
                            .first
                        )
                        otp_input.fill(code)
                        page.locator(
                            'button:has-text("Verify"), '
                            'button:has-text("Submit")'
                        ).filter(visible=True).first.click()
                        auto_filled = True
                    except Exception as fill_err:
                        print(f"Auto-fill failed, will wait for manual: {fill_err}")
                else:
                    print("No SMS code retrieved.")

                # Wait for MFA to clear (auto-filled or manual fallback).
                print("Waiting up to 5 minutes for MFA to clear...")
                try:
                    page.wait_for_function(
                        """() => {
                            const sels = [
                                'input[name="otp"]',
                                '#tlpvt-otp-input',
                                'input[id*="otp"]',
                                'input[autocomplete="one-time-code"]'
                            ];
                            return !sels.some(s => {
                                const el = document.querySelector(s);
                                return el && el.offsetParent !== null;
                            });
                        }""",
                        timeout=300000,
                    )
                    print("MFA cleared.")
                except Exception:
                    print("MFA timeout - OTP not submitted within 5 minutes.")
                    page.screenshot(path="mfa_timeout.png")
                    context.close()
                    return {
                        "status": "error",
                        "error": "MFA timeout - OTP not entered",
                        "screenshot": "mfa_timeout.png",
                    }

                # Click "Save this device" so future runs skip MFA entirely.
                _try_trust_device(page)

            print("Navigating to Zelle Transfer page...")
            page.goto(
                "https://secure.bankofamerica.com/paytransfer-peerpay/home/?",
                wait_until="networkidle",
            )

            print(f"Locating recipient: {recipient_name}")
            recipient_entry = _find_recipient(page, recipient_name)
            if recipient_entry is None:
                # Wait briefly for late-loading recipient list
                time.sleep(3)
                recipient_entry = _find_recipient(page, recipient_name)
            if recipient_entry is None:
                screenshot = "zelle_recipient_not_found.png"
                page.screenshot(path=screenshot, full_page=True)
                context.close()
                return {
                    "status": "error",
                    "error": (
                        f"Recipient '{recipient_name}' not found on Zelle "
                        f"page. Check ZELLE_RECIPIENT_NAME matches what BoA "
                        f"displays."
                    ),
                    "screenshot": screenshot,
                }
            recipient_entry.click()
            time.sleep(2)
            try:
                page.screenshot(path="zelle_after_recipient.png", full_page=True)
            except Exception:
                pass

            print(f"Inputting amount: {amount}")
            if not _fill_amount(page, amount):
                screenshot = "zelle_amount_not_found.png"
                page.screenshot(path=screenshot, full_page=True)
                context.close()
                return {
                    "status": "error",
                    "error": (
                        "Amount input not found after recipient click. "
                        "Check zelle_after_recipient.png to see what page "
                        "loaded. BoA may have changed the Zelle flow or the "
                        "recipient click landed on a non-navigating element."
                    ),
                    "screenshot": screenshot,
                }

            # Tab off the amount field so BoA commits the value
            try:
                page.keyboard.press("Tab")
            except Exception:
                pass
            time.sleep(1)
            try:
                page.screenshot(path="zelle_after_amount.png", full_page=True)
            except Exception:
                pass

            # Multi-step: click Next/Review repeatedly, filling date along
            # the way, until we reach the final Review screen (Send button).
            if not _advance_to_review(page):
                screenshot = "zelle_review_not_reached.png"
                page.screenshot(path=screenshot, full_page=True)
                context.close()
                return {
                    "status": "error",
                    "error": (
                        "Could not reach Review screen after amount entry. "
                        f"Check {screenshot} to see where the flow stalled."
                    ),
                    "screenshot": screenshot,
                }

            if not live:
                screenshot = "zelle_review_dryrun.png"
                try:
                    page.screenshot(path=screenshot, full_page=True)
                except Exception:
                    screenshot = ""
                print("DRY-RUN: would click Send. Set ZELLE_LIVE_SEND=1 to enable.")
                context.close()
                return {
                    "status": "dry_run",
                    "screenshot": screenshot,
                    "recipient_name": recipient_name,
                }

            print("LIVE: clicking final action button (Pay/Send)...")
            # BoA labels this 'Pay'; other flows use 'Send' / 'Send Now'.
            # Exact-name match avoids 'Pay <name>' on earlier pages.
            action_btn = (
                page.get_by_role("button", name=REVIEW_ACTION_PATTERN)
                .filter(visible=True)
                .first
            )
            action_btn.wait_for(state="visible", timeout=15000)
            action_btn.click()

            confirmation_id, screenshot = _capture_confirmation(page)
            context.close()

            # If the Pay click succeeded but no confirmation ID could be
            # captured, report a DISTINCT status so app.py flags the month for
            # manual verification instead of silently marking it paid.
            return {
                "status": "sent" if confirmation_id else "sent_unconfirmed",
                "confirmation_id": confirmation_id,
                "screenshot": screenshot,
                "recipient_name": recipient_name,
            }

        except Exception as e:
            print(f"Error during Zelle process: {e}")
            screenshot = "zelle_error.png"
            try:
                page.screenshot(path=screenshot)
            except Exception:
                screenshot = ""
            try:
                context.close()
            except Exception:
                pass
            return {
                "status": "error",
                "error": str(e),
                "screenshot": screenshot,
            }


if __name__ == "__main__":
    amount_to_pay = sys.argv[1] if len(sys.argv) > 1 else "0.00"
    result = handle_boa_zelle(amount_to_pay)
    _emit(result)
    sys.exit(
        0
        if result.get("status") in ("sent", "dry_run", "sent_unconfirmed")
        else 1
    )
