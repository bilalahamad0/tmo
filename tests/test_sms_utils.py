import os
import sqlite3
import sys
import time
from unittest.mock import MagicMock, patch

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

import sms_utils  # noqa: E402


SAMPLE_TMO_SMS = (
    "T-Mobile: Your bill for your account ending in XXXX is ready. "
    "Your balance due is $207.38. AutoPay is scheduled for 05/23/2026 "
    "using the payment method on file. Check out your member savings..."
)


def _ns_minus(seconds: int) -> int:
    """Return chat.db-style date_ns for now minus N seconds."""
    return int((time.time() - seconds - sms_utils.COCOA_EPOCH_OFFSET) * 1e9)


def test_tmobile_bill_re_matches_real_sms():
    assert sms_utils.TMOBILE_BILL_RE.search(SAMPLE_TMO_SMS) is not None


def test_tmobile_bill_re_rejects_unrelated_message():
    assert sms_utils.TMOBILE_BILL_RE.search("BofA: do not share this code") is None
    assert sms_utils.TMOBILE_BILL_RE.search("Your Amazon order shipped") is None


def test_balance_re_extracts_amount():
    m = sms_utils.BALANCE_RE.search(SAMPLE_TMO_SMS)
    assert m is not None
    assert float(m.group(1).replace(",", "")) == 207.38


def test_find_tmobile_bill_sms_returns_match():
    fake_rows = [(SAMPLE_TMO_SMS, _ns_minus(3600), "2535")]
    with patch.object(sms_utils, "_read_messages", return_value=fake_rows):
        result = sms_utils.find_tmobile_bill_sms(within_days=14)
    assert result is not None
    assert result["balance"] == 207.38
    assert result["sender"] == "2535"
    assert result["iso_date"]
    assert "T-Mobile" in result["text"]


def test_find_tmobile_bill_sms_returns_none_when_no_match():
    fake_rows = [
        ("BofA: do not share this code 123456", _ns_minus(60), "73981"),
        ("Amazon: your order has shipped", _ns_minus(120), "262966"),
    ]
    with patch.object(sms_utils, "_read_messages", return_value=fake_rows):
        result = sms_utils.find_tmobile_bill_sms(within_days=14)
    assert result is None


def test_find_tmobile_bill_sms_returns_most_recent_match():
    """When multiple T-Mobile bill SMSes exist, return the newest (rows are
    fed in DESC order by date, so the first match wins)."""
    fake_rows = [
        (SAMPLE_TMO_SMS.replace("207.38", "299.99"), _ns_minus(3600), "2535"),
        (SAMPLE_TMO_SMS, _ns_minus(86400 * 30), "2535"),
    ]
    with patch.object(sms_utils, "_read_messages", return_value=fake_rows):
        result = sms_utils.find_tmobile_bill_sms(within_days=60)
    assert result["balance"] == 299.99


def test_find_otp_code_uses_first_six_digit_match():
    fake_rows = [
        ("BoA: Your code is 654321. Do not share.", _ns_minus(30), "73981"),
    ]
    with patch.object(sms_utils, "_read_messages", return_value=fake_rows):
        with patch.object(sms_utils.time, "sleep"):
            code = sms_utils.find_otp_code(retries=1, delay_seconds=0)
    assert code == "654321"


def test_find_otp_code_returns_none_when_no_code():
    fake_rows = [
        ("This message has no code", _ns_minus(30), "12345"),
    ]
    with patch.object(sms_utils, "_read_messages", return_value=fake_rows):
        with patch.object(sms_utils.time, "sleep"):
            code = sms_utils.find_otp_code(retries=2, delay_seconds=0)
    assert code is None


def test_is_fda_error():
    import sqlite3
    assert sms_utils._is_fda_error(
        sqlite3.OperationalError("unable to open database file")
    )
    assert sms_utils._is_fda_error(
        sqlite3.OperationalError("authorization denied")
    )
    assert not sms_utils._is_fda_error(
        sqlite3.OperationalError("syntax error")
    )


def test_decode_attributed_body_extracts_longest_run():
    """attributedBody blobs on Big Sur+ are typedstream-encoded; the longest
    printable-ASCII run is overwhelmingly the message text itself."""
    fake_blob = (
        b"\x04\x0bstreamtyped"
        b"\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString"
        b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString"
        b"\x01\x95\x84\x01+"
        + bytes([len(SAMPLE_TMO_SMS)])
        + SAMPLE_TMO_SMS.encode("utf-8")
        + b"\x86\x84\x02iI"
    )
    decoded = sms_utils._decode_attributed_body(fake_blob)
    assert decoded is not None
    assert "T-Mobile" in decoded
    assert "$207.38" in decoded


def test_decode_attributed_body_returns_none_for_empty():
    assert sms_utils._decode_attributed_body(None) is None
    assert sms_utils._decode_attributed_body(b"") is None
    assert sms_utils._decode_attributed_body(b"\x00\x01\x02\x03") is None


# --------------------------------------------------------------------------
# _read_messages against a real (tiny) chat.db, exercising the actual SQL,
# the date-window filter, the LEFT JOIN to handle, and the attributedBody
# fallback - plus the FDA / DB error paths.
# --------------------------------------------------------------------------

def _make_chat_db(path, rows):
    """rows: list of (text, date_ns, handle_id, attributedBody)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
    conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '2535')")
    conn.execute(
        "CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, "
        "date INTEGER, handle_id INTEGER, attributedBody BLOB)"
    )
    conn.executemany(
        "INSERT INTO message (text, date, handle_id, attributedBody) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_read_messages_returns_rows_within_window(tmp_path, monkeypatch):
    db = tmp_path / "chat.db"
    long_body = "This is a sufficiently long message body for decoding"
    blob = b"\x01\x02" + long_body.encode("utf-8") + b"\x00\x00"
    rows = [
        ("plain recent text", _ns_minus(60), 1, None),
        (None, _ns_minus(120), 1, blob),       # text comes from attributedBody
        ("too old to include", _ns_minus(100000), 1, None),  # outside window
        (None, _ns_minus(30), 1, None),        # excluded by SQL (both NULL)
    ]
    _make_chat_db(str(db), rows)
    monkeypatch.setattr(sms_utils, "DB_PATH", str(db))

    result = sms_utils._read_messages(within_seconds=3600)

    texts = [r[0] for r in result]
    assert "plain recent text" in texts
    assert any("sufficiently long message body" in t for t in texts)
    assert "too old to include" not in texts
    # Sender id resolved via the LEFT JOIN to the handle table.
    assert all(r[2] == "2535" for r in result)


def test_read_messages_empty_when_db_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sms_utils, "DB_PATH", str(tmp_path / "absent.db"))
    assert sms_utils._read_messages(within_seconds=3600) == []


def test_read_messages_handles_fda_permission_error(tmp_path, monkeypatch):
    db = tmp_path / "chat.db"
    db.write_bytes(b"")  # exists, so we reach _connect
    monkeypatch.setattr(sms_utils, "DB_PATH", str(db))
    monkeypatch.setattr(
        sms_utils,
        "_connect",
        MagicMock(
            side_effect=sqlite3.OperationalError("unable to open database file")
        ),
    )
    assert sms_utils._read_messages(within_seconds=3600) == []


def test_read_messages_handles_query_error(tmp_path, monkeypatch):
    db = tmp_path / "chat.db"
    db.write_bytes(b"")
    monkeypatch.setattr(sms_utils, "DB_PATH", str(db))

    class _BadConn:
        def cursor(self):
            raise sqlite3.OperationalError("no such table: message")

        def close(self):
            pass

    monkeypatch.setattr(sms_utils, "_connect", MagicMock(return_value=_BadConn()))
    assert sms_utils._read_messages(within_seconds=3600) == []


def test_find_tmobile_bill_sms_end_to_end_via_db(tmp_path, monkeypatch):
    """Drive find_tmobile_bill_sms through the real _read_messages + SQL."""
    db = tmp_path / "chat.db"
    _make_chat_db(str(db), [(SAMPLE_TMO_SMS, _ns_minus(300), 1, None)])
    monkeypatch.setattr(sms_utils, "DB_PATH", str(db))

    result = sms_utils.find_tmobile_bill_sms(within_days=14)

    assert result is not None
    assert result["balance"] == 207.38
    assert result["sender"] == "2535"
