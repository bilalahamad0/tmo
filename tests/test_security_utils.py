import os
import sys
from unittest.mock import patch

# Setup path to import local modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import security_utils  # noqa: E402
from security_utils import (  # noqa: E402
    get_env_or_keychain,
    get_keychain_password,
)


def test_get_keychain_password_returns_value():
    with patch('subprocess.check_output') as mock_sub:
        mock_sub.return_value = b"test_password\n"
        result = get_keychain_password("TestService", "TestAccount")
        assert result == "test_password"


def test_get_keychain_password_returns_none_on_error():
    with patch('subprocess.check_output') as mock_sub:
        mock_sub.side_effect = Exception("error")
        result = get_keychain_password("TestService", "TestAccount")
        assert result is None


def test_get_keychain_password_defaults_account_to_user(monkeypatch):
    """When no account is passed, it falls back to $USER for the -a flag."""
    monkeypatch.delenv("KEYCHAIN_ACCOUNT", raising=False)
    monkeypatch.setenv("USER", "alice")
    with patch("subprocess.check_output") as mock_sub:
        mock_sub.return_value = b"secret\n"
        assert get_keychain_password("TMobile_Pass") == "secret"
        cmd = mock_sub.call_args[0][0]
        assert cmd[cmd.index("-a") + 1] == "alice"


# --------------------------------------------------------------------------
# _default_account
# --------------------------------------------------------------------------

def test_default_account_prefers_keychain_account_env(monkeypatch):
    monkeypatch.setenv("KEYCHAIN_ACCOUNT", "override-acct")
    monkeypatch.setenv("USER", "alice")
    assert security_utils._default_account() == "override-acct"


def test_default_account_falls_back_to_user(monkeypatch):
    monkeypatch.delenv("KEYCHAIN_ACCOUNT", raising=False)
    monkeypatch.setenv("USER", "alice")
    assert security_utils._default_account() == "alice"


def test_default_account_empty_when_nothing_set(monkeypatch):
    monkeypatch.delenv("KEYCHAIN_ACCOUNT", raising=False)
    monkeypatch.delenv("USER", raising=False)
    assert security_utils._default_account() == ""


# --------------------------------------------------------------------------
# get_env_or_keychain
# --------------------------------------------------------------------------

def test_get_env_or_keychain_prefers_env(monkeypatch):
    monkeypatch.setenv("ZELLE_RECIPIENT_NAME", "From Env")
    called = patch.object(security_utils, "get_keychain_password")
    with called as mock_kc:
        result = get_env_or_keychain("ZELLE_RECIPIENT_NAME", "ZELLE_RECIPIENT_NAME")
    assert result == "From Env"
    mock_kc.assert_not_called()


def test_get_env_or_keychain_falls_back_to_keychain(monkeypatch):
    monkeypatch.delenv("ZELLE_RECIPIENT_NAME", raising=False)
    with patch.object(
        security_utils, "get_keychain_password", return_value="From Keychain"
    ) as mock_kc:
        result = get_env_or_keychain("ZELLE_RECIPIENT_NAME", "ZELLE_RECIPIENT_NAME")
    assert result == "From Keychain"
    mock_kc.assert_called_once()


def test_get_env_or_keychain_uses_default_account(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    monkeypatch.delenv("KEYCHAIN_ACCOUNT", raising=False)
    monkeypatch.setenv("USER", "bob")
    with patch.object(
        security_utils, "get_keychain_password", return_value="x"
    ) as mock_kc:
        get_env_or_keychain("MISSING_VAR", "Some_Service")
    assert mock_kc.call_args[0][1] == "bob"
