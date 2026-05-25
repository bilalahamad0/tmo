import os
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

import notify  # noqa: E402


def _smtp_env(monkeypatch=None, **overrides):
    """Set required SMTP env for the duration of a test."""
    base = {
        "SMTP_EMAIL": "sender@example.com",
        "SMTP_PASSWORD": "pw",
        "RECIPIENT_EMAILS": "a@example.com,b@example.com",
    }
    base.update(overrides)
    patches = [patch.dict(os.environ, base, clear=False)]
    return patches


def _make_bill_data():
    return {
        "month_name": "April, 2026",
        "total_bill": "150.00",
        "header": "April, 2026: \t 150.00",
        "summary": ["Alice (1234567890): \t$50.00"],
        "structured_summary": [
            {"name": "Alice", "phone": "(1234567890)", "amount": 50.0}
        ],
        "total_calc": "Total Bill: \t $150.00",
        "total_amount": 150.0,
        "special_title": "Special Pool",
        "special_desc": "Coverage",
        "special_amount": 75.0,
        "special": "Special Pool\nCoverage: \t$75.00",
    }


def test_send_bill_summary_email_calls_smtp():
    with patch.dict(
        os.environ,
        {
            "SMTP_EMAIL": "sender@example.com",
            "SMTP_PASSWORD": "pw",
            "RECIPIENT_EMAILS": "a@example.com,b@example.com",
        },
        clear=False,
    ), patch("smtplib.SMTP_SSL") as mock_smtp, tempfile.NamedTemporaryFile(
        suffix=".pdf", delete=False
    ) as f:
        f.write(b"%PDF-1.4 fake")
        f.flush()
        server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = server

        ok = notify.send_bill_summary_email(_make_bill_data(), f.name)

        assert ok is True
        server.login.assert_called_once_with("sender@example.com", "pw")
        server.send_message.assert_called_once()
        sent_msg = server.send_message.call_args[0][0]
        assert "T-Mobile Bill Summary" in sent_msg["Subject"]
        assert sent_msg["To"] == "a@example.com, b@example.com"
        os.unlink(f.name)


def test_send_bill_summary_email_skips_when_no_recipients():
    with patch.dict(
        os.environ,
        {
            "SMTP_EMAIL": "sender@example.com",
            "SMTP_PASSWORD": "pw",
            "RECIPIENT_EMAILS": "",
        },
        clear=False,
    ), patch("smtplib.SMTP_SSL") as mock_smtp:
        ok = notify.send_bill_summary_email(_make_bill_data(), "/nonexistent.pdf")
        assert ok is False
        mock_smtp.assert_not_called()


def test_send_confirmation_email_includes_amount_and_id():
    with patch.dict(
        os.environ,
        {
            "SMTP_EMAIL": "sender@example.com",
            "SMTP_PASSWORD": "pw",
            "RECIPIENT_EMAILS": "ops@example.com",
        },
        clear=False,
    ), patch("smtplib.SMTP_SSL") as mock_smtp:
        server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = server

        ok = notify.send_confirmation_email(
            amount=75.50,
            recipient_name="Test Recipient",
            confirmation_id="CONF12345",
            month_name="April, 2026",
            screenshot_path=None,
        )

        assert ok is True
        sent_msg = server.send_message.call_args[0][0]
        assert "75.50" in sent_msg["Subject"]
        assert "Test Recipient" in sent_msg["Subject"]
        body = sent_msg.get_body(preferencelist=("plain",)).get_content()
        assert "CONF12345" in body
        assert "April, 2026" in body


def test_send_failure_alert_uses_failure_recipient():
    with patch.dict(
        os.environ,
        {
            "SMTP_EMAIL": "sender@example.com",
            "SMTP_PASSWORD": "pw",
            "RECIPIENT_EMAILS": "ops@example.com",
            "FAILURE_ALERT_EMAIL": "alerts@example.com",
        },
        clear=False,
    ), patch("smtplib.SMTP_SSL") as mock_smtp:
        server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = server

        ok = notify.send_failure_alert(
            stage="zelle_send", error="MFA timeout"
        )

        assert ok is True
        sent_msg = server.send_message.call_args[0][0]
        assert sent_msg["To"] == "alerts@example.com"
        assert "[ALERT]" in sent_msg["Subject"]
        assert "zelle_send" in sent_msg["Subject"]


def test_send_failure_alert_attaches_screenshot():
    with patch.dict(
        os.environ,
        {
            "SMTP_EMAIL": "sender@example.com",
            "SMTP_PASSWORD": "pw",
            "RECIPIENT_EMAILS": "ops@example.com",
        },
        clear=False,
    ), patch("smtplib.SMTP_SSL") as mock_smtp, tempfile.NamedTemporaryFile(
        suffix=".png", delete=False
    ) as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.flush()
        server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = server

        notify.send_failure_alert("download", "boom", [f.name])

        sent_msg = server.send_message.call_args[0][0]
        attachments = list(sent_msg.iter_attachments())
        assert len(attachments) == 1
        assert attachments[0].get_filename() == Path(f.name).name
        os.unlink(f.name)


# --------------------------------------------------------------------------
# _smtp_config / _failure_alert_recipients
# --------------------------------------------------------------------------

def test_smtp_config_strips_and_drops_empty(monkeypatch):
    monkeypatch.setenv("SMTP_EMAIL", "s@x.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("RECIPIENT_EMAILS", " a@x.com , b@x.com ,")
    sender, password, recipients = notify._smtp_config()
    assert sender == "s@x.com"
    assert password == "pw"
    assert recipients == ["a@x.com", "b@x.com"]


def test_failure_alert_recipients_uses_explicit(monkeypatch, smtp_env):
    monkeypatch.setenv("FAILURE_ALERT_EMAIL", "alerts@x.com")
    assert notify._failure_alert_recipients() == ["alerts@x.com"]


def test_failure_alert_recipients_falls_back_to_first(smtp_env):
    # smtp_env sets RECIPIENT_EMAILS=a,b and clears FAILURE_ALERT_EMAIL.
    assert notify._failure_alert_recipients() == ["a@example.com"]


def test_failure_alert_recipients_empty_when_none(monkeypatch):
    monkeypatch.delenv("FAILURE_ALERT_EMAIL", raising=False)
    monkeypatch.setenv("RECIPIENT_EMAILS", "")
    assert notify._failure_alert_recipients() == []


# --------------------------------------------------------------------------
# _attach_files
# --------------------------------------------------------------------------

def test_attach_files_skips_missing_and_none(tmp_path):
    msg = EmailMessage()
    notify._attach_files(msg, [str(tmp_path / "nope.pdf"), None, ""])
    assert list(msg.iter_attachments()) == []


def test_attach_files_uses_octet_stream_for_unknown_ext(tmp_path):
    f = tmp_path / "automation.log"
    f.write_text("log content")
    msg = EmailMessage()
    notify._attach_files(msg, [str(f)])
    att = list(msg.iter_attachments())[0]
    assert att.get_content_type() == "application/octet-stream"
    assert att.get_filename() == "automation.log"


def test_attach_files_uses_jpeg_subtype(tmp_path):
    f = tmp_path / "shot.jpg"
    f.write_bytes(b"\xff\xd8\xff\xe0")
    msg = EmailMessage()
    notify._attach_files(msg, [str(f)])
    att = list(msg.iter_attachments())[0]
    assert att.get_content_type() == "image/jpeg"


# --------------------------------------------------------------------------
# _build_summary_html
# --------------------------------------------------------------------------

def test_build_summary_html_includes_key_fields():
    html = notify._build_summary_html(_make_bill_data())
    assert "April, 2026" in html
    assert "Alice" in html
    assert "Special Pool" in html
    assert "$50.00" in html   # per-line amount
    assert "$75.00" in html   # special-pool amount
    assert "$150.00" in html  # calculated split total


# --------------------------------------------------------------------------
# _send error path
# --------------------------------------------------------------------------

def test_send_returns_false_on_smtp_error():
    msg = EmailMessage()
    msg["Subject"] = "x"
    with patch("smtplib.SMTP_SSL", side_effect=Exception("connection refused")):
        assert notify._send(msg, "s@x.com", "pw") is False


# --------------------------------------------------------------------------
# send_confirmation_email recipient override + skip
# --------------------------------------------------------------------------

def test_confirmation_email_honors_confirmation_recipients(monkeypatch, smtp_env):
    monkeypatch.setenv("CONFIRMATION_RECIPIENTS", "ops1@x.com, ops2@x.com")
    with patch("smtplib.SMTP_SSL") as mock_smtp:
        server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = server
        ok = notify.send_confirmation_email(50.0, "R", "C1", "April, 2026")
    assert ok is True
    sent = server.send_message.call_args[0][0]
    assert sent["To"] == "ops1@x.com, ops2@x.com"


def test_confirmation_email_skips_without_creds(monkeypatch):
    monkeypatch.delenv("SMTP_EMAIL", raising=False)
    monkeypatch.setenv("RECIPIENT_EMAILS", "")
    with patch("smtplib.SMTP_SSL") as mock_smtp:
        assert notify.send_confirmation_email(1.0, "R", "C", "M") is False
        mock_smtp.assert_not_called()


# --------------------------------------------------------------------------
# send_failure_alert skip
# --------------------------------------------------------------------------

def test_failure_alert_skips_without_recipients(monkeypatch):
    monkeypatch.setenv("SMTP_EMAIL", "s@x.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    monkeypatch.setenv("RECIPIENT_EMAILS", "")
    monkeypatch.delenv("FAILURE_ALERT_EMAIL", raising=False)
    with patch("smtplib.SMTP_SSL") as mock_smtp:
        assert notify.send_failure_alert("stage", "err") is False
        mock_smtp.assert_not_called()


# --------------------------------------------------------------------------
# send_2fa_alert
# --------------------------------------------------------------------------

def test_send_2fa_alert_uses_mfa_email(monkeypatch, smtp_env):
    monkeypatch.setenv("MFA_ALERT_EMAIL", "mfa@x.com")
    with patch("smtplib.SMTP_SSL") as mock_smtp:
        server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = server
        ok = notify.send_2fa_alert()
    assert ok is True
    sent = server.send_message.call_args[0][0]
    assert sent["To"] == "mfa@x.com"
    assert "ACTION REQUIRED" in sent["Subject"]


def test_send_2fa_alert_falls_back_to_failure_recipient(monkeypatch, smtp_env):
    monkeypatch.delenv("MFA_ALERT_EMAIL", raising=False)
    with patch("smtplib.SMTP_SSL") as mock_smtp:
        server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = server
        ok = notify.send_2fa_alert()
    assert ok is True
    sent = server.send_message.call_args[0][0]
    assert sent["To"] == "a@example.com"


def test_send_2fa_alert_returns_false_without_creds(monkeypatch):
    monkeypatch.delenv("SMTP_EMAIL", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.setenv("RECIPIENT_EMAILS", "")
    monkeypatch.delenv("MFA_ALERT_EMAIL", raising=False)
    assert notify.send_2fa_alert() is False
