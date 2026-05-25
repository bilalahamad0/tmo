"""Unit tests for app's non-pipeline helpers: the zelle_pay subprocess bridge,
the Downloads-folder bill finder, and PDF text extraction."""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app  # noqa: E402


# --------------------------------------------------------------------------
# _trigger_zelle: parses the JSON result zelle_pay.py emits on its last line
# --------------------------------------------------------------------------

def _fake_proc(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_trigger_parses_json_from_last_line(monkeypatch):
    out = "Starting...\nNavigating...\n" '{"status": "sent", "confirmation_id": "C1"}\n'
    monkeypatch.setattr(app.subprocess, "run", MagicMock(return_value=_fake_proc(out)))

    result = app._trigger_zelle(102.43)

    assert result == {"status": "sent", "confirmation_id": "C1"}


def test_trigger_picks_last_json_line_when_multiple(monkeypatch):
    out = '{"status": "dry_run"}\nmore logs\n{"status": "sent", "confirmation_id": "C2"}'
    monkeypatch.setattr(app.subprocess, "run", MagicMock(return_value=_fake_proc(out)))

    result = app._trigger_zelle(10.0)

    assert result["status"] == "sent"
    assert result["confirmation_id"] == "C2"


def test_trigger_ignores_non_json_noise(monkeypatch):
    out = "{ this is not json }\n" '{"status": "dry_run", "screenshot": "x.png"}'
    monkeypatch.setattr(app.subprocess, "run", MagicMock(return_value=_fake_proc(out)))

    result = app._trigger_zelle(5.0)

    assert result["status"] == "dry_run"


def test_trigger_returns_error_when_no_json_emitted(monkeypatch):
    monkeypatch.setattr(
        app.subprocess, "run", MagicMock(return_value=_fake_proc("just logs, no json"))
    )

    result = app._trigger_zelle(5.0)

    assert result["status"] == "error"
    assert "no JSON" in result["error"]


def test_trigger_nonzero_exit_overrides_success_status(monkeypatch):
    """If the script claims success but exits non-zero, treat it as an error -
    we must not record a confirmation off a crashed run."""
    out = '{"status": "sent", "confirmation_id": "C3"}'
    monkeypatch.setattr(
        app.subprocess, "run", MagicMock(return_value=_fake_proc(out, returncode=1))
    )

    result = app._trigger_zelle(5.0)

    assert result["status"] == "error"
    assert "exited 1" in result["error"]


def test_trigger_nonzero_exit_keeps_explicit_error(monkeypatch):
    out = '{"status": "error", "error": "MFA timeout"}'
    monkeypatch.setattr(
        app.subprocess, "run", MagicMock(return_value=_fake_proc(out, returncode=1))
    )

    result = app._trigger_zelle(5.0)

    assert result["status"] == "error"
    assert result["error"] == "MFA timeout"


def test_trigger_skips_invalid_json_line_and_uses_earlier_valid(monkeypatch):
    """The last brace-line is malformed; the scan must skip it (JSONDecodeError)
    and fall back to the earlier valid result line."""
    out = '{"status": "sent", "confirmation_id": "C9"}\n{ invalid json }'
    monkeypatch.setattr(app.subprocess, "run", MagicMock(return_value=_fake_proc(out)))

    result = app._trigger_zelle(5.0)

    assert result["status"] == "sent"
    assert result["confirmation_id"] == "C9"


def test_trigger_echoes_stderr(monkeypatch, capsys):
    proc = _fake_proc('{"status": "dry_run"}', stderr="a warning from zelle_pay")
    monkeypatch.setattr(app.subprocess, "run", MagicMock(return_value=proc))

    app._trigger_zelle(5.0)

    assert "a warning from zelle_pay" in capsys.readouterr().err


def test_trigger_formats_amount_to_cents(monkeypatch):
    run = MagicMock(return_value=_fake_proc('{"status": "dry_run"}'))
    monkeypatch.setattr(app.subprocess, "run", run)

    app._trigger_zelle(102.4)

    argv = run.call_args[0][0]
    assert argv[-1] == "102.40"
    assert argv[-2].endswith("zelle_pay.py")


# --------------------------------------------------------------------------
# find_latest_bill: newest matching PDF in ~/Downloads
# --------------------------------------------------------------------------

def test_find_latest_bill_returns_newest(monkeypatch, tmp_path):
    monkeypatch.setattr(app.os.path, "expanduser", lambda p: str(tmp_path))
    older = tmp_path / "SummaryBill_20260401.pdf"
    newer = tmp_path / "SummaryBill_20260504.pdf"
    tmo = tmp_path / "April-T-Mobile-bill.pdf"
    for f in (older, newer, tmo):
        f.write_bytes(b"%PDF")

    ctimes = {str(older): 100.0, str(newer): 300.0, str(tmo): 200.0}
    monkeypatch.setattr(app.os.path, "getctime", lambda p: ctimes[p])

    assert app.find_latest_bill() == str(newer)


def test_find_latest_bill_matches_tmobile_pattern(monkeypatch, tmp_path):
    monkeypatch.setattr(app.os.path, "expanduser", lambda p: str(tmp_path))
    tmo = tmp_path / "My-T-Mobile-statement.pdf"
    tmo.write_bytes(b"%PDF")
    monkeypatch.setattr(app.os.path, "getctime", lambda p: 1.0)

    assert app.find_latest_bill() == str(tmo)


def test_find_latest_bill_returns_none_when_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(app.os.path, "expanduser", lambda p: str(tmp_path))

    assert app.find_latest_bill() is None


# --------------------------------------------------------------------------
# _extract_bill_text: concatenate text across all PDF pages
# --------------------------------------------------------------------------

def test_extract_bill_text_walks_all_pages(monkeypatch):
    page0 = MagicMock()
    page0.extract_text.return_value = "line A\nline B"
    page1 = MagicMock()
    page1.extract_text.return_value = "line C"
    reader = MagicMock()
    reader.pages = [page0, page1]
    monkeypatch.setattr(app, "PdfReader", MagicMock(return_value=reader))

    lines = app._extract_bill_text("/fake.pdf")

    assert lines == ["line A", "line B", "line C"]


def test_extract_bill_text_tolerates_empty_page(monkeypatch):
    """A page whose extract_text returns None must not crash extraction."""
    blank = MagicMock()
    blank.extract_text.return_value = None
    page = MagicMock()
    page.extract_text.return_value = "real content"
    reader = MagicMock()
    reader.pages = [blank, page]
    monkeypatch.setattr(app, "PdfReader", MagicMock(return_value=reader))

    lines = app._extract_bill_text("/fake.pdf")

    assert "real content" in lines
