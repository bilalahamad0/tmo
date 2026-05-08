"""Read recent SMS from macOS Messages chat.db.

Two consumers:
- find_otp_code: bank 6-digit codes for BoA Zelle MFA
- find_tmobile_bill_sms: T-Mobile 'bill is ready' announcement

REQUIRES Full Disk Access on the python binary (or its parent process,
e.g. Terminal or launchd). Without FDA, sqlite3.connect raises
'unable to open database file' and these functions return None.
"""
import os
import re
import sqlite3
import sys
import time
from datetime import datetime


COCOA_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01
DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")

# T-Mobile sends "Your bill is ready" SMS from short code 2535.
# Match the announcement text; sender match is a bonus signal.
TMOBILE_BILL_RE = re.compile(
    r"T-?Mobile.{0,40}your bill.{0,80}is ready",
    re.IGNORECASE | re.DOTALL,
)
BALANCE_RE = re.compile(
    r"balance\s+due\s+is\s+\$?([\d,]+\.\d{2})", re.IGNORECASE
)


def _connect():
    """Open chat.db read-only. Raises on FDA permission error."""
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def _is_fda_error(err: Exception) -> bool:
    s = str(err).lower()
    return (
        "permission" in s
        or "authorization" in s
        or "unable to open" in s
    )


def _decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract readable text from a Messages attributedBody NSAttributedString blob.

    On Big Sur+ macOS, many messages have NULL `text` and store the actual
    content in `attributedBody` as a typedstream/NSKeyedArchiver blob. Full
    parsing requires a typedstream decoder, but for plain SMS/iMessage text
    we can heuristically extract the longest printable-ASCII run, which is
    typically the message body itself.
    """
    if not blob:
        return None
    try:
        runs: list[str] = []
        current: list[str] = []
        for b in blob:
            if 32 <= b < 127 or b in (9, 10, 13):
                current.append(chr(b))
            else:
                if len(current) > 20:
                    runs.append("".join(current))
                current = []
        if current and len(current) > 20:
            runs.append("".join(current))
        if not runs:
            return None
        # Longest run is overwhelmingly likely to be the message text
        return max(runs, key=len)
    except Exception:
        return None


def _read_messages(within_seconds: int, limit: int = 200) -> list[tuple]:
    """Return [(text, date_ns, sender_id), ...] for messages in the window.

    text is sourced from the `text` column when populated, otherwise decoded
    from `attributedBody`. sender_id is the phone/short-code/email of the
    sender. Returns [] on FDA permission error or DB issues.
    """
    if not os.path.exists(DB_PATH):
        print(f"sms_utils: chat.db not found at {DB_PATH}")
        return []
    cutoff_ns = int(
        (time.time() - within_seconds - COCOA_EPOCH_OFFSET) * 1_000_000_000
    )
    try:
        conn = _connect()
    except sqlite3.OperationalError as e:
        if _is_fda_error(e):
            print(
                f"sms_utils: chat.db PERMISSION DENIED. "
                f"Grant Full Disk Access to {sys.executable} (or its launching app)."
            )
        else:
            print(f"sms_utils: connect failed: {e}")
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT m.text, m.date, h.id, m.attributedBody "
            "FROM message m "
            "LEFT JOIN handle h ON m.handle_id = h.ROWID "
            "WHERE m.date > ? "
            "  AND (m.text IS NOT NULL OR m.attributedBody IS NOT NULL) "
            "ORDER BY m.date DESC LIMIT ?",
            (cutoff_ns, limit),
        )
        rows = []
        for text, date_ns, sender, attr_blob in cur.fetchall():
            if not text:
                text = _decode_attributed_body(attr_blob)
            if not text:
                continue
            rows.append((text, date_ns, sender))
        return rows
    except sqlite3.OperationalError as e:
        print(f"sms_utils: query failed: {e}")
        return []
    finally:
        conn.close()


def find_otp_code(
    retries: int = 6, delay_seconds: int = 5, within_seconds: int = 600
) -> str | None:
    """Find the latest 6-digit code in recent SMS, polling several times."""
    print("sms_utils: scanning for 6-digit OTP code...")
    for attempt in range(1, retries + 1):
        time.sleep(delay_seconds)
        rows = _read_messages(within_seconds=within_seconds)
        for text, _date_ns, _sender in rows:
            if not text:
                continue
            m = re.search(r"\b(\d{6})\b", text)
            if m:
                # Don't log the actual OTP - it ends up in automation.log
                # without any special access protection.
                print(f"sms_utils: OTP captured on attempt {attempt}.")
                return m.group(1)
        print(
            f"sms_utils: no OTP found on attempt {attempt}/{retries}; "
            f"retrying in {delay_seconds}s..."
        )
    return None


def find_tmobile_bill_sms(within_days: int = 14) -> dict | None:
    """Return info about the most recent T-Mobile 'bill is ready' SMS.

    Returns:
      {
        "text": "<truncated SMS body>",
        "iso_datetime": "2026-05-06T18:33:00",
        "iso_date":     "2026-05-06",
        "balance":      207.38 | None,
        "sender":       "2535" | None,
      }
    or None if no matching SMS within the window.
    """
    rows = _read_messages(within_seconds=within_days * 86400, limit=500)
    for text, date_ns, sender in rows:
        if not text or not TMOBILE_BILL_RE.search(text):
            continue
        ts = (date_ns / 1_000_000_000) + COCOA_EPOCH_OFFSET
        dt = datetime.fromtimestamp(ts)
        balance_match = BALANCE_RE.search(text)
        balance = (
            float(balance_match.group(1).replace(",", ""))
            if balance_match
            else None
        )
        return {
            "text": text[:300],
            "iso_datetime": dt.isoformat(timespec="seconds"),
            "iso_date": dt.date().isoformat(),
            "balance": balance,
            "sender": sender,
        }
    return None
