import json
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import dashboard  # noqa: E402
import state as state_mod  # noqa: E402


def _write(d, month, data):
    (d / f"bill_{month}.json").write_text(json.dumps(data))


# --------------------------------------------------------------------------
# classify()
# --------------------------------------------------------------------------

def test_classify_paid_with_confirmation():
    assert dashboard.classify(
        {"zelle_confirmed_at": "x", "zelle_confirmation_id": "ABC123"}
    ) == dashboard.STATUS_PAID


def test_classify_paid_without_confirmation_id():
    assert dashboard.classify(
        {"zelle_confirmed_at": "x", "zelle_confirmation_id": None}
    ) == dashboard.STATUS_PAID_NO_ID


def test_classify_unconfirmed():
    assert dashboard.classify(
        {"zelle_unconfirmed_at": "x"}
    ) == dashboard.STATUS_UNCONFIRMED


def test_classify_attempted():
    assert dashboard.classify(
        {"zelle_attempted_at": "x"}
    ) == dashboard.STATUS_ATTEMPTED


def test_classify_emailed():
    assert dashboard.classify(
        {"summary_emailed_at": "x"}
    ) == dashboard.STATUS_EMAILED


def test_classify_parsed():
    assert dashboard.classify({"parsed_total": 100.0}) == dashboard.STATUS_PARSED


def test_classify_pending():
    assert dashboard.classify({}) == dashboard.STATUS_PENDING


# --------------------------------------------------------------------------
# load_transactions()
# --------------------------------------------------------------------------

def test_load_transactions_sorted_newest_first_and_skips_bad(tmp_path):
    _write(tmp_path, "2026-05", {"parsed_total": 207.38, "special_amount": 102.43,
                                 "zelle_confirmed_at": "t", "zelle_confirmation_id": "C1"})
    _write(tmp_path, "2026-06", {"parsed_total": 187.42})
    (tmp_path / "bill_2026-07.json").write_text("not json {{{")     # skipped
    (tmp_path / "bill_2026-08.json").write_text('["a", "list"]')    # non-dict, skipped
    (tmp_path / "ignore.json").write_text("{}")                     # wrong prefix

    recs = dashboard.load_transactions(state_dir=tmp_path)

    assert [r["month"] for r in recs] == ["2026-06", "2026-05"]
    assert recs[0]["status"] == dashboard.STATUS_PARSED
    assert recs[1]["status"] == dashboard.STATUS_PAID


def test_load_transactions_uses_state_dir_default(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path)
    _write(tmp_path, "2026-05", {"parsed_total": 10.0})
    recs = dashboard.load_transactions()
    assert len(recs) == 1 and recs[0]["month"] == "2026-05"


# --------------------------------------------------------------------------
# summarize() / _money()
# --------------------------------------------------------------------------

def test_summarize_counts_and_totals():
    recs = [
        {"status": dashboard.STATUS_PAID, "special_amount": 100.0},
        {"status": dashboard.STATUS_PAID_NO_ID, "special_amount": 50.0},
        {"status": dashboard.STATUS_UNCONFIRMED, "special_amount": 0},
        {"status": dashboard.STATUS_PENDING},
    ]
    s = dashboard.summarize(recs)
    assert s["months"] == 4
    assert s["paid_count"] == 2
    assert s["paid_total"] == 150.0
    assert s["attention"] == 2  # PAID_NO_ID + UNCONFIRMED


def test_money_formatting_and_fallback():
    assert dashboard._money(1234.5) == "$1,234.50"
    assert dashboard._money(None) == "—"
    assert dashboard._money("oops") == "—"


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def test_render_html_contains_data_and_summary():
    recs = [
        {"month": "2026-05", "parsed_total": 207.38, "special_amount": 102.43,
         "status": dashboard.STATUS_PAID, "zelle_confirmation_id": "CONF1",
         "zelle_confirmed_at": "2026-05-07T16:57:15"},
    ]
    out = dashboard.render_html(recs, "2026-06-08T17:00:00")
    assert "<!DOCTYPE html>" in out
    assert "2026-05" in out
    assert "$207.38" in out
    assert "$102.43" in out
    assert "CONF1" in out
    assert "2026-06-08T17:00:00" in out


def test_render_html_escapes_and_handles_empty():
    out = dashboard.render_html([], "now")
    assert "No transactions recorded yet." in out


def test_render_html_escapes_malicious_confirmation_id():
    recs = [{"month": "2026-05", "status": dashboard.STATUS_PAID,
             "zelle_confirmation_id": "<script>alert(1)</script>"}]
    out = dashboard.render_html(recs, "now")
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


def test_render_text_table_and_footer():
    recs = [{"month": "2026-05", "parsed_total": 207.38, "special_amount": 102.43,
             "status": dashboard.STATUS_PAID}]
    txt = dashboard.render_text(recs)
    assert "2026-05" in txt
    assert "1 month(s)" in txt
    assert "1 paid" in txt


# --------------------------------------------------------------------------
# main()
# --------------------------------------------------------------------------

def test_main_writes_html_and_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path)
    _write(tmp_path, "2026-05", {"parsed_total": 207.38, "special_amount": 102.43,
                                 "zelle_confirmed_at": "t", "zelle_confirmation_id": "C1"})
    out_html = tmp_path / "dash.html"

    rc = dashboard.main([str(out_html)])

    assert rc == 0
    assert out_html.exists()
    content = out_html.read_text()
    assert "2026-05" in content and "$102.43" in content
    captured = capsys.readouterr()
    assert "2026-05" in captured.out
    assert "Dashboard written to" in captured.out
