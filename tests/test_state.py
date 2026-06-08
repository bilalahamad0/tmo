import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)

import state  # noqa: E402


def _patched_state_dir(tmp: Path):
    return patch.object(state, "STATE_DIR", tmp)


def test_load_state_returns_empty_when_no_file():
    with tempfile.TemporaryDirectory() as tmp:
        with _patched_state_dir(Path(tmp)):
            assert state.load_state("2026-05") == {}


def test_save_and_load_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        with _patched_state_dir(Path(tmp)):
            data = {"bill_posted_date": "2026-05-04", "parsed_total": 123.45}
            state.save_state("2026-05", data)
            loaded = state.load_state("2026-05")
            assert loaded == data


def test_update_state_merges_fields():
    with tempfile.TemporaryDirectory() as tmp:
        with _patched_state_dir(Path(tmp)):
            state.save_state("2026-05", {"a": 1, "b": 2})
            merged = state.update_state("2026-05", b=20, c=3)
            assert merged == {"a": 1, "b": 20, "c": 3}
            assert state.load_state("2026-05") == {"a": 1, "b": 20, "c": 3}


def test_save_atomic_no_partial_file_on_crash():
    """If json.dump fails mid-write, the target file must be unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with _patched_state_dir(tmp_path):
            state.save_state("2026-05", {"original": True})
            with patch("json.dump", side_effect=RuntimeError("boom")):
                try:
                    state.save_state("2026-05", {"corrupted": True})
                except RuntimeError:
                    pass
            assert state.load_state("2026-05") == {"original": True}
            leftover = list(tmp_path.glob("*.tmp"))
            assert leftover == []


def test_load_returns_empty_on_invalid_json():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with _patched_state_dir(tmp_path):
            (tmp_path / "bill_2026-05.json").write_text("not json {{{")
            assert state.load_state("2026-05") == {}


def test_current_year_month_format():
    ym = state.current_year_month()
    assert len(ym) == 7
    assert ym[4] == "-"
    int(ym[:4])
    assert 1 <= int(ym[5:7]) <= 12


def test_month_lock_acquires_and_releases():
    with tempfile.TemporaryDirectory() as tmp:
        with _patched_state_dir(Path(tmp)):
            with state.month_lock("2026-06") as acquired:
                assert acquired is True
            # Lock is released on exit, so it can be re-acquired.
            with state.month_lock("2026-06") as again:
                assert again is True


def test_month_lock_blocks_second_holder():
    """A second acquisition of the same month's lock while the first is held
    must return False (this is the concurrent-double-pay guard)."""
    with tempfile.TemporaryDirectory() as tmp:
        with _patched_state_dir(Path(tmp)):
            with state.month_lock("2026-06") as first:
                assert first is True
                with state.month_lock("2026-06") as second:
                    assert second is False


def test_month_lock_is_per_month():
    """Different bill months use independent locks and never block each
    other."""
    with tempfile.TemporaryDirectory() as tmp:
        with _patched_state_dir(Path(tmp)):
            with state.month_lock("2026-06") as a:
                assert a is True
                with state.month_lock("2026-07") as b:
                    assert b is True
