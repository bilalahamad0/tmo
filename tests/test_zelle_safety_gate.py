"""Unit tests for app._zelle_safety_gate - the hard preconditions that stand
between a parsed bill and real money moving over Zelle.

Every branch here is a guard against either double-paying or over-paying, so
each one gets explicit coverage. We patch app.get_env_or_keychain so the cap
and recipient come from the test, never the real env or macOS Keychain.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app  # noqa: E402


@pytest.fixture
def config(monkeypatch):
    """Drive get_env_or_keychain from a dict the test controls.

    Returns the dict so a test can mutate cap / recipient. Defaults to a sane
    $300 cap and a configured recipient (the 'happy' baseline)."""
    values = {
        "ZELLE_AMOUNT_CAP": "300",
        "ZELLE_RECIPIENT_NAME": "Real Recipient",
    }

    def fake_lookup(env_var, keychain_service, account_name=None):
        return values.get(env_var)

    monkeypatch.setattr(app, "get_env_or_keychain", fake_lookup)
    return values


def test_gate_passes_happy_path(config):
    ok, reason = app._zelle_safety_gate(102.43, {}, force=False)
    assert ok is True
    assert reason == ""


def test_gate_blocks_when_already_confirmed(config):
    st = {
        "zelle_confirmed_at": "2026-05-10T09:01:00",
        "zelle_confirmation_id": "CONF123",
    }
    ok, reason = app._zelle_safety_gate(102.43, st, force=False)
    assert ok is False
    assert "already confirmed" in reason
    assert "CONF123" in reason


def test_force_never_bypasses_confirmed_lock(config):
    """The confirmed-payment lock is the hard guard against double paying;
    even --force must not get past it."""
    st = {"zelle_confirmed_at": "2026-05-10T09:01:00"}
    ok, reason = app._zelle_safety_gate(102.43, st, force=True)
    assert ok is False
    assert "already confirmed" in reason


def test_gate_blocks_attempted_without_confirmation(config):
    """A crash after 'attempted' but before 'confirmed' must block automatic
    retry - we can't tell if the money already moved."""
    st = {"zelle_attempted_at": "2026-05-10T09:00:00"}
    ok, reason = app._zelle_safety_gate(102.43, st, force=False)
    assert ok is False
    assert "attempted" in reason
    assert "--force" in reason


def test_force_bypasses_attempted_guard(config):
    st = {"zelle_attempted_at": "2026-05-10T09:00:00"}
    ok, reason = app._zelle_safety_gate(102.43, st, force=True)
    assert ok is True
    assert reason == ""


def test_gate_blocks_zero_amount(config):
    ok, reason = app._zelle_safety_gate(0.0, {}, force=False)
    assert ok is False
    assert "0 or negative" in reason


def test_gate_blocks_negative_amount(config):
    ok, reason = app._zelle_safety_gate(-5.0, {}, force=False)
    assert ok is False
    assert "0 or negative" in reason


def test_gate_blocks_amount_over_cap(config):
    config["ZELLE_AMOUNT_CAP"] = "300"
    ok, reason = app._zelle_safety_gate(350.00, {}, force=False)
    assert ok is False
    assert "exceeds cap" in reason
    assert "$350.00" in reason
    assert "$300.00" in reason


def test_gate_amount_exactly_at_cap_is_allowed(config):
    """Cap is an inclusive ceiling: amount == cap should pass (guard is > cap)."""
    config["ZELLE_AMOUNT_CAP"] = "300"
    ok, reason = app._zelle_safety_gate(300.00, {}, force=False)
    assert ok is True
    assert reason == ""


def test_gate_honors_custom_cap(config):
    config["ZELLE_AMOUNT_CAP"] = "100"
    blocked, _ = app._zelle_safety_gate(150.0, {}, force=False)
    assert blocked is False
    ok, _ = app._zelle_safety_gate(99.0, {}, force=False)
    assert ok is True


def test_gate_defaults_cap_to_300_when_unset(config):
    config["ZELLE_AMOUNT_CAP"] = None
    blocked, reason = app._zelle_safety_gate(301.0, {}, force=False)
    assert blocked is False
    assert "$300.00" in reason
    ok, _ = app._zelle_safety_gate(299.0, {}, force=False)
    assert ok is True


def test_gate_defaults_cap_when_value_invalid(config):
    """A garbage cap string must fail safe to the $300 default, not crash or
    disable the cap."""
    config["ZELLE_AMOUNT_CAP"] = "not-a-number"
    blocked, reason = app._zelle_safety_gate(400.0, {}, force=False)
    assert blocked is False
    assert "$300.00" in reason


def test_gate_blocks_when_recipient_missing(config):
    config["ZELLE_RECIPIENT_NAME"] = None
    ok, reason = app._zelle_safety_gate(102.43, {}, force=False)
    assert ok is False
    assert "ZELLE_RECIPIENT_NAME not configured" in reason


def test_confirmed_lock_checked_before_amount(config):
    """Ordering matters: a confirmed bill is refused even if the amount is also
    invalid, so the message points at the real reason (idempotency)."""
    st = {"zelle_confirmed_at": "2026-05-10T09:01:00"}
    ok, reason = app._zelle_safety_gate(0.0, st, force=False)
    assert ok is False
    assert "already confirmed" in reason
