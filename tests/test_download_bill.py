"""Unit tests for download_bill's pure logic: the bill-posted date regex, the
cookie/notification button matchers used to dismiss T-Mobile's stacked
overlays, and the PDF content hash. The Playwright navigation itself is out of
scope (live portal only)."""
import hashlib
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import download_bill  # noqa: E402


# --------------------------------------------------------------------------
# BILL_POSTED_PATTERN
# --------------------------------------------------------------------------

def test_bill_posted_pattern_captures_date_parts():
    m = download_bill.BILL_POSTED_PATTERN.search("Bill posted 05/04/2026 ok")
    assert m.groups() == ("05", "04", "2026")


def test_bill_posted_pattern_is_case_and_space_insensitive():
    m = download_bill.BILL_POSTED_PATTERN.search("BILL   POSTED 4/5/2026")
    assert m.groups() == ("4", "5", "2026")


def test_bill_posted_pattern_no_match():
    assert download_bill.BILL_POSTED_PATTERN.search("Autopay scheduled") is None


# --------------------------------------------------------------------------
# Overlay-dismissal button matchers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["Reject", "Decline", "Reject All", "reject all"])
def test_reject_pattern_matches(label):
    assert download_bill.REJECT_BUTTON_PATTERN.match(label) is not None


@pytest.mark.parametrize("label", ["Reject cookies", "Rejected", "Decliner"])
def test_reject_pattern_rejects_partial(label):
    assert download_bill.REJECT_BUTTON_PATTERN.match(label) is None


@pytest.mark.parametrize("label", ["Accept", "Accept All", "accept all"])
def test_accept_pattern_matches(label):
    assert download_bill.ACCEPT_BUTTON_PATTERN.match(label) is not None


@pytest.mark.parametrize("label", ["Accepted", "Accept cookies"])
def test_accept_pattern_rejects_partial(label):
    assert download_bill.ACCEPT_BUTTON_PATTERN.match(label) is None


@pytest.mark.parametrize(
    "label",
    ["Don't allow", "Dont allow", "No thanks", "Not now", "Maybe later", "Dismiss"],
)
def test_decline_notify_pattern_matches(label):
    assert download_bill.DECLINE_NOTIFY_PATTERN.match(label) is not None


@pytest.mark.parametrize("label", ["Allow", "Yes", "Enable notifications"])
def test_decline_notify_pattern_rejects_affirmatives(label):
    assert download_bill.DECLINE_NOTIFY_PATTERN.match(label) is None


# --------------------------------------------------------------------------
# _sha256
# --------------------------------------------------------------------------

def test_sha256_matches_hashlib(tmp_path):
    content = b"%PDF-1.4 some bill bytes\nwith a few lines\n"
    f = tmp_path / "bill.pdf"
    f.write_bytes(content)

    assert download_bill._sha256(str(f)) == hashlib.sha256(content).hexdigest()


def test_sha256_differs_for_different_content(tmp_path):
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"content A")
    b.write_bytes(b"content B")

    assert download_bill._sha256(str(a)) != download_bill._sha256(str(b))
