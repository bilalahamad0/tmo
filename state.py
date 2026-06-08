"""Per-bill state file with atomic writes.

State lives at ~/.tmo_state/bill_<YYYY-MM>.json and is the source of truth
for what the pipeline has already done for the current month's bill. The
zelle_confirmed_at field is the hard guard against double-paying: once set,
the pipeline refuses to send Zelle again for that month.
"""
import contextlib
import fcntl
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


STATE_DIR = Path.home() / ".tmo_state"


def _state_path(year_month: str) -> Path:
    return STATE_DIR / f"bill_{year_month}.json"


def _lock_path(year_month: str) -> Path:
    return STATE_DIR / f"bill_{year_month}.lock"


@contextlib.contextmanager
def month_lock(year_month: str):
    """Exclusive, non-blocking per-month run lock.

    Prevents two concurrent pipeline runs (e.g. an overlapping scheduled
    fire and a manual run, or a hung prior run) from both passing the Zelle
    safety gate and sending duplicate REAL payments for the same bill month.

    Yields True if the lock was acquired, False if another process already
    holds it. The OS releases the lock automatically when the holding
    process exits or dies, so a crash cannot wedge future runs.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    lock_file = open(_lock_path(year_month), "w")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        lock_file.close()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_state(year_month: str) -> dict:
    """Load state for the given YYYY-MM. Returns {} if no state file exists."""
    path = _state_path(year_month)
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Warning: could not read state file {path}: {e}")
        return {}


def save_state(year_month: str, state: dict) -> None:
    """Atomically write state for the given YYYY-MM.

    Writes to a temp file in the same directory, then renames over the target
    so a crash mid-write never leaves a corrupt state file. Permissions are
    forced to 0600 (owner read/write only) since state contains transaction
    confirmation IDs and bill amounts.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    path = _state_path(year_month)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(STATE_DIR), prefix=f"bill_{year_month}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def update_state(year_month: str, **fields) -> dict:
    """Load, merge fields, save. Returns the merged state."""
    state = load_state(year_month)
    state.update(fields)
    save_state(year_month, state)
    return state


def current_year_month() -> str:
    return datetime.now().strftime("%Y-%m")
