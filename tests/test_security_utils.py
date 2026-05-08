import os
import sys
from unittest.mock import patch

# Setup path to import local modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from security_utils import get_keychain_password  # noqa: E402


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
