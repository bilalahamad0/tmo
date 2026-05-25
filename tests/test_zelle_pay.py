"""Unit tests for the pure, non-browser logic in zelle_pay.py.

The Playwright flow itself can only run against a live Bank of America session
(see README "Tests"), but the surrounding decision logic - the live-send flag,
the confirmation-ID regexes, the final-action button matcher, and the OTP
delegation - is deterministic and tested here.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import zelle_pay  # noqa: E402


# --------------------------------------------------------------------------
# _live_send_enabled: the gate between dry-run and moving real money
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        (" 1 ", True),  # stripped
        ("0", False),
        ("", False),
        ("true", False),  # only the literal "1" enables live send
        ("2", False),
    ],
)
def test_live_send_enabled(monkeypatch, value, expected):
    monkeypatch.setenv("ZELLE_LIVE_SEND", value)
    assert zelle_pay._live_send_enabled() is expected


def test_live_send_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("ZELLE_LIVE_SEND", raising=False)
    assert zelle_pay._live_send_enabled() is False


# --------------------------------------------------------------------------
# CONFIRMATION_PATTERNS: pull a confirmation ID off the BoA confirmation page
# --------------------------------------------------------------------------

def _match_confirmation(text):
    """Mirror the scan in _capture_confirmation: first pattern that hits wins."""
    for pat in zelle_pay.CONFIRMATION_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Your payment is sent. Confirmation # ABC123", "ABC123"),
        ("Confirmation Number: XYZ-789", "XYZ-789"),
        ("Reference #: REF4567 for your records", "REF4567"),
        ("Transaction ID - TX12AB", "TX12AB"),
        ("Confirmation #conf99ab", "conf99ab"),  # IGNORECASE
    ],
)
def test_confirmation_patterns_extract_id(text, expected):
    assert _match_confirmation(text) == expected


def test_confirmation_pattern_ignores_short_ids():
    """The {3,} quantifier rejects 2-3 char noise so we don't latch onto a
    stray 'Confirmation # OK'."""
    assert _match_confirmation("Confirmation # OK") is None


def test_confirmation_pattern_returns_none_when_absent():
    assert _match_confirmation("Payment is on its way. Thank you.") is None


# --------------------------------------------------------------------------
# REVIEW_ACTION_PATTERN: identify the final Pay/Send button exactly
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["Pay", "Send", "Send Now", "pay", "SEND"])
def test_review_action_pattern_matches_final_buttons(label):
    assert zelle_pay.REVIEW_ACTION_PATTERN.match(label) is not None


@pytest.mark.parametrize(
    "label", ["Pay Bilal Ahamad", "Send Money", "Repay", "Send to Alice", "Payment"]
)
def test_review_action_pattern_rejects_non_final_buttons(label):
    """Anchored ^...$ match avoids firing on 'Pay <name>' / 'Send Money' that
    appear on earlier steps of the flow."""
    assert zelle_pay.REVIEW_ACTION_PATTERN.match(label) is None


# --------------------------------------------------------------------------
# get_last_sms_code: thin backwards-compat wrapper over sms_utils
# --------------------------------------------------------------------------

def test_get_last_sms_code_delegates_to_sms_utils(monkeypatch):
    fake = MagicMock(return_value="654321")
    monkeypatch.setattr(zelle_pay.sms_utils, "find_otp_code", fake)

    code = zelle_pay.get_last_sms_code(retries=3, delay_seconds=2)

    assert code == "654321"
    fake.assert_called_once_with(retries=3, delay_seconds=2)


def test_edit_button_keywords_cover_destructive_actions():
    """_find_recipient relies on these to skip Edit/Delete buttons that share
    the recipient's name - lock the contract."""
    for kw in ("edit", "delete", "remove", "manage"):
        assert kw in zelle_pay.EDIT_BUTTON_KEYWORDS
