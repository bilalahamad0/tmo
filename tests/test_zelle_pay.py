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


def test_extract_confirmation_id_from_real_boa_success_text():
    """Regression: the real BoA success page renders the label and the id in
    separate DOM nodes, so the scraped text is 'Confirmation #\\ns1xrlf1ab'.
    This exact payment was falsely reported 'unconfirmed' in production."""
    scraped = (
        "Success\nYour payment is sent.\nTo\nSachin Rath\n"
        "sachinbubun@gmail.com | Enrolled as SACHIN RATH\nFrom\n"
        "Adv Plus Banking - 9348\nAmount\n$89.17\nDate\nJul 07, 2026\n"
        "Confirmation #\ns1xrlf1ab\nSend another payment\nGo to Activity"
    )
    assert zelle_pay._extract_confirmation_id(scraped) == "s1xrlf1ab"


def test_extract_confirmation_id_none_on_empty():
    assert zelle_pay._extract_confirmation_id("") is None
    assert zelle_pay._extract_confirmation_id("Review your payment") is None


# --------------------------------------------------------------------------
# _payment_sent: BoA's 'payment sent' banner is a first-class success signal,
# independent of whether the reference number could be scraped.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Your payment is sent.",          # the real BoA web banner
        "your payment is sent",           # case-insensitive
        "Your payment was sent",
        "Your payment has been sent",
        "Payment successfully sent",
        "Your money is on its way",
    ],
)
def test_payment_sent_detects_success_banner(text):
    assert zelle_pay._payment_sent(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Review your payment",
        "Pay $89.17 to Sachin Rath",   # the Review-screen action button label
        "Enter an amount to send",
    ],
)
def test_payment_sent_false_on_non_success(text):
    assert zelle_pay._payment_sent(text) is False


# --------------------------------------------------------------------------
# _classify_send: id OR banner -> 'sent'; neither -> 'sent_unconfirmed'
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "confirmation_id,payment_sent,expected",
    [
        ("s1xrlf1ab", True, "sent"),           # both signals
        ("s1xrlf1ab", False, "sent"),          # reference number alone
        (None, True, "sent"),                  # success banner alone
        (None, False, "sent_unconfirmed"),     # neither -> ambiguous
    ],
)
def test_classify_send(confirmation_id, payment_sent, expected):
    assert zelle_pay._classify_send(confirmation_id, payment_sent) == expected


# --------------------------------------------------------------------------
# _read_confirmation: reads the id/banner from the (iframe) DOM. The id and
# banner come from inner_text; text_content is a tightly-gated fallback that
# can only flip payment_sent, never store a (possibly mangled) id.
# --------------------------------------------------------------------------

class _FakeLocator:
    def __init__(self, inner, content):
        self._inner, self._content = inner, content

    def inner_text(self, timeout=None):
        if self._inner is None:
            raise RuntimeError("inner_text unavailable")
        return self._inner

    def text_content(self, timeout=None):
        if self._content is None:
            raise RuntimeError("text_content unavailable")
        return self._content


class _FakeFrame:
    def __init__(self, inner, content):
        self._loc = _FakeLocator(inner, content)

    def locator(self, _selector):
        return self._loc


class _FakePage:
    """Frame 0 is the (empty) main frame; frame 1 is BoA's Zelle iframe."""
    def __init__(self, inner, content):
        self.frames = [_FakeFrame("", ""), _FakeFrame(inner, content)]


def test_read_confirmation_clean_success_page():
    """inner_text renders block elements as newlines -> id parses cleanly and
    the banner is detected."""
    inner = (
        "Success\nYour payment is sent.\nAmount\n$89.17\n"
        "Confirmation #\ns1xrlf1ab\nGo to Activity"
    )
    cid, sent = zelle_pay._read_confirmation(_FakePage(inner, "ignored"))
    assert cid == "s1xrlf1ab"
    assert sent is True


def test_read_confirmation_banner_without_id():
    cid, sent = zelle_pay._read_confirmation(
        _FakePage("Your payment is sent.", "ignored")
    )
    assert cid is None
    assert sent is True


def test_read_confirmation_falls_back_to_text_content_when_inner_empty():
    """When inner_text is unreadable (None) the mashed text_content still proves
    a send happened -> payment_sent True, but NO mangled id is stored."""
    mashed = (
        "SuccessYour payment is sent.Amount$89.17"
        "Confirmation #s1xrlf1abSend another payment"
    )
    cid, sent = zelle_pay._read_confirmation(_FakePage(None, mashed))
    assert sent is True
    # The id would mangle to 's1xrlf1abSend' from text_content, so we store none.
    assert cid is None


def test_read_confirmation_does_not_use_text_content_when_inner_readable():
    """If inner_text returns real (non-success) text, the text_content fallback
    must stay OFF - otherwise a stray/hidden 'Confirmation #' could falsely mark
    a payment sent. Ambiguous -> (None, False) -> sent_unconfirmed upstream."""
    cid, sent = zelle_pay._read_confirmation(
        _FakePage("Review your payment", "Confirmation #hidden999")
    )
    assert cid is None
    assert sent is False


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
