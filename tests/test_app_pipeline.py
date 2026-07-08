"""Feature tests for the end-to-end pipeline orchestration (app._run_pipeline
and app.main).

These exercise the real stage-progression, idempotency, and state-file logic
while stubbing every dangerous boundary: no browser launches, no SMTP sends,
no Keychain reads, no chat.db access, and no zelle_pay subprocess. Each test
asserts both the process exit code and the observable side effects (which
emails fired, whether a Zelle was triggered, what landed in the state file).
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app  # noqa: E402
import download_bill  # noqa: E402
import state as state_mod  # noqa: E402

YM = "2026-05"


@pytest.fixture
def pipeline(monkeypatch, state_dir, fake_pdf, bill_data):
    """Wire up every external boundary as a mock and hand them back so tests
    can tweak return values and assert on calls."""
    m = SimpleNamespace()

    m.parse_bill = MagicMock(return_value=bill_data)
    monkeypatch.setattr(app, "parse_bill", m.parse_bill)

    m.send_summary = MagicMock(return_value=True)
    m.send_confirmation = MagicMock(return_value=True)
    m.send_failure = MagicMock(return_value=True)
    monkeypatch.setattr(app.notify, "send_bill_summary_email", m.send_summary)
    monkeypatch.setattr(app.notify, "send_confirmation_email", m.send_confirmation)
    monkeypatch.setattr(app.notify, "send_failure_alert", m.send_failure)

    m.trigger_zelle = MagicMock(
        return_value={
            "status": "dry_run",
            "screenshot": "zelle_review_dryrun.png",
            "recipient_name": "Real Recipient",
        }
    )
    monkeypatch.setattr(app, "_trigger_zelle", m.trigger_zelle)

    m.config = {"ZELLE_AMOUNT_CAP": "300", "ZELLE_RECIPIENT_NAME": "Real Recipient"}
    monkeypatch.setattr(
        app,
        "get_env_or_keychain",
        lambda env_var, svc, account_name=None: m.config.get(env_var),
    )

    m.find_sms = MagicMock(
        return_value={
            "iso_date": "2026-05-04",
            "balance": 207.38,
            "sender": "2535",
            "text": "T-Mobile: Your bill is ready...",
        }
    )
    monkeypatch.setattr(app.sms_utils, "find_tmobile_bill_sms", m.find_sms)

    m.download = MagicMock(
        return_value={
            "status": "ok",
            "posted_date": "2026-05-04",
            "pdf_path": fake_pdf,
            "pdf_sha256": "deadbeef",
        }
    )
    monkeypatch.setattr(download_bill, "download_tmobile_bill", m.download)

    monkeypatch.setenv("ZELLE_LIVE_SEND", "0")

    m.fake_pdf = fake_pdf
    m.bill_data = bill_data
    return m


def _state():
    return state_mod.load_state(YM)


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------

def test_dry_run_happy_path(pipeline):
    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    assert rc == 0
    pipeline.send_summary.assert_called_once()
    pipeline.trigger_zelle.assert_called_once()
    # Triggered with the special-pool amount, formatted to cents.
    assert pipeline.trigger_zelle.call_args[0][0] == pytest.approx(102.43)
    pipeline.send_confirmation.assert_not_called()

    st = _state()
    assert st["summary_emailed_at"]
    assert st["parsed_total"] == pytest.approx(207.38)
    assert st["special_amount"] == pytest.approx(102.43)
    # Dry-run must not pollute state with attempt/confirm markers.
    assert "zelle_attempted_at" not in st
    assert "zelle_confirmed_at" not in st


def test_live_send_happy_path(pipeline, monkeypatch):
    monkeypatch.setenv("ZELLE_LIVE_SEND", "1")
    pipeline.trigger_zelle.return_value = {
        "status": "sent",
        "confirmation_id": "CONF999",
        "screenshot": "zelle_confirmation_x.png",
        "recipient_name": "Real Recipient",
    }

    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    assert rc == 0
    pipeline.send_summary.assert_called_once()
    pipeline.send_confirmation.assert_called_once()
    kwargs = pipeline.send_confirmation.call_args.kwargs
    assert kwargs["amount"] == pytest.approx(102.43)
    assert kwargs["recipient_name"] == "Real Recipient"
    assert kwargs["confirmation_id"] == "CONF999"

    st = _state()
    assert st["zelle_attempted_at"]
    assert st["zelle_confirmed_at"]
    assert st["zelle_confirmation_id"] == "CONF999"


# --------------------------------------------------------------------------
# Stage 0 gating (no explicit PDF)
# --------------------------------------------------------------------------

def test_stage0_exits_when_already_paid(pipeline):
    state_mod.save_state(YM, {"zelle_confirmed_at": "2026-05-10T09:00:00"})

    rc = app._run_pipeline(YM, explicit_pdf=None, force=False)

    assert rc == 0
    pipeline.find_sms.assert_not_called()
    pipeline.parse_bill.assert_not_called()
    pipeline.trigger_zelle.assert_not_called()


def test_stage0_exits_when_no_sms(pipeline):
    pipeline.find_sms.return_value = None

    rc = app._run_pipeline(YM, explicit_pdf=None, force=False)

    assert rc == 0
    pipeline.find_sms.assert_called_once()
    pipeline.download.assert_not_called()
    pipeline.parse_bill.assert_not_called()


def test_stage0_records_sms_then_downloads_and_proceeds(pipeline):
    rc = app._run_pipeline(YM, explicit_pdf=None, force=False)

    assert rc == 0
    pipeline.find_sms.assert_called_once()
    pipeline.download.assert_called_once()
    st = _state()
    assert st["bill_sms_date"] == "2026-05-04"
    assert st["bill_sms_balance"] == pytest.approx(207.38)
    assert st["pdf_path"] == pipeline.fake_pdf
    assert st["pdf_sha256"] == "deadbeef"


def test_reuses_pdf_from_state_instead_of_downloading(pipeline):
    """If a prior run already downloaded the PDF (state has a valid pdf_path),
    a re-run reuses it rather than driving the portal again."""
    state_mod.save_state(YM, {"pdf_path": pipeline.fake_pdf})

    rc = app._run_pipeline(YM, explicit_pdf=None, force=False)

    assert rc == 0
    pipeline.download.assert_not_called()
    pipeline.parse_bill.assert_called_once()


def test_download_not_new_exits_clean(pipeline):
    pipeline.download.return_value = {"status": "not_new", "posted_date": "2026-05-04"}

    rc = app._run_pipeline(YM, explicit_pdf=None, force=False)

    assert rc == 0
    pipeline.parse_bill.assert_not_called()
    pipeline.trigger_zelle.assert_not_called()


def test_download_error_sends_alert(pipeline):
    pipeline.download.return_value = {
        "status": "error",
        "error": "Login failed",
        "screenshot": "login_error.png",
    }

    rc = app._run_pipeline(YM, explicit_pdf=None, force=False)

    assert rc == 2
    pipeline.send_failure.assert_called_once()
    assert pipeline.send_failure.call_args[0][0] == "download"


# --------------------------------------------------------------------------
# Idempotency / safety gate
# --------------------------------------------------------------------------

def test_refuses_zelle_when_already_confirmed(pipeline):
    state_mod.save_state(YM, {"zelle_confirmed_at": "2026-05-10T09:00:00"})

    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    assert rc == 0
    pipeline.trigger_zelle.assert_not_called()
    pipeline.send_confirmation.assert_not_called()
    # No alert: a confirmed bill is the success state, not a failure.
    pipeline.send_failure.assert_not_called()


def test_over_cap_alerts_and_skips_zelle(pipeline):
    pipeline.config["ZELLE_AMOUNT_CAP"] = "50"  # bill special is 102.43

    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    assert rc == 0
    pipeline.trigger_zelle.assert_not_called()
    pipeline.send_failure.assert_called_once()
    assert pipeline.send_failure.call_args[0][0] == "zelle_gate"


def test_attempted_without_confirm_blocks_without_force(pipeline):
    state_mod.save_state(YM, {"zelle_attempted_at": "2026-05-10T09:00:00"})

    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    assert rc == 0
    pipeline.trigger_zelle.assert_not_called()
    pipeline.send_failure.assert_called_once()
    assert pipeline.send_failure.call_args[0][0] == "zelle_gate"


def test_gate_does_not_realert_when_already_flagged_unconfirmed(pipeline):
    """A month already flagged 'unconfirmed' at Stage 6 (the human was told to
    verify in BoA) must stay blocked but NOT re-send zelle_gate on every
    subsequent run - that repeat is noise, not new information."""
    state_mod.save_state(
        YM,
        {
            "zelle_attempted_at": "2026-05-07T13:00:00",
            "zelle_unconfirmed_at": "2026-05-07T13:01:00",
        },
    )

    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    assert rc == 0
    pipeline.trigger_zelle.assert_not_called()
    pipeline.send_failure.assert_not_called()


def test_gate_alerts_once_then_suppresses_for_bare_attempt(pipeline):
    """A bare stuck attempt (e.g. crash mid-Zelle) alerts on the FIRST blocked
    run and records it, then stays silent on later runs for the same state."""
    state_mod.save_state(YM, {"zelle_attempted_at": "2026-05-07T13:00:00"})

    rc1 = app._run_pipeline(YM, pipeline.fake_pdf, force=False)
    assert rc1 == 0
    assert pipeline.send_failure.call_count == 1
    assert _state().get("zelle_gate_alerted_at")

    rc2 = app._run_pipeline(YM, pipeline.fake_pdf, force=False)
    assert rc2 == 0
    # Still just the one alert from the first run - no daily repeats.
    assert pipeline.send_failure.call_count == 1


def test_over_cap_keeps_alerting_each_run(pipeline):
    """Config-style blocks (over-cap) are NOT deduped - they recur until fixed,
    so each run should still surface them."""
    pipeline.config["ZELLE_AMOUNT_CAP"] = "50"  # bill special is 102.43

    app._run_pipeline(YM, pipeline.fake_pdf, force=False)
    app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    assert pipeline.send_failure.call_count == 2
    assert all(
        c.args[0] == "zelle_gate" for c in pipeline.send_failure.call_args_list
    )


# --------------------------------------------------------------------------
# --mark-paid: reconcile a genuinely-paid-but-stuck month (the SAFE
# alternative to deleting the state file, which would re-pay).
# --------------------------------------------------------------------------

def test_mark_paid_reconciles_stuck_month(state_dir):
    state_mod.save_state(
        YM,
        {
            "zelle_attempted_at": "2026-05-07T13:00:00",
            "zelle_unconfirmed_at": "2026-05-07T13:01:00",
            "zelle_gate_alerted_at": "2026-05-08T09:00:00",
        },
    )

    rc = app._mark_paid_cli(["--mark-paid", "s1xrlf1ab", "--month", YM])

    assert rc == 0
    st = _state()
    assert st["zelle_confirmed_at"]
    assert st["zelle_confirmation_id"] == "s1xrlf1ab"
    assert st["zelle_reconciled_at"] == st["zelle_confirmed_at"]
    # Limbo flags cleared so the gate/dashboard see a clean paid month.
    assert "zelle_unconfirmed_at" not in st
    assert "zelle_gate_alerted_at" not in st


def test_mark_paid_without_id_records_paid_no_id(state_dir):
    state_mod.save_state(YM, {"zelle_attempted_at": "2026-05-07T13:00:00"})

    rc = app._mark_paid_cli(["--mark-paid", "--month", YM])

    assert rc == 0
    st = _state()
    assert st["zelle_confirmed_at"]
    assert st["zelle_confirmation_id"] is None


def test_mark_paid_is_noop_when_already_confirmed(state_dir):
    """Never overwrite a month that's already paid - preserve the original id."""
    state_mod.save_state(
        YM,
        {
            "zelle_confirmed_at": "2026-05-07T13:05:00",
            "zelle_confirmation_id": "ORIG123",
        },
    )

    rc = app._mark_paid_cli(["--mark-paid", "NEW999", "--month", YM])

    assert rc == 0
    assert _state()["zelle_confirmation_id"] == "ORIG123"


def test_mark_paid_rejects_bad_month(state_dir):
    assert app._mark_paid_cli(["--mark-paid", "s1xrlf1ab", "--month", "July"]) == 2


def test_mark_paid_then_normal_run_skips_and_is_silent(pipeline):
    """After reconciling, a normal run treats the month as already paid: no
    Zelle attempt and no alert."""
    app._mark_paid_cli(["--mark-paid", "s1xrlf1ab", "--month", YM])

    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    assert rc == 0
    pipeline.trigger_zelle.assert_not_called()
    pipeline.send_failure.assert_not_called()


# --------------------------------------------------------------------------
# Failure routing
# --------------------------------------------------------------------------

def test_parse_failure_sends_alert(pipeline):
    pipeline.parse_bill.return_value = None

    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    assert rc == 3
    pipeline.send_failure.assert_called_once()
    assert pipeline.send_failure.call_args[0][0] == "parse"
    pipeline.trigger_zelle.assert_not_called()


def test_missing_explicit_pdf_sends_alert(pipeline):
    rc = app._run_pipeline(YM, "/no/such/bill.pdf", force=False)

    assert rc == 2
    pipeline.send_failure.assert_called_once()
    assert pipeline.send_failure.call_args[0][0] == "download"


def test_zelle_send_error_sends_alert(pipeline, monkeypatch):
    monkeypatch.setenv("ZELLE_LIVE_SEND", "1")
    pipeline.trigger_zelle.return_value = {
        "status": "error",
        "error": "MFA timeout",
        "screenshot": "mfa_timeout.png",
    }

    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    assert rc == 6
    pipeline.send_failure.assert_called_once()
    assert pipeline.send_failure.call_args[0][0] == "zelle_send"
    pipeline.send_confirmation.assert_not_called()
    st = _state()
    assert st["zelle_attempted_at"]
    assert "zelle_confirmed_at" not in st


def test_summary_email_failure_alerts_but_continues(pipeline):
    pipeline.send_summary.return_value = False

    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    # Summary failure alerts but does not abort - the gate + dry-run still run.
    assert rc == 0
    assert pipeline.send_failure.call_args_list[0][0][0] == "summary_email"
    pipeline.trigger_zelle.assert_called_once()


# --------------------------------------------------------------------------
# --force behavior
# --------------------------------------------------------------------------

def test_force_resends_summary_when_already_sent(pipeline):
    state_mod.save_state(YM, {"summary_emailed_at": "2026-05-09T08:00:00"})

    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=True)

    assert rc == 0
    pipeline.send_summary.assert_called_once()


def test_summary_skipped_when_already_sent_without_force(pipeline):
    state_mod.save_state(YM, {"summary_emailed_at": "2026-05-09T08:00:00"})

    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    assert rc == 0
    pipeline.send_summary.assert_not_called()
    # Zelle still proceeds (dry-run) - only the email is skipped.
    pipeline.trigger_zelle.assert_called_once()


# --------------------------------------------------------------------------
# main() argument handling
# --------------------------------------------------------------------------

def test_main_parses_force_flag(monkeypatch, state_dir):
    captured = {}

    def fake_pipeline(year_month, explicit_pdf, force=False):
        captured["explicit_pdf"] = explicit_pdf
        captured["force"] = force
        return 0

    monkeypatch.setattr(app, "_run_pipeline", fake_pipeline)
    monkeypatch.setattr(sys, "argv", ["app.py", "--force"])

    assert app.main() == 0
    assert captured["force"] is True
    assert captured["explicit_pdf"] is None


def test_main_parses_explicit_pdf_arg(monkeypatch, state_dir):
    captured = {}

    def fake_pipeline(year_month, explicit_pdf, force=False):
        captured["explicit_pdf"] = explicit_pdf
        captured["force"] = force
        return 0

    monkeypatch.setattr(app, "_run_pipeline", fake_pipeline)
    monkeypatch.setattr(sys, "argv", ["app.py", "/tmp/SummaryBill.pdf"])

    assert app.main() == 0
    assert captured["explicit_pdf"] == "/tmp/SummaryBill.pdf"
    assert captured["force"] is False


def test_main_crash_sends_alert_and_returns_1(monkeypatch, state_dir):
    monkeypatch.setattr(
        app, "_run_pipeline", MagicMock(side_effect=RuntimeError("boom"))
    )
    alert = MagicMock()
    monkeypatch.setattr(app.notify, "send_failure_alert", alert)
    monkeypatch.setattr(sys, "argv", ["app.py"])

    assert app.main() == 1
    alert.assert_called_once()
    assert alert.call_args[0][0] == "pipeline_crash"


def test_main_exits_cleanly_when_month_lock_already_held(monkeypatch, state_dir):
    """If another process already holds this month's lock, main() must exit 0
    without running the pipeline - the concurrent-double-pay guard."""
    ym = state_mod.current_year_month()
    run = MagicMock(return_value=0)
    monkeypatch.setattr(app, "_run_pipeline", run)
    monkeypatch.setattr(sys, "argv", ["app.py"])

    with state_mod.month_lock(ym) as held:
        assert held is True
        rc = app.main()

    assert rc == 0
    run.assert_not_called()


# --------------------------------------------------------------------------
# Unconfirmed live payment (Pay clicked, no confirmation ID captured)
# --------------------------------------------------------------------------

def test_zelle_sent_unconfirmed_flags_for_manual_review(pipeline, monkeypatch):
    monkeypatch.setenv("ZELLE_LIVE_SEND", "1")
    pipeline.trigger_zelle.return_value = {
        "status": "sent_unconfirmed",
        "confirmation_id": None,
        "screenshot": "zelle_review_x.png",
        "recipient_name": "Real Recipient",
    }

    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    assert rc == 6
    pipeline.send_failure.assert_called_once()
    assert pipeline.send_failure.call_args[0][0] == "zelle_unconfirmed"
    pipeline.send_confirmation.assert_not_called()

    st = _state()
    # Attempt is recorded (blocks auto-retry) but NOT marked confirmed/paid.
    assert st["zelle_attempted_at"]
    assert st["zelle_unconfirmed_at"]
    assert "zelle_confirmed_at" not in st


def test_zelle_sent_banner_only_marks_paid(pipeline, monkeypatch):
    """A 'sent' status with no reference number - BoA showed the 'payment sent'
    banner but the id couldn't be scraped - is a SUCCESS. zelle_pay already
    decided the payment went through, so app.py marks the month paid (which
    also hard-locks against a duplicate payment) and emails the confirmation
    instead of firing a false 'unconfirmed' alarm."""
    monkeypatch.setenv("ZELLE_LIVE_SEND", "1")
    pipeline.trigger_zelle.return_value = {
        "status": "sent",
        "confirmation_id": None,
        "payment_sent": True,
        "screenshot": "zelle_confirmation_x.png",
        "recipient_name": "Real Recipient",
    }

    rc = app._run_pipeline(YM, pipeline.fake_pdf, force=False)

    assert rc == 0
    pipeline.send_failure.assert_not_called()
    pipeline.send_confirmation.assert_called_once()
    assert pipeline.send_confirmation.call_args.kwargs["confirmation_id"] is None

    st = _state()
    assert st["zelle_confirmed_at"]
    assert st.get("zelle_confirmation_id") is None
    assert "zelle_unconfirmed_at" not in st
