"""BoA Zelle automation. Emits a JSON result on the LAST stdout line:

  {"status": "sent", "confirmation_id": "...", "payment_sent": true, "screenshot": "...", "recipient_name": "..."}
  {"status": "sent_unconfirmed", "confirmation_id": null, "payment_sent": false, "screenshot": "...", "recipient_name": "..."}
  {"status": "dry_run", "screenshot": "..."}    # ZELLE_LIVE_SEND != "1"
  {"status": "error", "error": "...", "screenshot": "..."}

"sent" means the payment went through - proven by a captured confirmation id
AND/OR BoA's "Your payment is sent." banner. "sent_unconfirmed" means Pay was
clicked but NEITHER signal was seen, so a human must verify in Bank of America
before the next run (blocks auto-retry to avoid a double payment).

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

# BoA's Zelle success page shows a "Your payment is sent." banner separately
# from the "Confirmation #" reference number. That banner is an independent,
# first-class proof the money moved - so a run where we can read the banner but
# not scrape the reference number is a SUCCESS, not a "maybe it failed". Kept
# specific (each phrase only appears on a completed send, never on Review) so
# it can't false-positive the earlier steps of the flow.
SUCCESS_PATTERNS = re.compile(
    r"your\s+payment\s+is\s+sent"
    r"|payment\s+(?:is|has\s+been|was)\s+sent"
    r"|successfully\s+sent"
    r"|(?:payment|money)\s+is\s+on\s+its\s+way"
    r"|money\s+(?:has\s+been\s+)?sent",
    re.IGNORECASE,
)


def _extract_confirmation_id(text: str) -> str | None:
    """First CONFIRMATION_PATTERNS match in `text`, or None."""
    if not text:
        return None
    for pat in CONFIRMATION_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _payment_sent(text: str) -> bool:
    """True if `text` contains BoA's 'payment sent' success banner.

    Fed the page's RENDERED (visible) text only, so a hidden success template
    in the DOM can never trip it into a false positive.
    """
    return bool(text) and SUCCESS_PATTERNS.search(text) is not None


def _classify_send(confirmation_id: str | None, payment_sent: bool) -> str:
    """Map capture results to a status.

    A payment is 'sent' when we have EITHER a confirmation id OR BoA's explicit
    'payment sent' banner. Only when we have NEITHER is the outcome genuinely
    ambiguous ('sent_unconfirmed') and worth a human's manual verification.
    """
    return "sent" if (confirmation_id or payment_sent) else "sent_unconfirmed"


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


def _gather_frame_text(page) -> tuple[str, str]:
    """Return (visible_text, raw_text) aggregated across EVERY frame.

    BoA renders the whole Zelle UI - and the "Confirmation #" - inside an
    iframe, so the main frame's body text never contains the id.

    - visible_text = inner_text() (rendered; hidden nodes excluded). The TRUSTED
      source for both the reference number and the 'payment sent' banner.
    - raw_text = text_content() (raw DOM text; layout-independent). A fallback
      that stays readable even when inner_text() on the BoA iframe comes back
      empty right after the send. It is deliberately NOT used to store a
      reference number: text_content drops inter-element whitespace, so an id
      can run into the next element's text ('s1xrlf1ab' + 'Send' ->
      's1xrlf1abSend'). It only ever flips payment_sent -> True.
    """
    visible_parts: list[str] = []
    raw_parts: list[str] = []
    for fr in page.frames:
        try:
            vis = fr.locator("body").inner_text(timeout=3000)
            if vis:
                visible_parts.append(vis)
        except Exception:
            pass
        try:
            raw = fr.locator("body").text_content(timeout=3000)
            if raw:
                raw_parts.append(raw)
        except Exception:
            pass
    return "\n".join(visible_parts), "\n".join(raw_parts)


def _read_confirmation(page) -> tuple[str | None, bool]:
    """One read of the confirmation state: (confirmation_id, payment_sent).

    The id and the banner both come from inner_text (clean, hidden nodes
    excluded). text_content is consulted ONLY when inner_text is entirely empty
    across every frame - the pathological 'iframe body unreadable via layout'
    case - and then only to conclude a send completed, never to store a
    possibly-mangled id.
    """
    visible_text, raw_text = _gather_frame_text(page)
    confirmation_id = _extract_confirmation_id(visible_text)
    payment_sent = _payment_sent(visible_text)
    if not confirmation_id and not payment_sent and not visible_text.strip():
        if _extract_confirmation_id(raw_text) or _payment_sent(raw_text):
            payment_sent = True
    return confirmation_id, payment_sent


def _capture_confirmation(page) -> tuple[str | None, bool, str]:
    """After clicking Pay/Send, wait for the confirmation page, then capture
    the confirmation ID and whether BoA showed its 'payment sent' banner.

    Returns (confirmation_id, payment_sent, screenshot_path).

    A missing reference number is NOT treated as a possible failure when the
    banner (and BoA's own confirmation email) prove the money moved - that
    false alarm is exactly what made a clean run look broken.

    We poll every frame until either a confirmation id or the success banner
    appears, then screenshot. This replaces the old main-frame-only waits,
    which never saw the iframe content and just burned ~20s doing nothing; the
    per-iteration re-read of page.frames also dodges the stale-frame races that
    made a single post-send inner_text() read come back empty.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot = f"zelle_confirmation_{timestamp}.png"
    confirmation_id = None
    payment_sent = False

    print("Waiting for Zelle confirmation page to render...")
    for _ in range(15):  # ~30s of polling, with early break
        confirmation_id, payment_sent = _read_confirmation(page)
        if confirmation_id or payment_sent:
            print(
                f"Confirmation detected (id={confirmation_id!r}, "
                f"payment_sent={payment_sent})."
            )
            break
        time.sleep(2)

    # Let deferred content settle, then screenshot the final rendered state.
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    try:
        page.screenshot(path=screenshot, full_page=True)
    except Exception as e:
        print(f"Could not screenshot confirmation page: {e}")
        screenshot = ""

    # Final read after settling - the reference number sometimes renders a beat
    # after the success banner does.
    cid2, sent2 = _read_confirmation(page)
    confirmation_id = confirmation_id or cid2
    payment_sent = payment_sent or sent2

    if confirmation_id:
        print(f"Captured confirmation id: {confirmation_id}")
    elif payment_sent:
        print(
            "No confirmation id parsed, but BoA showed its 'payment sent' "
            "banner - treating the payment as sent."
        )
    else:
        visible_text, raw_text = _gather_frame_text(page)
        print(
            "Neither a confirmation id nor a 'payment sent' banner was found.\n"
            f"Visible text (first 300): {visible_text[:300]!r}\n"
            f"Raw text (first 300): {raw_text[:300]!r}"
        )

    return confirmation_id, payment_sent, screenshot


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

            confirmation_id, payment_sent, screenshot = _capture_confirmation(
                page
            )
            context.close()

            # 'sent' when we have a confirmation id AND/OR BoA's 'payment sent'
            # banner. Only 'sent_unconfirmed' (NEITHER signal) tells app.py to
            # flag the month for manual verification instead of marking it paid.
            return {
                "status": _classify_send(confirmation_id, payment_sent),
                "confirmation_id": confirmation_id,
                "payment_sent": payment_sent,
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
