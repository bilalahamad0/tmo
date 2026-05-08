import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

import app  # noqa: E402
import download_bill  # noqa: E402


SAMPLE_PAGE_TEXT = """
\nApril, 2026 statement
\nLine details
\nAccount $20.00
\nAlice (1234567890) Talk Charges $30.00
\nBob (0987654321) Talk Charges $40.00
\nTotals 90.00
"""


def test_extract_month_name_finds_month_line():
    lines = SAMPLE_PAGE_TEXT.split("\n")
    assert app._extract_month_name(lines).startswith("April,")


def test_extract_month_name_falls_back_to_current():
    result = app._extract_month_name(["no month here", "or here"])
    assert "," in result


def test_extract_month_name_handles_abbreviation_with_day():
    """T-Mobile bills often start with 'Apr 04, 2026'. The original code only
    matched full month names, mislabeling April bills as the current month."""
    lines = ["Some header", "Apr 04, 2026", "more content"]
    result = app._extract_month_name(lines)
    assert result.startswith("Apr 04")


def test_extract_month_name_handles_abbreviation_alone():
    lines = ["Mar, 2026"]
    result = app._extract_month_name(lines)
    assert result.startswith("Mar")


def test_parse_bill_finds_totals_across_pages():
    """parse_bill should walk every page for Totals/Account, not page index 1."""
    page0 = MagicMock()
    page0.extract_text.return_value = "Cover page only"
    page1 = MagicMock()
    page1.extract_text.return_value = SAMPLE_PAGE_TEXT
    fake_reader = MagicMock()
    fake_reader.pages = [page0, page1]

    mapping = json.dumps(
        {"Alice(1234567890)": "Alice", "Bob(0987654321)": "Bob"}
    )
    with patch.dict(
        os.environ,
        {
            "USER_MAPPING": mapping,
            "SPECIAL_POOL_NAMES": "Alice",
            "SPECIAL_POOL_TITLE": "Premium",
            "SPECIAL_POOL_DESC": "Coverage",
        },
        clear=False,
    ), patch("app.PdfReader", return_value=fake_reader):
        result = app.parse_bill("/fake/path.pdf")

    assert result is not None
    # total_bill is now the COMPUTED sum, formatted without $ prefix so HTML
    # templates can prepend it without producing $$.
    assert result["total_bill"] == "90.00"
    # base $20 split between 2 members = $10 each
    # Alice base $30 + 10 share = $40; Bob $40 + 10 = $50; total_calc = 90
    assert abs(result["total_amount"] - 90.0) < 0.01
    assert abs(result["special_amount"] - 40.0) < 0.01
    # Header shows the carrier total (computed from line items + base).
    assert "$90.00" in result["header"]
    # plan_total falls back to total_check when no 'Plan' subtotal found.
    assert result["plan_total"] == "90.00"


def test_parse_bill_resilient_when_page0_is_empty():
    """If T-Mobile shifts the bill content to a different page, we still parse."""
    blank = MagicMock()
    blank.extract_text.return_value = ""
    bill_page = MagicMock()
    bill_page.extract_text.return_value = SAMPLE_PAGE_TEXT
    fake_reader = MagicMock()
    fake_reader.pages = [blank, blank, bill_page]

    mapping = json.dumps(
        {"Alice(1234567890)": "Alice", "Bob(0987654321)": "Bob"}
    )
    with patch.dict(
        os.environ, {"USER_MAPPING": mapping}, clear=False
    ), patch("app.PdfReader", return_value=fake_reader):
        result = app.parse_bill("/fake/path.pdf")

    assert result is not None
    assert result["total_bill"] == "90.00"


def test_parse_bill_returns_none_on_missing_user_mapping():
    with patch.dict(os.environ, {"USER_MAPPING": ""}, clear=False):
        os.environ.pop("USER_MAPPING", None)
        assert app.parse_bill("/fake/path.pdf") is None


def test_parse_posted_date_extracts_iso_string():
    text = "Some header\nBill posted 04/30/2026\nMore content"
    assert download_bill._parse_posted_date(text) == "2026-04-30"


def test_parse_posted_date_handles_single_digit_month_day():
    text = "Bill posted 4/5/2026"
    assert download_bill._parse_posted_date(text) == "2026-04-05"


def test_parse_posted_date_returns_none_when_absent():
    assert download_bill._parse_posted_date("nothing relevant") is None


def test_parse_posted_date_returns_none_on_invalid_date():
    text = "Bill posted 13/45/2026"
    assert download_bill._parse_posted_date(text) is None


def test_extract_plan_total_finds_inline_amount():
    """T-Mobile bills sometimes have 'Plan $157.42' inline."""
    lines = ["Header", "Plan $157.42", "Other content"]
    assert app._extract_plan_total(lines) == 157.42


def test_extract_plan_total_finds_amount_on_following_line():
    """Plan label and amount may be on adjacent lines."""
    lines = ["Plan", "$157.42", "Account $18.00"]
    assert app._extract_plan_total(lines) == 157.42


def test_extract_plan_total_returns_none_when_absent():
    lines = ["Bill total $207.38", "Taxes & fees $37.42"]
    assert app._extract_plan_total(lines) is None


def test_extract_plan_total_skips_unreasonable_values():
    """A 'Plan $5.00' is below sanity threshold (probably a credit, not the
    plan subtotal); should be rejected so we fall back to computed total."""
    lines = ["Plan $5.00"]
    assert app._extract_plan_total(lines) is None
