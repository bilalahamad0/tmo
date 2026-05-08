import subprocess
import os


def _default_account() -> str:
    """Default Keychain account name. Falls back to current macOS user."""
    return os.getenv("KEYCHAIN_ACCOUNT") or os.getenv("USER") or ""


def get_keychain_password(service_name, account_name=None):
    """
    Retrieves a password from the macOS Keychain.

    Args:
        service_name (str): The name of the service (e.g., 'TMobile_Pass').
        account_name (str): The account name. Defaults to env $KEYCHAIN_ACCOUNT
            or $USER (the current macOS username).

    Returns:
        str: The retrieved password or None if not found/error.
    """
    if account_name is None:
        account_name = _default_account()
    try:
        # Using a list instead of a string with shell=True to prevent
        # command injection as flagged by bandit.
        cmd = [
            "security", "find-generic-password",
            "-s", service_name, "-a", account_name, "-w"
        ]
        output = subprocess.check_output(cmd).decode('utf-8').strip()
        return output
    except Exception:
        # Don't log the service name if it might be sensitive
        # We'll just log that a retrieval failed.
        print(f"Error: Could not retrieve '{service_name}' from Keychain.")
        return None


def get_env_or_keychain(env_var, keychain_service, account_name=None):
    """
    Attempts to get a value from environment variables first,
    falling back to macOS Keychain.
    """
    if account_name is None:
        account_name = _default_account()
    val = os.getenv(env_var)
    if val:
        return val
    return get_keychain_password(keychain_service, account_name)
