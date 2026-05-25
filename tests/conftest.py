"""Shared fixtures for the test suite.

These keep the unit and feature tests hermetic: state writes go to a temp
directory (never the real ~/.tmo_state), SMTP credentials are fake, and no
test ever touches the real macOS Keychain, Messages chat.db, a browser, or
a live SMTP server.
"""
import os
import sys

import pytest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

import state as state_mod  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point the state module at a throwaway directory for the test."""
    d = tmp_path / ".tmo_state"
    monkeypatch.setattr(state_mod, "STATE_DIR", d)
    return d


@pytest.fixture
def smtp_env(monkeypatch):
    """Minimal SMTP config so notify.* functions attempt a (mocked) send."""
    monkeypatch.setenv("SMTP_EMAIL", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("RECIPIENT_EMAILS", "a@example.com,b@example.com")
    # Clear optional overrides so individual tests start from a known state.
    for var in (
        "FAILURE_ALERT_EMAIL",
        "CONFIRMATION_RECIPIENTS",
        "MFA_ALERT_EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_pdf(tmp_path):
    """A real file on disk so os.path.exists() checks pass. Contents are not
    parsed (parse_bill is mocked in the feature tests)."""
    p = tmp_path / "SummaryBill_20260504.pdf"
    p.write_bytes(b"%PDF-1.4 fake bill content")
    return str(p)


@pytest.fixture
def bill_data():
    """Canned parse_bill() output used to drive the orchestration tests."""
    return {
        "month_name": "May, 2026",
        "total_bill": "207.38",
        "plan_total": "157.42",
        "header": "May, 2026: \t $207.38",
        "summary": ["Alice (1234567890): \t$102.43"],
        "structured_summary": [
            {"name": "Alice", "phone": "(1234567890)", "amount": 102.43}
        ],
        "total_calc": "Total Bill: \t $207.38",
        "total_amount": 207.38,
        "special_title": "Premium Pool",
        "special_desc": "Coverage for Alice",
        "special_amount": 102.43,
        "special": "Premium Pool\nCoverage for Alice: \t$102.43",
    }
