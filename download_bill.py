"""T-Mobile portal automation: detect new bill, download PDF.

Returns a structured result so the orchestrator can drive idempotency:
- posted_date: ISO date string parsed from "Bill posted MM/DD/YYYY"
- pdf_path: absolute path to the saved PDF
- pdf_sha256: content hash for fingerprint comparison

If the portal's bill posted date matches the date in state, the function
exits early with status="not_new" and the orchestrator skips downstream stages.
"""
import hashlib
import os
import re
import time
from datetime import datetime
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from notify import send_2fa_alert
from security_utils import get_keychain_password

load_dotenv()


BILL_POSTED_PATTERN = re.compile(
    r"Bill\s+posted\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.IGNORECASE
)
BROWSER_PROFILE_DIR = os.path.expanduser("~/.tmo_browser_profile")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_posted_date(text: str) -> str | None:
    """Find 'Bill posted MM/DD/YYYY' in page text; return ISO date or None."""
    match = BILL_POSTED_PATTERN.search(text)
    if not match:
        return None
    month, day, year = match.groups()
    try:
        return datetime(int(year), int(month), int(day)).date().isoformat()
    except ValueError:
        return None


def _login(page, t_user: str) -> None:
    """Phase 1: go to dashboard; if persistent session is active, skip login.

    With the persistent browser profile, T-Mobile remembers the session and
    redirects /signin straight to the dashboard. Detecting the username
    field is the reliable way to know whether we still need to authenticate.
    """
    print("Phase 1: Starting Login...")
    page.goto(
        "https://www.t-mobile.com/my-account/dashboard",
        timeout=90000,
        wait_until="domcontentloaded",
    )
    time.sleep(3)
    _dismiss_cookie_banner(page)

    # The T-Mobile sign-in field is labelled "Email or phone number". Its
    # name/id is NOT reliably "username" (the old assumption never surfaced
    # because a valid session always skipped login), so match it broadly:
    # by autocomplete/identifier names, type=email, and the visible placeholder.
    username_sel = (
        'input[name="username"], #username, input[autocomplete="username"], '
        'input[name="identifier"], input[type="email"], '
        'input[placeholder*="Email" i], input[placeholder*="phone" i]'
    )
    # Decide login state only after a definitive signal renders: the sign-in
    # field (=> must log in) or the dashboard "View bill" control (=> already in).
    try:
        page.wait_for_selector(
            username_sel
            + ', a:has-text("View bill"), button:has-text("View bill")',
            timeout=25000,
        )
    except Exception:
        pass
    username_locator = page.locator(username_sel)
    try:
        field_count = username_locator.count()
    except Exception:
        field_count = 0
    print(f"Login-state check: url={page.url} | sign-in fields found={field_count}")

    if field_count == 0:
        print("Already logged in via persistent profile. Skipping login flow.")
        return

    print("Sign-in form detected; performing login + push 2FA flow...")
    u_field = username_locator.first
    u_field.wait_for(state="visible", timeout=20000)
    u_field.fill(t_user)
    print(f"Entered T-Mobile username into the sign-in field (len={len(t_user)}).")

    print("Clicking Next...")
    page.locator(
        'button:has-text("Next"), button:has-text("Log in"), '
        'button[type="submit"]'
    ).first.click()

    # Capture the screen AFTER username+Next so the post-username flow is
    # visible (in logs + the failure-alert screenshot) even if a later step's
    # selector still needs updating to match the current page.
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    time.sleep(2)
    try:
        page.screenshot(path="after_username_next.png")
        print("Captured post-username screen -> after_username_next.png")
    except Exception:
        pass
    print(f"After Next: url={page.url}")

    print("Looking for Face ID / Fingerprint (passwordless) option...")
    f_btn = page.locator(
        'button:has-text("Continue with Face ID/Fingerprint")'
    )
    f_btn.wait_for(state="visible", timeout=15000)
    f_btn.click()

    print("Waiting for 'Check the notification' push prompt...")
    n_msg = page.locator("text=/Check the notification/i")
    n_msg.wait_for(state="visible", timeout=30000)

    print("Sending 2FA alert email (approve the push on your phone)...")
    send_2fa_alert()

    page.wait_for_url("**/my-account/dashboard**", timeout=300000)
    print("Successfully reached the dashboard!")
    _dismiss_cookie_banner(page)


def _navigate_to_bill_page(page) -> None:
    """Click 'View bill' from the dashboard and wait for /bill/summary."""
    _dismiss_overlays(page)
    v_link = page.locator(
        'a:has-text("View bill"), button:has-text("View bill")'
    ).first
    v_link.wait_for(state="visible", timeout=30000)
    v_link.click()
    page.wait_for_url("**/bill/summary**", timeout=60000)
    time.sleep(5)
    # Dismiss any overlays that appear on the bill summary page (MoEngage etc.)
    _dismiss_overlays(page)


REJECT_BUTTON_PATTERN = re.compile(r"^(Reject|Decline|Reject All)$", re.IGNORECASE)
ACCEPT_BUTTON_PATTERN = re.compile(r"^(Accept|Accept All)$", re.IGNORECASE)
DECLINE_NOTIFY_PATTERN = re.compile(
    r"^(Don.{0,2} allow|No thanks|Not now|Maybe later|Dismiss)$",
    re.IGNORECASE,
)


def _dismiss_overlays(page) -> None:
    """Dismiss cookie banners, notification opt-in modals, and similar overlays.

    T-Mobile shows multiple stacked overlays on every fresh navigation:
    - OneTrust / 'T-Mobile Notice' cookie banner
    - MoEngage 'stay up to date with notifications' modal (id="moengage-optin-id")
    Each one's dark overlay intercepts pointer events on everything beneath.
    Run this before every click that might be intercepted; it's a no-op when
    no overlay is visible.
    """
    # 1. OneTrust cookie banner
    cookie_selectors = [
        "#onetrust-reject-all-handler",
        "#onetrust-accept-btn-handler",
        ".ot-pc-refuse-all-handler",
        'button:has-text("Reject all")',
        'button:has-text("Reject All")',
        'button:has-text("Accept all")',
        'button:has-text("Accept All")',
        'button[aria-label*="reject" i]',
    ]
    for sel in cookie_selectors:
        try:
            btn = page.locator(sel).filter(visible=True).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=3000)
                print(f"Dismissed cookie banner via {sel}")
                time.sleep(1)
                break
        except Exception:
            continue

    # 2. T-Mobile uses 'Reject' / 'Accept' (without 'All') in some banners.
    for pattern, label in (
        (REJECT_BUTTON_PATTERN, "Reject"),
        (ACCEPT_BUTTON_PATTERN, "Accept"),
    ):
        try:
            btn = (
                page.get_by_role("button", name=pattern)
                .filter(visible=True)
                .first
            )
            if btn.is_visible(timeout=1500):
                btn.click(timeout=3000)
                print(f"Dismissed cookie banner via exact '{label}' button.")
                time.sleep(1)
                break
        except Exception:
            continue

    # 3. MoEngage / generic notification opt-in modal - click 'Don't allow'
    try:
        btn = (
            page.get_by_role("button", name=DECLINE_NOTIFY_PATTERN)
            .filter(visible=True)
            .first
        )
        if btn.is_visible(timeout=1500):
            btn.click(timeout=3000)
            print("Dismissed notification opt-in modal.")
            time.sleep(1)
    except Exception:
        pass

    # 4. Last resort: scoped click on MoEngage modal explicitly by ID
    try:
        moengage_btn = (
            page.locator('#moengage-optin-id button')
            .filter(visible=True)
            .last  # 'Don't allow' is typically the second/last button
        )
        if moengage_btn.is_visible(timeout=800):
            moengage_btn.click(timeout=3000)
            print("Dismissed MoEngage modal via #moengage-optin-id selector.")
            time.sleep(1)
    except Exception:
        pass


# Backwards-compatible alias - keep until we sweep all callers
_dismiss_cookie_banner = _dismiss_overlays


def _read_posted_date(page) -> str | None:
    """Extract 'Bill posted MM/DD/YYYY' from the bill summary page."""
    try:
        body_text = page.locator("body").inner_text(timeout=10000)
    except Exception as e:
        print(f"Could not read bill page text: {e}")
        return None
    posted = _parse_posted_date(body_text)
    if posted:
        print(f"Bill posted date detected: {posted}")
    else:
        print("Could not detect 'Bill posted MM/DD/YYYY' on the page.")
    return posted


def _download_pdf(page) -> str:
    """Click the download button, save PDF to ~/Downloads, return path."""
    # Defensive: re-dismiss any overlay that crept in since the page loaded.
    _dismiss_overlays(page)

    # The 'Download my bill (PDF)' label sometimes resolves to a non-clickable
    # inner div (aria-hidden=true). Click the enclosing clickable row instead.
    d_btn = (
        page.locator(
            'a:has-text("Download my bill (PDF)"), '
            'button:has-text("Download my bill (PDF)"), '
            '[role="button"]:has-text("Download my bill (PDF)")'
        )
        .filter(visible=True)
        .first
    )
    try:
        d_btn.wait_for(state="visible", timeout=15000)
    except Exception:
        # Fallback to the bare text locator
        d_btn = page.locator('text="Download my bill (PDF)"').first
        d_btn.wait_for(state="visible", timeout=15000)
    d_btn.click()

    s_btn = page.get_by_text("Download summary bill", exact=True).first
    s_btn.wait_for(state="visible", timeout=30000)
    time.sleep(5)

    print("Clicking 'Download summary bill'...")
    try:
        with page.expect_download(timeout=120000) as d_info:
            s_btn.focus()
            s_btn.dispatch_event("mousedown")
            time.sleep(0.1)
            s_btn.dispatch_event("mouseup")
            s_btn.click(delay=100, force=True)
        download = d_info.value
    except Exception:
        print("Trying fallback evaluate...")
        with page.expect_download(timeout=60000) as d_fallback:
            s_btn.evaluate(
                "el => el.dispatchEvent(new MouseEvent('click', "
                "{bubbles: true, cancelable: true, view: window}))"
            )
        download = d_fallback.value

    d_folder = os.path.expanduser("~/Downloads")
    fname = f"SummaryBill_{time.strftime('%Y%m%d')}.pdf"
    dest = os.path.join(d_folder, fname)
    download.save_as(dest)
    print(f"Success! Bill saved to: {dest}")
    return dest


def download_tmobile_bill(known_posted_date: str | None = None) -> dict:
    """Drive the full download flow.

    If known_posted_date is provided and matches what the portal shows, return
    early with status="not_new" so the caller skips downstream processing.

    Returns one of:
      {"status": "not_new", "posted_date": "..."}
      {"status": "ok", "posted_date": "...", "pdf_path": "...", "pdf_sha256": "..."}
      {"status": "error", "error": "...", "screenshot": "..."}
    """
    t_user = os.getenv("TMOBILE_USER") or get_keychain_password("TMobile_User")
    t_pass = os.getenv("TMOBILE_PASS") or get_keychain_password("TMobile_Pass")

    if not t_user:
        return {"status": "error", "error": "TMOBILE_USER not in env or Keychain"}
    if not t_pass:
        print(
            "Warning: TMOBILE_PASS not found. Biometric login fallback may fail."
        )

    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        u_agent = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        # Persistent context: cookies survive across runs so T-Mobile's
        # "remember device" can skip 2FA after the first successful login.
        context = p.chromium.launch_persistent_context(
            user_data_dir=BROWSER_PROFILE_DIR,
            headless=False,
            slow_mo=500,
            viewport={"width": 1280, "height": 800},
            user_agent=u_agent,
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        Stealth().apply_stealth_sync(page)

        try:
            _login(page, t_user)
        except Exception as e:
            print(f"Error during Phase 1 Login: {e}")
            screenshot = "login_error.png"
            try:
                page.screenshot(path=screenshot)
            except Exception:
                screenshot = None
            context.close()
            return {
                "status": "error",
                "error": f"Login failed: {e}",
                "screenshot": screenshot,
            }

        try:
            _navigate_to_bill_page(page)
        except Exception as e:
            print(f"Error navigating to bill page: {e}")
            screenshot = "nav_error.png"
            try:
                page.screenshot(path=screenshot)
            except Exception:
                screenshot = None
            context.close()
            return {
                "status": "error",
                "error": f"Bill page nav failed: {e}",
                "screenshot": screenshot,
            }

        posted_date = _read_posted_date(page)

        if (
            posted_date
            and known_posted_date
            and posted_date == known_posted_date
        ):
            print(
                f"Bill posted date {posted_date} matches state. No new bill."
            )
            context.close()
            return {"status": "not_new", "posted_date": posted_date}

        try:
            pdf_path = _download_pdf(page)
        except Exception as e:
            print(f"Error during download: {e}")
            screenshot = "download_error.png"
            try:
                page.screenshot(path=screenshot)
            except Exception:
                screenshot = None
            context.close()
            return {
                "status": "error",
                "error": f"Download failed: {e}",
                "screenshot": screenshot,
            }

        context.close()

    return {
        "status": "ok",
        "posted_date": posted_date,
        "pdf_path": pdf_path,
        "pdf_sha256": _sha256(pdf_path),
    }


if __name__ == "__main__":
    result = download_tmobile_bill()
    print(f"Result: {result}")
