import os
import sys
import tempfile
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
