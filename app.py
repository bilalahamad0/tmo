"""Stage-aware orchestrator for the T-Mobile bill -> Zelle pipeline.

The pipeline is:
  1. Detect new bill on the T-Mobile portal (skip if already processed)
  2. Download the PDF (or reuse a path passed on the CLI)
  3. Parse the PDF (resilient to page-index variation)
  4. Email the bill summary
  5. Safety-gate Zelle (amount cap, recipient match, idempotency)
  6. Send Zelle via BoA Playwright automation (live)
  7. Email the payment confirmation

State for the current month is stored at ~/.tmo_state/bill_<YYYY-MM>.json.
Any uncaught failure routes to send_failure_alert() and exits non-zero.
"""
import glob
import json
import os
import re
import subprocess
import sys
import traceback

from pypdf import PdfReader
from dotenv import load_dotenv

import notify
import sms_utils
import state as state_mod
from security_utils import get_env_or_keychain

load_dotenv()


def find_latest_bill() -> str | None:
    """Finds the most recently downloaded T-Mobile bill in ~/Downloads."""
    downloads_folder = os.path.expanduser("~/Downloads")
    patterns = [
        os.path.join(downloads_folder, "SummaryBill*.pdf"),
        os.path.join(downloads_folder, "*T-Mobile*.pdf"),
    ]
    candidates = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    if not candidates:
        return None
    return max(candidates, key=os.path.getctime)


def _extract_bill_text(pdf_path: str) -> list[str]:
    """Concatenate text from all PDF pages.

    The original parser hardcoded reader.pages[1]; if T-Mobile shifts pages
    (cover page, ads), that breaks. Walking every page and looking for the
    Totals/Account markers is much more resilient.
    """
    reader = PdfReader(pdf_path)
    all_lines: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        all_lines.extend(text.split("\n"))
    return all_lines


_PLAN_TOTAL_RE = re.compile(
    r"\bPlan\b[\s\S]{0,80}?\$?(\d+(?:,\d{3})*\.\d{2})",
    re.IGNORECASE,
)


def _extract_plan_total(text_lines: list[str]) -> float | None:
    """Find the 'Plan' subtotal on a T-Mobile bill PDF.

    T-Mobile bills break down into Plan / Equipment / Services /
    Taxes & fees / One-time charges / Bill total. The 'Plan' subtotal is
    what most users think of as their recurring monthly amount.
    Returns the float or None if not detected.
    """
    for line in text_lines:
        m = _PLAN_TOTAL_RE.search(line)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if 20 <= val <= 5000:
                    return val
            except ValueError:
                continue
    joined = "\n".join(text_lines)
    for m in _PLAN_TOTAL_RE.finditer(joined):
        try:
            val = float(m.group(1).replace(",", ""))
            if 20 <= val <= 5000:
                return val
        except ValueError:
            continue
    return None


def _extract_month_name(text_lines: list[str]) -> str:
    """Find a 'MonthName, YYYY' line. Falls back to current month if not found.

    Recognises both full ('April') and abbreviated ('Apr') month names, and
    handles 'Apr 04, 2026'-style lines where month + day share parts[0].
    """
    import calendar
    from datetime import datetime as _dt

    months = {m.lower() for m in calendar.month_name if m}
    months |= {m.lower() for m in calendar.month_abbr if m}
    for line in text_lines:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        first = parts[0].strip()
        if not first:
            continue
        first_word = first.split()[0].lower()
        if first_word in months:
            return f"{first},{parts[1][:5]}"
    return _dt.now().strftime("%B, %Y")


def parse_bill(pdf_path: str) -> dict | None:
    """Extracts month, total, and per-line charges from the PDF."""
    print(f"Parsing bill: {pdf_path}")

    mapping_str = os.getenv("USER_MAPPING")
    if not mapping_str:
        print(
            "Error: USER_MAPPING not found in .env. "
            "Please define it as a JSON string."
        )
        return None
    try:
        name_mapping = json.loads(mapping_str)
    except Exception as e:
        print(f"Error parsing USER_MAPPING JSON from .env: {e}")
        return None

    try:
        text_lines = _extract_bill_text(pdf_path)
        month_name = _extract_month_name(text_lines)

        # Note: we no longer rely on parsing 'Totals' from the PDF text -
        # T-Mobile bills have multiple 'Totals' sub-lines and the last one
        # often isn't the grand total. Instead, total_bill is computed as
        # the sum of all line amounts + base charge below (total_check).
        base_charge = 0.0
        for ele in text_lines:
            if "Account $" in ele:
                try:
                    base_charge = float(ele.split()[-1][1:])
                except ValueError:
                    pass

        counter = 0
        final_dict: dict[str, float] = {}
        for line in text_lines:
            if "Credits" in line and "&" in line and "adjustments" in line:
                continue
            if "(" in line:
                temp_ = line.split()
                if len(temp_) >= 2:
                    phone_number = temp_[0] + temp_[1]
                    try:
                        bill = float(temp_[-1][1:])
                        final_dict[phone_number] = bill
                        counter += 1
                    except ValueError:
                        pass
            if counter > len(name_mapping.keys()) - 1:
                break

        if not final_dict:
            print("Could not parse individual line details.")
            return None

        members = len(final_dict)
        member_base_charge = base_charge / members if members else 0.0

        summary: list[str] = []
        structured_summary: list[dict] = []
        total_check = 0.0
        special_amount = 0.0

        pool_env = os.getenv("SPECIAL_POOL_NAMES", "")
        special_pool_names = [n.strip() for n in pool_env.split(",") if n.strip()]

        for phone, bill in final_dict.items():
            bill += member_base_charge
            total_check += bill
            name = name_mapping.get(phone, "Unknown")
            if special_pool_names and name in special_pool_names:
                special_amount += bill
            summary.append(f"{name} {phone}: \t${bill:.2f}")
            structured_summary.append(
                {"name": name, "phone": phone, "amount": bill}
            )

        # total_bill is the carrier total (sum of all line items including base).
        # plan_total is the T-Mobile 'Plan' subtotal parsed from the PDF, which
        # is what users typically mean by 'monthly plan cost' (excludes one-time
        # charges, taxes, services). Falls back to total when not detected.
        # total_bill is stored without a $ prefix so HTML templates can render
        # '${total_bill}' without producing $$X.XX.
        plan_total = _extract_plan_total(text_lines)
        if plan_total is None:
            plan_total = total_check
        output = {
            "month_name": month_name,
            "total_bill": f"{total_check:.2f}",
            "plan_total": f"{plan_total:.2f}",
            "header": f"{month_name}: \t ${total_check:.2f}",
            "summary": summary,
            "structured_summary": sorted(
                structured_summary, key=lambda x: x["name"]
            ),
            "total_calc": f"Total Bill: \t ${total_check:.2f}",
            "total_amount": total_check,
            "special_title": os.getenv("SPECIAL_POOL_TITLE", "Special Pool"),
            "special_desc": os.getenv(
                "SPECIAL_POOL_DESC", "Included custom coverage"
            ),
            "special_amount": special_amount,
        }
        st = output["special_title"]
        sd = output["special_desc"]
        output["special"] = f"{st}\n{sd}: \t${special_amount:.2f}"
        print("Bill parsed successfully.")
        return output
    except Exception as e:
        print(f"An error occurred during PDF parsing: {e}")
        return None


def _zelle_safety_gate(
    special_amount: float, st: dict, force: bool = False
) -> tuple[bool, str]:
    """Hard preconditions before Zelle. Returns (ok, reason_if_not).

    force=True bypasses the 'previously attempted' guard, but NEVER bypasses
    the 'already confirmed' guard - to retry after a confirmed payment, you
    must manually delete ~/.tmo_state/bill_<YYYY-MM>.json.
    """
    if st.get("zelle_confirmed_at"):
        return False, (
            f"Zelle already confirmed for this month at "
            f"{st['zelle_confirmed_at']} (id={st.get('zelle_confirmation_id')}); "
            f"refusing to re-pay. Delete the state file to override."
        )
    if (
        st.get("zelle_attempted_at")
        and not st.get("zelle_confirmed_at")
        and not force
    ):
        return False, (
            f"Zelle was attempted at {st['zelle_attempted_at']} but no "
            f"confirmation was captured. Refusing to retry automatically. "
            f"Pass --force to override after manually verifying no payment went through."
        )
    if special_amount <= 0:
        return False, "special_amount is 0 or negative; nothing to pay."

    cap_str = get_env_or_keychain("ZELLE_AMOUNT_CAP", "ZELLE_AMOUNT_CAP")
    try:
        cap = float(cap_str) if cap_str else 300.0
    except ValueError:
        cap = 300.0
    if special_amount > cap:
        return False, (
            f"special_amount ${special_amount:.2f} exceeds cap ${cap:.2f}. "
            f"Manual review required."
        )

    expected_recipient = get_env_or_keychain(
        "ZELLE_RECIPIENT_NAME", "ZELLE_RECIPIENT_NAME"
    )
    if not expected_recipient:
        return False, "ZELLE_RECIPIENT_NAME not configured (env or Keychain)."

    return True, ""


def _trigger_zelle(amount: float) -> dict:
    """Run zelle_pay.py as a subprocess; parse JSON result from last stdout line."""
    python_exe = sys.executable
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zelle_pay.py")
    proc = subprocess.run(
        [python_exe, script, f"{amount:.2f}"],
        capture_output=True,
        text=True,
    )
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)

    result = {"status": "error", "error": "no JSON result emitted by zelle_pay.py"}
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                result = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    if proc.returncode != 0 and result.get("status") != "error":
        result = {
            "status": "error",
            "error": f"zelle_pay.py exited {proc.returncode}",
        }
    return result


def _run_pipeline(
    year_month: str, explicit_pdf: str | None, force: bool = False
) -> int:
    """The full stage-by-stage pipeline. Returns process exit code.

    force=True re-runs the email and Zelle stages even if state shows they
    already happened, with one exception: a confirmed Zelle is never re-sent
    automatically (delete the state file to truly reset).
    """
    st = state_mod.load_state(year_month)

    # Stage 0: T-Mobile SMS pre-check.
    # If T-Mobile hasn't sent a 'Your bill is ready' SMS in the last 14 days,
    # don't bother logging in (avoids wasted MFA pushes). Bypassed when an
    # explicit PDF is provided or --force is set.
    #
    # Idempotency is gated on actual COMPLETION signals (zelle_confirmed_at),
    # NOT on having seen the SMS - otherwise a mid-pipeline failure would
    # mark the bill 'processed' and prevent retry on the next scheduled run.
    if not explicit_pdf and not force:
        if st.get("zelle_confirmed_at"):
            print(
                f"Stage 0: Bill for {year_month} already paid at "
                f"{st['zelle_confirmed_at']} "
                f"(id={st.get('zelle_confirmation_id')}). Exiting."
            )
            return 0
        sms = sms_utils.find_tmobile_bill_sms(within_days=14)
        if sms is None:
            print(
                "Stage 0: No T-Mobile 'bill is ready' SMS in last 14 days. "
                "Exiting cleanly (no MFA push required)."
            )
            return 0
        print(
            f"Stage 0: T-Mobile bill SMS found from {sms['iso_date']} "
            f"(balance: ${sms.get('balance')}, sender: {sms.get('sender')})."
        )
        # Save SMS info for cross-validation; not used as an idempotency gate.
        st = state_mod.update_state(
            year_month,
            bill_sms_date=sms["iso_date"],
            bill_sms_balance=sms.get("balance"),
        )

    # Stage 1+2: download (or reuse explicit PDF)
    if explicit_pdf:
        latest_bill = explicit_pdf
        print(f"Using explicitly provided file: {latest_bill}")
    else:
        if st.get("pdf_path") and os.path.exists(st["pdf_path"]):
            latest_bill = st["pdf_path"]
            print(f"Reusing PDF from state: {latest_bill}")
        else:
            print("Stage 1+2: Detecting new bill and downloading...")
            from download_bill import download_tmobile_bill

            result = download_tmobile_bill(
                known_posted_date=st.get("bill_posted_date")
            )
            if result["status"] == "not_new":
                print("No new bill posted yet. Exiting cleanly.")
                return 0
            if result["status"] != "ok":
                err = result.get("error", "unknown")
                screenshot = result.get("screenshot")
                notify.send_failure_alert(
                    "download", err, [screenshot] if screenshot else []
                )
                return 2
            latest_bill = result["pdf_path"]
            st = state_mod.update_state(
                year_month,
                bill_posted_date=result.get("posted_date"),
                pdf_path=result["pdf_path"],
                pdf_sha256=result["pdf_sha256"],
            )

    if not latest_bill or not os.path.exists(latest_bill):
        notify.send_failure_alert(
            "download", f"PDF not found at {latest_bill!r}"
        )
        return 2

    # Stage 3: parse
    print("Stage 3: Parsing PDF...")
    bill_data = parse_bill(latest_bill)
    if not bill_data:
        notify.send_failure_alert(
            "parse", f"Could not parse {latest_bill}", [latest_bill]
        )
        return 3

    st = state_mod.update_state(
        year_month,
        parsed_total=bill_data.get("total_amount"),
        special_amount=bill_data.get("special_amount"),
    )

    separator = "\n" + "#" * 30 + "\n"
    console_output = separator.join(
        [
            bill_data["header"],
            "\n".join(bill_data["summary"]),
            bill_data["total_calc"],
            bill_data["special"],
        ]
    )
    print(console_output)
    print(separator)

    # Stage 4: bill summary email (max 1 send unless --force)
    if force or not st.get("summary_emailed_at"):
        print("Stage 4: Sending bill summary email...")
        ok = notify.send_bill_summary_email(bill_data, latest_bill)
        if ok:
            st = state_mod.update_state(
                year_month, summary_emailed_at=state_mod.now_iso()
            )
        else:
            notify.send_failure_alert(
                "summary_email", "send_bill_summary_email returned False"
            )
    else:
        print(
            f"Bill summary already emailed at {st['summary_emailed_at']}. "
            f"Skipping (use --force to resend)."
        )

    # Stage 5: safety gate (max 1 Zelle attempt unless --force)
    special_amount = float(bill_data.get("special_amount", 0.0) or 0.0)
    ok, reason = _zelle_safety_gate(special_amount, st, force=force)
    if not ok:
        print(f"Zelle safety gate: {reason}")
        if special_amount > 0 and not st.get("zelle_confirmed_at"):
            notify.send_failure_alert("zelle_gate", reason)
        return 0

    # Stage 6: Zelle send (live or dry-run)
    zelle_live = os.getenv("ZELLE_LIVE_SEND", "0").strip() == "1"
    print(
        f"Stage 6: Triggering Zelle Payment of ${special_amount:.2f} "
        f"(live={zelle_live})..."
    )
    # Only mark attempt for LIVE runs - dry-runs don't actually send and
    # shouldn't pollute the state file with false-positive 'attempted' flags.
    if zelle_live:
        st = state_mod.update_state(
            year_month, zelle_attempted_at=state_mod.now_iso()
        )
    result = _trigger_zelle(special_amount)
    status = result.get("status")

    if status == "dry_run":
        print(
            f"Zelle dry-run completed. Screenshot: {result.get('screenshot')}. "
            f"Set ZELLE_LIVE_SEND=1 to enable live send."
        )
        return 0

    if status not in ("sent", "sent_unconfirmed"):
        err = result.get("error", "unknown")
        screenshot = result.get("screenshot")
        notify.send_failure_alert(
            "zelle_send", err, [screenshot] if screenshot else []
        )
        return 6

    confirmation_id = result.get("confirmation_id")
    screenshot = result.get("screenshot")
    recipient_name = result.get("recipient_name") or get_env_or_keychain(
        "ZELLE_RECIPIENT_NAME", "ZELLE_RECIPIENT_NAME"
    )

    # 'sent_unconfirmed': the live Pay click happened but zelle_pay saw NEITHER
    # a confirmation id NOR BoA's 'payment sent' banner. The money may or may
    # not have moved, so do NOT write zelle_confirmed_at (that would falsely
    # lock the month as paid with no proof). Flag it for manual verification
    # and alert. zelle_attempted_at is already set, so the safety gate refuses
    # any AUTOMATIC retry - a human must check Bank of America, then either
    # delete the state file (to retry) or treat it as paid.
    #
    # NOTE: we branch on status ALONE, not on `not confirmation_id`. When BoA
    # shows "Your payment is sent." but the reference number can't be scraped,
    # zelle_pay returns status='sent' with confirmation_id=None - a SUCCESS, not
    # a failure. Conflating the two here is what fired a false 'zelle_unconfirmed'
    # alert on a payment that had actually gone through.
    if status == "sent_unconfirmed":
        st = state_mod.update_state(
            year_month,
            zelle_unconfirmed_at=state_mod.now_iso(),
            zelle_screenshot=screenshot,
        )
        msg = (
            "Zelle Pay was clicked but NO confirmation of the payment could be "
            "captured (no reference number and no 'payment sent' banner). The "
            "payment may or may not have completed - MANUAL verification in "
            "Bank of America is required before the next run. Automatic retry "
            "is blocked to avoid a double payment. To retry after confirming "
            "no payment went through, delete "
            f"~/.tmo_state/bill_{year_month}.json."
        )
        print(msg)
        notify.send_failure_alert(
            "zelle_unconfirmed", msg, [screenshot] if screenshot else []
        )
        return 6

    # status == 'sent': the payment is confirmed sent - via a captured
    # confirmation id and/or BoA's 'payment sent' banner. A missing reference
    # number is not a failure; marking the month paid also HARD-LOCKS it
    # against a duplicate payment on the next run (zelle_confirmation_id may be
    # None here, which dashboard.py renders as 'PAID (no confirmation id)').
    st = state_mod.update_state(
        year_month,
        zelle_confirmed_at=state_mod.now_iso(),
        zelle_confirmation_id=confirmation_id,
        zelle_screenshot=screenshot,
    )
    if confirmation_id:
        print(f"Zelle send succeeded. Confirmation: {confirmation_id}")
    else:
        print(
            "Zelle send succeeded (BoA 'payment sent' banner detected; "
            "reference number not captured). Marked paid to prevent a "
            "duplicate payment."
        )

    # Stage 7: confirmation email
    print("Stage 7: Sending confirmation email...")
    notify.send_confirmation_email(
        amount=special_amount,
        recipient_name=recipient_name,
        confirmation_id=confirmation_id,
        month_name=bill_data.get("month_name", year_month),
        screenshot_path=screenshot,
    )
    return 0


def main() -> int:
    print("Starting T-Mobile Local Bill Processor (stage-aware)...")

    args = sys.argv[1:]
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    explicit_pdf = args[0] if args else None
    year_month = state_mod.current_year_month()

    if force:
        print("--force flag set: idempotency guards relaxed for this run.")

    try:
        with state_mod.month_lock(year_month) as acquired:
            if not acquired:
                print(
                    "Another run is already processing this month "
                    f"({year_month}); exiting to avoid concurrent or "
                    "duplicate Zelle payment."
                )
                return 0
            return _run_pipeline(year_month, explicit_pdf, force=force)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"Pipeline crashed: {e}\n{tb}")
        notify.send_failure_alert("pipeline_crash", f"{e}\n\n{tb}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
