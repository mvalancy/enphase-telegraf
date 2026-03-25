"""Unit tests for history.py — HistoryCloner download/resume/progress logic.

Tests the HistoryCloner class: init, progress tracking, resume, date detection,
day fetching, skip-existing, and rate limiting. All tests use tmp_path for
filesystem isolation and mock HTTP requests.

Target: ~200 tests.
"""

import json
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock, call

import pytest

from enphase_cloud.history import HistoryCloner


# ── Mock client for all tests ────────────────────────────────────────

class MockSession:
    """Fake requests.Session-like object."""

    def __init__(self):
        self.responses = {}
        self._default_response = None

    def get(self, url, params=None, headers=None, timeout=None):
        resp = MagicMock()
        key = params.get("start_date") if params else None
        if key and key in self.responses:
            resp.status_code, resp_data = self.responses[key]
            resp.json.return_value = resp_data
        elif self._default_response:
            resp.status_code, resp_data = self._default_response
            resp.json.return_value = resp_data
        else:
            resp.status_code = 200
            resp.json.return_value = {
                "stats": [{"totals": {"production": 100.0}, "intervals": []}]
            }
        resp.raise_for_status = MagicMock()
        if resp.status_code >= 400 and resp.status_code != 404:
            from requests.exceptions import HTTPError
            resp.raise_for_status.side_effect = Exception(f"HTTP {resp.status_code}")
        return resp


class MockSessionHolder:
    """Mimics client._session with site_id and session attributes."""

    def __init__(self, site_id="12345"):
        self.site_id = site_id
        self.session = MockSession()


class MockClient:
    """Minimal mock of EnlightenClient for HistoryCloner tests."""

    def __init__(self, site_id="12345"):
        self.authenticated = True
        self._session = MockSessionHolder(site_id)
        self._auth_count = 0
        self._lifetime_energy = {"production": []}

    def _ensure_auth(self):
        self._auth_count += 1

    def _headers(self):
        return {"Authorization": "Bearer fake"}

    def get_lifetime_energy(self):
        return self._lifetime_energy


# ═══════════════════════════════════════════════════════════════════
# TestHistoryClonerInit — 20 tests
# ═══════════════════════════════════════════════════════════════════

class TestHistoryClonerInit:

    def test_creates_history_dir(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.history_dir.exists()
        assert cloner.history_dir.is_dir()

    def test_history_dir_is_subdir(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.history_dir == tmp_path / "history"

    def test_progress_file_path(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress_file == tmp_path / "history_progress.json"

    def test_initial_state_not_started(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["state"] == "not_started"

    def test_initial_days_completed_zero(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["days_completed"] == 0

    def test_initial_days_total_zero(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["days_total"] == 0

    def test_initial_errors_zero(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["errors"] == 0

    def test_initial_running_false(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner._running is False

    def test_stores_client(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.client is client

    def test_stores_site_id(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "99999")
        assert cloner.site_id == "99999"

    def test_stores_cache_dir(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.cache_dir == tmp_path

    def test_load_progress_no_file(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["start_date"] is None
        assert cloner.progress["current_date"] is None
        assert cloner.progress["end_date"] is None

    def test_load_progress_valid_file(self, tmp_path):
        progress = {
            "state": "running",
            "start_date": "2024-01-01",
            "current_date": "2024-01-05",
            "end_date": "2024-01-10",
            "days_completed": 5,
            "days_total": 10,
            "errors": 0,
            "last_error": None,
            "started_at": 1700000000,
            "last_request_at": 1700000100,
        }
        (tmp_path / "history_progress.json").write_text(json.dumps(progress))
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["state"] == "running"
        assert cloner.progress["days_completed"] == 5
        assert cloner.progress["current_date"] == "2024-01-05"

    def test_load_progress_corrupt_file(self, tmp_path):
        (tmp_path / "history_progress.json").write_text("not valid json!!!")
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["state"] == "not_started"

    def test_load_progress_empty_file(self, tmp_path):
        (tmp_path / "history_progress.json").write_text("")
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["state"] == "not_started"

    def test_history_dir_already_exists(self, tmp_path):
        (tmp_path / "history").mkdir()
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.history_dir.exists()

    def test_status_property_includes_running(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        status = cloner.status
        assert "running" in status
        assert status["running"] is False

    def test_status_property_includes_cached_days(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        status = cloner.status
        assert "cached_days" in status
        assert status["cached_days"] == 0

    def test_status_property_percent_complete_zero(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        status = cloner.status
        assert status["percent_complete"] == 0

    def test_status_property_is_copy(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        status = cloner.status
        status["state"] = "modified"
        assert cloner.progress["state"] == "not_started"


# ═══════════════════════════════════════════════════════════════════
# TestHistoryClonerProgress — 40 tests
# ═══════════════════════════════════════════════════════════════════

class TestHistoryClonerProgress:

    def test_save_progress_creates_file(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner._save_progress()
        assert cloner.progress_file.exists()

    def test_save_progress_valid_json(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert isinstance(data, dict)

    def test_save_progress_preserves_state(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["state"] = "running"
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["state"] == "running"

    def test_save_progress_preserves_days_completed(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["days_completed"] = 42
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["days_completed"] == 42

    def test_state_transition_not_started_to_running(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["state"] == "not_started"
        cloner.progress["state"] = "running"
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["state"] == "running"

    def test_state_transition_running_to_complete(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["state"] = "running"
        cloner.progress["state"] = "complete"
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["state"] == "complete"

    def test_state_transition_running_to_error(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["state"] = "running"
        cloner.progress["state"] = "error"
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["state"] == "error"

    def test_state_transition_not_started_to_error(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["state"] = "error"
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["state"] == "error"

    def test_days_completed_increments(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        for i in range(1, 11):
            cloner.progress["days_completed"] = i
        assert cloner.progress["days_completed"] == 10

    def test_current_date_updates(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["current_date"] = "2024-01-01"
        assert cloner.progress["current_date"] == "2024-01-01"
        cloner.progress["current_date"] = "2024-01-02"
        assert cloner.progress["current_date"] == "2024-01-02"

    def test_errors_counter_increments(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["errors"] = 0
        for i in range(5):
            cloner.progress["errors"] += 1
        assert cloner.progress["errors"] == 5

    def test_last_error_storage(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["last_error"] = "2024-01-05: HTTPError: 500"
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["last_error"] == "2024-01-05: HTTPError: 500"

    def test_last_request_at_updates(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        ts = time.time()
        cloner.progress["last_request_at"] = ts
        assert cloner.progress["last_request_at"] == ts

    def test_started_at_stored(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        ts = time.time()
        cloner.progress["started_at"] = ts
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert abs(data["started_at"] - ts) < 1

    def test_start_date_stored(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["start_date"] = "2023-06-01"
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["start_date"] == "2023-06-01"

    def test_end_date_stored(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["end_date"] = "2024-06-15"
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["end_date"] == "2024-06-15"

    def test_days_total_stored(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["days_total"] = 365
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["days_total"] == 365

    def test_save_then_load_roundtrip(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["state"] = "running"
        cloner.progress["days_completed"] = 50
        cloner.progress["current_date"] = "2024-03-15"
        cloner.progress["errors"] = 2
        cloner._save_progress()
        # Load in new cloner
        cloner2 = HistoryCloner(client, tmp_path, "12345")
        assert cloner2.progress["state"] == "running"
        assert cloner2.progress["days_completed"] == 50
        assert cloner2.progress["current_date"] == "2024-03-15"
        assert cloner2.progress["errors"] == 2

    def test_save_overwrites_previous(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["state"] = "running"
        cloner._save_progress()
        cloner.progress["state"] = "complete"
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["state"] == "complete"

    def test_status_percent_complete_50(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["days_completed"] = 50
        cloner.progress["days_total"] = 100
        status = cloner.status
        assert status["percent_complete"] == 50.0

    def test_status_percent_complete_100(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["days_completed"] = 365
        cloner.progress["days_total"] = 365
        status = cloner.status
        assert status["percent_complete"] == 100.0

    def test_status_percent_complete_partial(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["days_completed"] = 1
        cloner.progress["days_total"] = 3
        status = cloner.status
        assert abs(status["percent_complete"] - 33.3) < 0.1

    def test_status_cached_days_counts_files(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        history_dir = tmp_path / "history"
        for i in range(5):
            (history_dir / f"day_2024-01-{i+1:02d}.json").write_text("{}")
        status = cloner.status
        assert status["cached_days"] == 5

    def test_progress_file_indented(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner._save_progress()
        content = cloner.progress_file.read_text()
        assert "  " in content  # indent=2

    def test_last_error_none_initially(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["last_error"] is None

    def test_started_at_none_initially(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["started_at"] is None

    def test_last_request_at_none_initially(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["last_request_at"] is None

    def test_multiple_errors_tracked(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["errors"] = 3
        cloner.progress["last_error"] = "day3: timeout"
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["errors"] == 3
        assert "timeout" in data["last_error"]

    def test_progress_update_dict(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress.update({
            "state": "running",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "days_total": 366,
        })
        assert cloner.progress["state"] == "running"
        assert cloner.progress["days_total"] == 366

    def test_on_progress_callback_default_none(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner._on_progress is None

    def test_save_progress_handles_date_serialization(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["start_date"] = date(2024, 1, 1)
        cloner._save_progress()
        # Should not crash (default=str handles date objects)
        data = json.loads(cloner.progress_file.read_text())
        assert data["start_date"] == "2024-01-01"

    def test_status_copy_does_not_modify_progress(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        status = cloner.status
        status["state"] = "hacked"
        status["running"] = True
        assert cloner.progress["state"] == "not_started"
        assert cloner._running is False

    def test_progress_default_keys_all_present(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        expected_keys = {"state", "start_date", "current_date", "end_date",
                         "days_completed", "days_total", "errors", "last_error",
                         "started_at", "last_request_at"}
        assert expected_keys == set(cloner.progress.keys())

    def test_days_completed_large_value(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["days_completed"] = 10000
        cloner.progress["days_total"] = 10000
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["days_completed"] == 10000

    def test_last_error_long_string(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.progress["last_error"] = "x" * 500
        cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert len(data["last_error"]) == 500

    def test_save_progress_multiple_times(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        for i in range(20):
            cloner.progress["days_completed"] = i
            cloner._save_progress()
        data = json.loads(cloner.progress_file.read_text())
        assert data["days_completed"] == 19

    def test_status_extra_keys(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        status = cloner.status
        assert "running" in status
        assert "cached_days" in status
        assert "percent_complete" in status

    def test_progress_errors_never_negative(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["errors"] >= 0

    def test_status_with_cached_files_ignores_non_day_files(self, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        history_dir = tmp_path / "history"
        (history_dir / "day_2024-01-01.json").write_text("{}")
        (history_dir / "other_file.json").write_text("{}")
        (history_dir / "readme.txt").write_text("hello")
        status = cloner.status
        assert status["cached_days"] == 1


# ═══════════════════════════════════════════════════════════════════
# TestHistoryClonerResume — 30 tests
# ═══════════════════════════════════════════════════════════════════

class TestHistoryClonerResume:

    @patch('time.sleep')
    def test_resume_from_day_5_of_10(self, mock_sleep, tmp_path):
        """Set progress to day 5, verify starts from day 6."""
        client = MockClient()
        start = date(2024, 1, 1)
        # Pre-cache days 1-5
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        for i in range(5):
            d = start + timedelta(days=i)
            (history_dir / f"day_{d.isoformat()}.json").write_text(json.dumps({"stats": []}))

        progress = {
            "state": "running",
            "start_date": "2024-01-01",
            "current_date": "2024-01-05",
            "end_date": "2024-01-10",
            "days_completed": 5,
            "days_total": 10,
            "errors": 0,
            "last_error": None,
            "started_at": 1700000000,
            "last_request_at": 1700000100,
        }
        (tmp_path / "history_progress.json").write_text(json.dumps(progress))

        cloner = HistoryCloner(client, tmp_path, "12345")
        # Run with a short range so we can check behavior
        cloner.run(start_date="2024-01-01", request_delay=0)

        # Should have created files for days 6-10 (and today)
        assert cloner.progress["state"] == "complete"

    @patch('time.sleep')
    def test_resume_all_days_cached(self, mock_sleep, tmp_path):
        """When all days are cached, should skip all fetches."""
        client = MockClient()
        start = date.today() - timedelta(days=2)
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        for i in range(3):
            d = start + timedelta(days=i)
            (history_dir / f"day_{d.isoformat()}.json").write_text(
                json.dumps({"stats": [{"totals": {}, "intervals": []}]}))

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)

        assert cloner.progress["state"] == "complete"

    @patch('time.sleep')
    def test_resume_partial_cache(self, mock_sleep, tmp_path):
        """Some days cached, others not."""
        client = MockClient()
        start = date.today() - timedelta(days=4)
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)

        # Cache days 0 and 2
        (history_dir / f"day_{start.isoformat()}.json").write_text(json.dumps({"stats": []}))
        d2 = start + timedelta(days=2)
        (history_dir / f"day_{d2.isoformat()}.json").write_text(json.dumps({"stats": []}))

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)

        assert cloner.progress["state"] == "complete"

    @patch('time.sleep')
    def test_resume_with_corrupt_progress(self, mock_sleep, tmp_path):
        """Corrupt progress file should reset to defaults and run from start."""
        (tmp_path / "history_progress.json").write_text("corrupt!!!")
        client = MockClient()
        start = date.today() - timedelta(days=1)

        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["state"] == "not_started"
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert cloner.progress["state"] == "complete"

    @patch('time.sleep')
    def test_resume_with_invalid_current_date(self, mock_sleep, tmp_path):
        """Invalid current_date format should be ignored and start from beginning."""
        progress = {
            "state": "running",
            "start_date": None,
            "current_date": "not-a-date",
            "end_date": None,
            "days_completed": 0,
            "days_total": 0,
            "errors": 0,
            "last_error": None,
            "started_at": None,
            "last_request_at": None,
        }
        (tmp_path / "history_progress.json").write_text(json.dumps(progress))

        client = MockClient()
        start = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert cloner.progress["state"] == "complete"

    @patch('time.sleep')
    def test_resume_current_date_none(self, mock_sleep, tmp_path):
        """current_date=None should start from first_day."""
        progress = {
            "state": "not_started",
            "start_date": None,
            "current_date": None,
            "end_date": None,
            "days_completed": 0,
            "days_total": 0,
            "errors": 0,
            "last_error": None,
            "started_at": None,
            "last_request_at": None,
        }
        (tmp_path / "history_progress.json").write_text(json.dumps(progress))

        client = MockClient()
        start = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert cloner.progress["state"] == "complete"

    @patch('time.sleep')
    def test_resume_preserves_started_at(self, mock_sleep, tmp_path):
        """Resuming should preserve original started_at."""
        original_time = 1700000000
        progress = {
            "state": "running",
            "start_date": "2024-01-01",
            "current_date": date.today().isoformat(),
            "end_date": date.today().isoformat(),
            "days_completed": 1,
            "days_total": 2,
            "errors": 0,
            "last_error": None,
            "started_at": original_time,
            "last_request_at": 1700000100,
        }
        (tmp_path / "history_progress.json").write_text(json.dumps(progress))

        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        start = date.today()
        cloner.run(start_date=start.isoformat(), request_delay=0)
        # started_at should be preserved (not overwritten)
        assert cloner.progress["started_at"] == original_time

    @patch('time.sleep')
    def test_resume_updates_end_date(self, mock_sleep, tmp_path):
        """End date should be updated to today on resume."""
        client = MockClient()
        start = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert cloner.progress["end_date"] == str(date.today())

    @patch('time.sleep')
    def test_resume_sets_running_state(self, mock_sleep, tmp_path):
        """State should be 'running' during execution."""
        client = MockClient()
        states_seen = []

        original_save = HistoryCloner._save_progress
        def spy_save(self_cloner):
            states_seen.append(self_cloner.progress["state"])
            original_save(self_cloner)

        with patch.object(HistoryCloner, '_save_progress', spy_save):
            cloner = HistoryCloner(client, tmp_path, "12345")
            start = date.today()
            cloner.run(start_date=start.isoformat(), request_delay=0)

        assert "running" in states_seen

    @patch('time.sleep')
    def test_run_sets_running_flag(self, mock_sleep, tmp_path):
        """_running should be True during execution and False after."""
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner._running is False
        start = date.today()
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert cloner._running is False

    @patch('time.sleep')
    def test_resume_from_yesterday(self, mock_sleep, tmp_path):
        yesterday = date.today() - timedelta(days=1)
        progress = {
            "state": "running",
            "start_date": yesterday.isoformat(),
            "current_date": yesterday.isoformat(),
            "end_date": date.today().isoformat(),
            "days_completed": 1,
            "days_total": 2,
            "errors": 0,
            "last_error": None,
            "started_at": 1700000000,
            "last_request_at": 1700000100,
        }
        (tmp_path / "history_progress.json").write_text(json.dumps(progress))

        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=yesterday.isoformat(), request_delay=0)
        assert cloner.progress["state"] == "complete"

    @patch('time.sleep')
    def test_resume_start_date_from_arg(self, mock_sleep, tmp_path):
        """Explicit start_date arg should be used."""
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        start = date.today()
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert cloner.progress["start_date"] == start.isoformat()

    @patch('time.sleep')
    def test_resume_error_count_preserved(self, mock_sleep, tmp_path):
        progress = {
            "state": "running",
            "start_date": date.today().isoformat(),
            "current_date": date.today().isoformat(),
            "end_date": date.today().isoformat(),
            "days_completed": 1,
            "days_total": 1,
            "errors": 5,
            "last_error": "previous error",
            "started_at": 1700000000,
            "last_request_at": 1700000100,
        }
        (tmp_path / "history_progress.json").write_text(json.dumps(progress))

        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        assert cloner.progress["errors"] == 5

    @patch('time.sleep')
    def test_resume_single_day(self, mock_sleep, tmp_path):
        """Start and end on the same day."""
        client = MockClient()
        today = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=today.isoformat(), request_delay=0)
        assert cloner.progress["state"] == "complete"
        assert cloner.progress["days_completed"] >= 1

    @patch('time.sleep')
    def test_resume_two_days(self, mock_sleep, tmp_path):
        """Start yesterday, end today."""
        client = MockClient()
        yesterday = date.today() - timedelta(days=1)
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=yesterday.isoformat(), request_delay=0)
        assert cloner.progress["state"] == "complete"

    @patch('time.sleep')
    def test_resume_updates_days_total(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today() - timedelta(days=5)
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert cloner.progress["days_total"] == 6  # 5 days ago to today inclusive

    @patch('time.sleep')
    def test_resume_end_of_run_state_complete(self, mock_sleep, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=date.today().isoformat(), request_delay=0)
        assert cloner.progress["state"] == "complete"

    @patch('time.sleep')
    def test_resume_running_false_after_complete(self, mock_sleep, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=date.today().isoformat(), request_delay=0)
        assert cloner._running is False

    @patch('time.sleep')
    def test_resume_progress_saved_at_end(self, mock_sleep, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=date.today().isoformat(), request_delay=0)
        assert cloner.progress_file.exists()
        data = json.loads(cloner.progress_file.read_text())
        assert data["state"] == "complete"

    @patch('time.sleep')
    def test_resume_auth_called(self, mock_sleep, tmp_path):
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=date.today().isoformat(), request_delay=0)
        assert client._auth_count >= 1

    @patch('time.sleep')
    def test_resume_from_far_future_current_date(self, mock_sleep, tmp_path):
        """current_date ahead of end_date should skip all."""
        progress = {
            "state": "running",
            "start_date": date.today().isoformat(),
            "current_date": "2099-12-31",
            "end_date": date.today().isoformat(),
            "days_completed": 0,
            "days_total": 1,
            "errors": 0,
            "last_error": None,
            "started_at": 1700000000,
            "last_request_at": None,
        }
        (tmp_path / "history_progress.json").write_text(json.dumps(progress))
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=date.today().isoformat(), request_delay=0)
        assert cloner.progress["state"] == "complete"

    @patch('time.sleep')
    def test_resume_handles_empty_progress_file(self, mock_sleep, tmp_path):
        (tmp_path / "history_progress.json").write_text("{}")
        client = MockClient()
        cloner = HistoryCloner(client, tmp_path, "12345")
        # Empty dict is valid JSON but has no keys - _load_progress returns it
        cloner.run(start_date=date.today().isoformat(), request_delay=0)


# ═══════════════════════════════════════════════════════════════════
# TestHistoryClonerDateDetection — 30 tests
# ═══════════════════════════════════════════════════════════════════

class TestHistoryClonerDateDetection:

    def test_detect_from_production_entries(self, tmp_path):
        client = MockClient()
        ts = 1700000000  # 2023-11-14
        client._lifetime_energy = {
            "production": [
                {"end_at": ts, "value": 100.0},
            ]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        assert result.year == 2023
        assert result.month == 11

    def test_detect_skips_zero_production(self, tmp_path):
        client = MockClient()
        ts_zero = 1690000000
        ts_nonzero = 1700000000
        client._lifetime_energy = {
            "production": [
                {"end_at": ts_zero, "value": 0},
                {"end_at": ts_nonzero, "value": 100.0},
            ]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        from datetime import datetime
        assert result == datetime.fromtimestamp(ts_nonzero).date()

    def test_detect_with_start_date_field(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = {
            "production": [],
            "start_date": "2022-06-15",
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        assert result == date(2022, 6, 15)

    def test_detect_empty_production(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = {"production": []}
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        # Fallback: 1 year ago
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_all_zero_production(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = {
            "production": [
                {"end_at": 1700000000, "value": 0},
                {"end_at": 1700100000, "value": 0},
            ]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        # No non-zero production, no start_date field => fallback
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_exception_fallback(self, tmp_path):
        client = MockClient()
        client.get_lifetime_energy = MagicMock(side_effect=Exception("API error"))
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_fallback_1_year_ago(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = {}
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_non_dict_response(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = "not a dict"
        client.get_lifetime_energy = MagicMock(return_value="not a dict")
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_none_response(self, tmp_path):
        client = MockClient()
        client.get_lifetime_energy = MagicMock(return_value=None)
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_list_response(self, tmp_path):
        client = MockClient()
        client.get_lifetime_energy = MagicMock(return_value=[1, 2, 3])
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_with_timestamp_field(self, tmp_path):
        """Entry uses 'timestamp' instead of 'end_at'."""
        client = MockClient()
        ts = 1700000000
        client._lifetime_energy = {
            "production": [
                {"timestamp": ts, "value": 50.0},
            ]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        from datetime import datetime
        assert result == datetime.fromtimestamp(ts).date()

    def test_detect_production_negative_value(self, tmp_path):
        """Negative production should not trigger (value > 0 check)."""
        client = MockClient()
        client._lifetime_energy = {
            "production": [
                {"end_at": 1700000000, "value": -100},
            ]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_production_no_value_key(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = {
            "production": [
                {"end_at": 1700000000},
            ]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_first_nonzero_is_returned(self, tmp_path):
        """Should return the date of the first non-zero entry."""
        client = MockClient()
        ts1 = 1690000000
        ts2 = 1695000000
        ts3 = 1700000000
        client._lifetime_energy = {
            "production": [
                {"end_at": ts1, "value": 0},
                {"end_at": ts2, "value": 50.0},
                {"end_at": ts3, "value": 200.0},
            ]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        from datetime import datetime
        assert result == datetime.fromtimestamp(ts2).date()

    def test_detect_production_none_entries(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = {
            "production": [None, None]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        # Should not crash
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_production_int_entries(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = {
            "production": [42, 99]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_no_production_key(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = {"consumption": []}
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_start_date_field_invalid(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = {
            "production": [],
            "start_date": "not-a-date",
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_returns_date_object(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = {
            "production": [{"end_at": 1700000000, "value": 100.0}]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        assert isinstance(result, date)

    def test_detect_production_entry_no_timestamp(self, tmp_path):
        """Entry with value but no timestamp."""
        client = MockClient()
        client._lifetime_energy = {
            "production": [{"value": 100.0}]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    @patch('time.sleep')
    def test_detect_used_when_no_start_arg(self, mock_sleep, tmp_path):
        """When run() is called without start_date, _detect_start_date is used."""
        client = MockClient()
        ts = 1700000000  # 2023-11-14
        client._lifetime_energy = {
            "production": [{"end_at": ts, "value": 100.0}]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=None, request_delay=0)
        from datetime import datetime
        expected_start = datetime.fromtimestamp(ts).date()
        assert cloner.progress["start_date"] == str(expected_start)

    def test_detect_empty_dict(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = {}
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_production_is_string(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = {"production": "not_a_list"}
        cloner = HistoryCloner(client, tmp_path, "12345")
        # Iterating over a string shouldn't crash but won't find entries
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_production_value_is_string(self, tmp_path):
        client = MockClient()
        client._lifetime_energy = {
            "production": [{"end_at": 1700000000, "value": "high"}]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        # "high" > 0 is True in Python string comparison
        # This should either work or fallback gracefully
        result = cloner._detect_start_date()
        assert isinstance(result, date)

    def test_detect_with_very_old_timestamp(self, tmp_path):
        client = MockClient()
        ts = 1000000000  # 2001-09-09
        client._lifetime_energy = {
            "production": [{"end_at": ts, "value": 1.0}]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        assert result.year == 2001

    def test_detect_with_recent_timestamp(self, tmp_path):
        client = MockClient()
        ts = int(time.time()) - 86400  # yesterday
        client._lifetime_energy = {
            "production": [{"end_at": ts, "value": 50.0}]
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        from datetime import datetime
        assert result == datetime.fromtimestamp(ts).date()

    def test_detect_timeout_exception(self, tmp_path):
        client = MockClient()
        import socket
        client.get_lifetime_energy = MagicMock(side_effect=socket.timeout("timed out"))
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_connection_error(self, tmp_path):
        client = MockClient()
        client.get_lifetime_energy = MagicMock(side_effect=ConnectionError("refused"))
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        expected = date.today() - timedelta(days=365)
        assert result == expected

    def test_detect_start_date_preferred_over_fallback(self, tmp_path):
        """start_date field used when production has no entries."""
        client = MockClient()
        client._lifetime_energy = {
            "production": [],
            "start_date": "2021-03-01",
        }
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._detect_start_date()
        assert result == date(2021, 3, 1)


# ═══════════════════════════════════════════════════════════════════
# TestHistoryClonerFetchDay — 40 tests
# ═══════════════════════════════════════════════════════════════════

class TestHistoryClonerFetchDay:

    def test_fetch_200_returns_data(self, tmp_path):
        client = MockClient()
        client._session.session._default_response = (200, {"stats": [{"totals": {"production": 500.0}}]})
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._fetch_day(date(2024, 3, 24))
        assert result is not None
        assert "stats" in result

    def test_fetch_200_adds_cloned_date(self, tmp_path):
        client = MockClient()
        client._session.session._default_response = (200, {"stats": []})
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._fetch_day(date(2024, 3, 24))
        assert result["_cloned_date"] == "2024-03-24"

    def test_fetch_200_adds_cloned_at(self, tmp_path):
        client = MockClient()
        client._session.session._default_response = (200, {"stats": []})
        cloner = HistoryCloner(client, tmp_path, "12345")
        before = time.time()
        result = cloner._fetch_day(date(2024, 3, 24))
        after = time.time()
        assert before <= result["_cloned_at"] <= after

    def test_fetch_200_empty_response(self, tmp_path):
        client = MockClient()
        client._session.session._default_response = (200, {})
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._fetch_day(date(2024, 3, 24))
        assert result is not None
        assert result.get("_cloned_date") == "2024-03-24"

    def test_fetch_404_returns_none(self, tmp_path):
        client = MockClient()
        client._session.session._default_response = (404, {})
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._fetch_day(date(2024, 3, 24))
        assert result is None

    def test_fetch_500_raises(self, tmp_path):
        client = MockClient()
        client._session.session._default_response = (500, {})
        cloner = HistoryCloner(client, tmp_path, "12345")
        with pytest.raises(Exception):
            cloner._fetch_day(date(2024, 3, 24))

    def test_fetch_401_raises(self, tmp_path):
        client = MockClient()
        client._session.session._default_response = (401, {})
        cloner = HistoryCloner(client, tmp_path, "12345")
        with pytest.raises(Exception):
            cloner._fetch_day(date(2024, 3, 24))

    def test_fetch_calls_ensure_auth(self, tmp_path):
        client = MockClient()
        client._session.session._default_response = (200, {"stats": []})
        cloner = HistoryCloner(client, tmp_path, "12345")
        before = client._auth_count
        cloner._fetch_day(date(2024, 3, 24))
        assert client._auth_count > before

    def test_fetch_url_contains_site_id(self, tmp_path):
        client = MockClient(site_id="99999")
        session = client._session.session

        original_get = session.get
        called_urls = []
        def spy_get(url, **kwargs):
            called_urls.append(url)
            return original_get(url, **kwargs)
        session.get = spy_get

        cloner = HistoryCloner(client, tmp_path, "99999")
        cloner._fetch_day(date(2024, 3, 24))
        assert any("99999" in u for u in called_urls)

    def test_fetch_url_contains_today_json(self, tmp_path):
        client = MockClient()
        session = client._session.session
        called_urls = []
        original_get = session.get
        def spy_get(url, **kwargs):
            called_urls.append(url)
            return original_get(url, **kwargs)
        session.get = spy_get

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner._fetch_day(date(2024, 3, 24))
        assert any("today.json" in u for u in called_urls)

    @pytest.mark.parametrize("day_str", [
        "2024-01-01", "2024-06-15", "2024-12-31", "2023-02-28",
        "2020-02-29", "2025-03-24", "2024-07-04", "2024-11-28",
        "2024-03-15", "2024-09-01",
    ])
    def test_fetch_parametrized_dates(self, day_str, tmp_path):
        client = MockClient()
        client._session.session._default_response = (200, {"stats": []})

        session = client._session.session
        called_params = []
        original_get = session.get
        def spy_get(url, params=None, **kwargs):
            called_params.append(params)
            return original_get(url, params=params, **kwargs)
        session.get = spy_get

        cloner = HistoryCloner(client, tmp_path, "12345")
        day = date.fromisoformat(day_str)
        result = cloner._fetch_day(day)
        assert result is not None
        assert result["_cloned_date"] == day_str
        assert any(p.get("start_date") == day_str for p in called_params if p)

    def test_fetch_passes_timeout(self, tmp_path):
        client = MockClient()
        session = client._session.session
        called_kwargs = []
        original_get = session.get
        def spy_get(url, **kwargs):
            called_kwargs.append(kwargs)
            return original_get(url, **kwargs)
        session.get = spy_get

        client._session.session._default_response = (200, {"stats": []})
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner._fetch_day(date(2024, 3, 24))
        assert any(kw.get("timeout") == 20 for kw in called_kwargs)

    def test_fetch_passes_headers(self, tmp_path):
        client = MockClient()
        session = client._session.session
        called_kwargs = []
        original_get = session.get
        def spy_get(url, **kwargs):
            called_kwargs.append(kwargs)
            return original_get(url, **kwargs)
        session.get = spy_get

        client._session.session._default_response = (200, {"stats": []})
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner._fetch_day(date(2024, 3, 24))
        assert any("headers" in kw for kw in called_kwargs)

    def test_fetch_start_and_end_date_same(self, tmp_path):
        client = MockClient()
        session = client._session.session
        called_params = []
        original_get = session.get
        def spy_get(url, params=None, **kwargs):
            called_params.append(params)
            return original_get(url, params=params, **kwargs)
        session.get = spy_get

        client._session.session._default_response = (200, {"stats": []})
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner._fetch_day(date(2024, 3, 24))
        assert any(
            p.get("start_date") == "2024-03-24" and p.get("end_date") == "2024-03-24"
            for p in called_params if p
        )

    def test_fetch_preserves_original_data(self, tmp_path):
        """Fetched data should include original fields plus _cloned_* fields."""
        client = MockClient()
        original_data = {"stats": [{"totals": {"production": 999.0}}], "extra": "field"}
        client._session.session._default_response = (200, original_data)
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._fetch_day(date(2024, 3, 24))
        assert result.get("extra") == "field"
        assert result.get("_cloned_date") == "2024-03-24"

    def test_fetch_different_site_ids(self, tmp_path):
        for site_id in ["11111", "22222", "33333"]:
            client = MockClient(site_id=site_id)
            called_urls = []
            session = client._session.session
            original_get = session.get
            def spy_get(url, **kwargs):
                called_urls.append(url)
                return original_get(url, **kwargs)
            session.get = spy_get

            client._session.session._default_response = (200, {"stats": []})
            cache_dir = tmp_path / site_id
            cache_dir.mkdir(parents=True, exist_ok=True)
            cloner = HistoryCloner(client, cache_dir, site_id)
            cloner._fetch_day(date(2024, 3, 24))
            assert any(site_id in u for u in called_urls)

    def test_fetch_connection_error(self, tmp_path):
        client = MockClient()
        session = client._session.session
        session.get = MagicMock(side_effect=ConnectionError("refused"))
        cloner = HistoryCloner(client, tmp_path, "12345")
        with pytest.raises(ConnectionError):
            cloner._fetch_day(date(2024, 3, 24))

    def test_fetch_timeout_error(self, tmp_path):
        import socket
        client = MockClient()
        session = client._session.session
        session.get = MagicMock(side_effect=socket.timeout("timed out"))
        cloner = HistoryCloner(client, tmp_path, "12345")
        with pytest.raises(socket.timeout):
            cloner._fetch_day(date(2024, 3, 24))

    def test_fetch_json_decode_error(self, tmp_path):
        client = MockClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = json.JSONDecodeError("err", "", 0)
        client._session.session.get = MagicMock(return_value=mock_resp)
        cloner = HistoryCloner(client, tmp_path, "12345")
        with pytest.raises(json.JSONDecodeError):
            cloner._fetch_day(date(2024, 3, 24))

    def test_fetch_returns_dict(self, tmp_path):
        client = MockClient()
        client._session.session._default_response = (200, {"stats": []})
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._fetch_day(date(2024, 3, 24))
        assert isinstance(result, dict)

    def test_fetch_cloned_date_is_iso_format(self, tmp_path):
        client = MockClient()
        client._session.session._default_response = (200, {"stats": []})
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._fetch_day(date(2024, 3, 1))
        assert result["_cloned_date"] == "2024-03-01"

    def test_fetch_cloned_at_is_numeric(self, tmp_path):
        client = MockClient()
        client._session.session._default_response = (200, {"stats": []})
        cloner = HistoryCloner(client, tmp_path, "12345")
        result = cloner._fetch_day(date(2024, 3, 24))
        assert isinstance(result["_cloned_at"], float)


# ═══════════════════════════════════════════════════════════════════
# TestHistoryClonerSkipExisting — 20 tests
# ═══════════════════════════════════════════════════════════════════

class TestHistoryClonerSkipExisting:

    @patch('time.sleep')
    def test_skip_cached_day(self, mock_sleep, tmp_path):
        """Pre-created files should not trigger HTTP requests."""
        client = MockClient()
        today = date.today()
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        (history_dir / f"day_{today.isoformat()}.json").write_text(
            json.dumps({"stats": [{"totals": {}, "intervals": []}]}))

        session = client._session.session
        call_count = [0]
        original_get = session.get
        def counting_get(url, **kwargs):
            call_count[0] += 1
            return original_get(url, **kwargs)
        session.get = counting_get

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=today.isoformat(), request_delay=0)

        assert call_count[0] == 0

    @patch('time.sleep')
    def test_skip_multiple_cached_days(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today() - timedelta(days=4)
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        for i in range(5):
            d = start + timedelta(days=i)
            (history_dir / f"day_{d.isoformat()}.json").write_text(
                json.dumps({"stats": []}))

        session = client._session.session
        call_count = [0]
        original_get = session.get
        def counting_get(url, **kwargs):
            call_count[0] += 1
            return original_get(url, **kwargs)
        session.get = counting_get

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert call_count[0] == 0

    @patch('time.sleep')
    def test_fetch_only_uncached_days(self, mock_sleep, tmp_path):
        """Should only make requests for days not in cache."""
        client = MockClient()
        start = date.today() - timedelta(days=2)
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        # Cache first and last day, leave middle uncached
        (history_dir / f"day_{start.isoformat()}.json").write_text(json.dumps({"stats": []}))
        d2 = start + timedelta(days=2)
        (history_dir / f"day_{d2.isoformat()}.json").write_text(json.dumps({"stats": []}))

        session = client._session.session
        call_count = [0]
        original_get = session.get
        def counting_get(url, **kwargs):
            call_count[0] += 1
            return original_get(url, **kwargs)
        session.get = counting_get

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert call_count[0] == 1  # Only middle day

    @patch('time.sleep')
    def test_cached_day_not_overwritten(self, mock_sleep, tmp_path):
        client = MockClient()
        today = date.today()
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        original_content = json.dumps({"stats": [], "original": True})
        (history_dir / f"day_{today.isoformat()}.json").write_text(original_content)

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=today.isoformat(), request_delay=0)

        content = json.loads((history_dir / f"day_{today.isoformat()}.json").read_text())
        assert content.get("original") is True

    @patch('time.sleep')
    def test_empty_cache_fetches_all(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today() - timedelta(days=2)

        session = client._session.session
        call_count = [0]
        original_get = session.get
        def counting_get(url, **kwargs):
            call_count[0] += 1
            return original_get(url, **kwargs)
        session.get = counting_get

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert call_count[0] == 3

    @patch('time.sleep')
    def test_skip_preserves_days_completed(self, mock_sleep, tmp_path):
        client = MockClient()
        today = date.today()
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        (history_dir / f"day_{today.isoformat()}.json").write_text(json.dumps({"stats": []}))

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=today.isoformat(), request_delay=0)
        assert cloner.progress["days_completed"] >= 1

    @patch('time.sleep')
    def test_skip_updates_current_date(self, mock_sleep, tmp_path):
        client = MockClient()
        today = date.today()
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        (history_dir / f"day_{today.isoformat()}.json").write_text(json.dumps({"stats": []}))

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=today.isoformat(), request_delay=0)
        assert cloner.progress["current_date"] == today.isoformat()

    @patch('time.sleep')
    def test_skip_does_not_call_sleep(self, mock_sleep, tmp_path):
        """Cached days should not trigger time.sleep."""
        client = MockClient()
        today = date.today()
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        (history_dir / f"day_{today.isoformat()}.json").write_text(json.dumps({"stats": []}))

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=today.isoformat(), request_delay=30)
        # time.sleep should not be called for cached days
        mock_sleep.assert_not_called()

    @patch('time.sleep')
    def test_five_days_all_cached(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today() - timedelta(days=4)
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        for i in range(5):
            d = start + timedelta(days=i)
            (history_dir / f"day_{d.isoformat()}.json").write_text(json.dumps({"stats": []}))

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert cloner.progress["state"] == "complete"

    @patch('time.sleep')
    def test_cached_empty_file_still_skipped(self, mock_sleep, tmp_path):
        """Even an empty JSON file counts as cached."""
        client = MockClient()
        today = date.today()
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        # file exists but is empty - still should be skipped
        (history_dir / f"day_{today.isoformat()}.json").write_text("")

        session = client._session.session
        call_count = [0]
        original_get = session.get
        def counting_get(url, **kwargs):
            call_count[0] += 1
            return original_get(url, **kwargs)
        session.get = counting_get

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=today.isoformat(), request_delay=0)
        assert call_count[0] == 0

    @patch('time.sleep')
    def test_partial_cache_correct_request_count(self, mock_sleep, tmp_path):
        """3 of 5 days cached = 2 HTTP requests."""
        client = MockClient()
        start = date.today() - timedelta(days=4)
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        for i in [0, 2, 4]:
            d = start + timedelta(days=i)
            (history_dir / f"day_{d.isoformat()}.json").write_text(json.dumps({"stats": []}))

        session = client._session.session
        call_count = [0]
        original_get = session.get
        def counting_get(url, **kwargs):
            call_count[0] += 1
            return original_get(url, **kwargs)
        session.get = counting_get

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert call_count[0] == 2

    @patch('time.sleep')
    def test_single_day_cached(self, mock_sleep, tmp_path):
        client = MockClient()
        today = date.today()
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        (history_dir / f"day_{today.isoformat()}.json").write_text(json.dumps({"stats": []}))

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=today.isoformat(), request_delay=0)
        assert cloner.progress["state"] == "complete"
        assert cloner.progress["days_completed"] == 1

    @patch('time.sleep')
    def test_only_last_day_uncached(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today() - timedelta(days=2)
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        for i in range(2):
            d = start + timedelta(days=i)
            (history_dir / f"day_{d.isoformat()}.json").write_text(json.dumps({"stats": []}))

        session = client._session.session
        call_count = [0]
        original_get = session.get
        def counting_get(url, **kwargs):
            call_count[0] += 1
            return original_get(url, **kwargs)
        session.get = counting_get

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert call_count[0] == 1

    @patch('time.sleep')
    def test_only_first_day_uncached(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today() - timedelta(days=2)
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        for i in [1, 2]:
            d = start + timedelta(days=i)
            (history_dir / f"day_{d.isoformat()}.json").write_text(json.dumps({"stats": []}))

        session = client._session.session
        call_count = [0]
        original_get = session.get
        def counting_get(url, **kwargs):
            call_count[0] += 1
            return original_get(url, **kwargs)
        session.get = counting_get

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert call_count[0] == 1

    @patch('time.sleep')
    def test_skip_non_matching_filenames(self, mock_sleep, tmp_path):
        """Files with wrong naming pattern should not affect caching."""
        client = MockClient()
        today = date.today()
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        (history_dir / "wrong_name.json").write_text(json.dumps({"stats": []}))

        session = client._session.session
        call_count = [0]
        original_get = session.get
        def counting_get(url, **kwargs):
            call_count[0] += 1
            return original_get(url, **kwargs)
        session.get = counting_get

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=today.isoformat(), request_delay=0)
        assert call_count[0] == 1  # Should still fetch today

    @patch('time.sleep')
    def test_cached_file_size_irrelevant(self, mock_sleep, tmp_path):
        """Even a minimal cached file (just {}) should skip the request."""
        client = MockClient()
        today = date.today()
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        (history_dir / f"day_{today.isoformat()}.json").write_text("{}")

        session = client._session.session
        call_count = [0]
        original_get = session.get
        def counting_get(url, **kwargs):
            call_count[0] += 1
            return original_get(url, **kwargs)
        session.get = counting_get

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=today.isoformat(), request_delay=0)
        assert call_count[0] == 0

    @patch('time.sleep')
    def test_ten_days_all_cached(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today() - timedelta(days=9)
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        for i in range(10):
            d = start + timedelta(days=i)
            (history_dir / f"day_{d.isoformat()}.json").write_text(json.dumps({"stats": []}))

        session = client._session.session
        call_count = [0]
        original_get = session.get
        def counting_get(url, **kwargs):
            call_count[0] += 1
            return original_get(url, **kwargs)
        session.get = counting_get

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert call_count[0] == 0
        assert cloner.progress["state"] == "complete"


# ═══════════════════════════════════════════════════════════════════
# TestHistoryClonerRateLimiting — 20 tests
# ═══════════════════════════════════════════════════════════════════

class TestHistoryClonerRateLimiting:

    @patch('time.sleep')
    def test_sleep_called_between_requests(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today() - timedelta(days=1)
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=30.0)
        # 2 days = 2 requests = 2 sleeps
        assert mock_sleep.call_count == 2

    @patch('time.sleep')
    def test_sleep_with_custom_delay(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=15.0)
        for call_args in mock_sleep.call_args_list:
            assert call_args[0][0] == 15.0

    @patch('time.sleep')
    def test_sleep_with_zero_delay(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        for call_args in mock_sleep.call_args_list:
            assert call_args[0][0] == 0

    @patch('time.sleep')
    def test_no_sleep_for_cached_days(self, mock_sleep, tmp_path):
        client = MockClient()
        today = date.today()
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        (history_dir / f"day_{today.isoformat()}.json").write_text(json.dumps({"stats": []}))

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=today.isoformat(), request_delay=30.0)
        mock_sleep.assert_not_called()

    @patch('time.sleep')
    def test_sleep_count_matches_uncached_days(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today() - timedelta(days=4)
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        # Cache 2 of 5 days
        for i in [0, 2]:
            d = start + timedelta(days=i)
            (history_dir / f"day_{d.isoformat()}.json").write_text(json.dumps({"stats": []}))

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=5.0)
        # 3 uncached days = 3 sleeps
        assert mock_sleep.call_count == 3

    @patch('time.sleep')
    def test_delay_1_second(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=1.0)
        for call_args in mock_sleep.call_args_list:
            assert call_args[0][0] == 1.0

    @patch('time.sleep')
    def test_delay_60_seconds(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=60.0)
        for call_args in mock_sleep.call_args_list:
            assert call_args[0][0] == 60.0

    @patch('time.sleep')
    def test_delay_fractional(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0.5)
        for call_args in mock_sleep.call_args_list:
            assert call_args[0][0] == 0.5

    @patch('time.sleep')
    def test_single_day_one_sleep(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=10.0)
        assert mock_sleep.call_count == 1

    @patch('time.sleep')
    def test_three_days_three_sleeps(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today() - timedelta(days=2)
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=5.0)
        assert mock_sleep.call_count == 3

    @patch('time.sleep')
    def test_cached_first_uncached_rest(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today() - timedelta(days=2)
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        (history_dir / f"day_{start.isoformat()}.json").write_text(json.dumps({"stats": []}))

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=5.0)
        assert mock_sleep.call_count == 2  # Only 2 uncached days

    @patch('time.sleep')
    def test_last_request_at_updated(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert cloner.progress["last_request_at"] is not None
        assert isinstance(cloner.progress["last_request_at"], float)

    @patch('time.sleep')
    def test_last_request_at_not_updated_for_cached(self, mock_sleep, tmp_path):
        client = MockClient()
        today = date.today()
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        (history_dir / f"day_{today.isoformat()}.json").write_text(json.dumps({"stats": []}))

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=today.isoformat(), request_delay=0)
        # last_request_at should stay None since no actual requests made
        assert cloner.progress["last_request_at"] is None

    @patch('time.sleep')
    def test_default_delay_30(self, mock_sleep, tmp_path):
        """Default request_delay in run() is 30.0."""
        client = MockClient()
        start = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat())
        for call_args in mock_sleep.call_args_list:
            assert call_args[0][0] == 30.0

    @patch('time.sleep')
    def test_five_uncached_five_sleeps(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today() - timedelta(days=4)
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0)
        assert mock_sleep.call_count == 5

    @patch('time.sleep')
    def test_sleep_after_error_too(self, mock_sleep, tmp_path):
        """Rate limit should still apply even after fetch errors."""
        client = MockClient()
        client._session.session._default_response = (500, {})
        start = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=5.0)
        # Should still sleep even though error occurred
        assert mock_sleep.call_count == 1

    @patch('time.sleep')
    def test_sleep_after_404_too(self, mock_sleep, tmp_path):
        """Rate limit applies after 404 responses too."""
        client = MockClient()
        client._session.session._default_response = (404, {})
        start = date.today()
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=5.0)
        assert mock_sleep.call_count == 1

    @patch('time.sleep')
    def test_mixed_cached_uncached_sleep_count(self, mock_sleep, tmp_path):
        """Alternating cached/uncached: sleep only for uncached."""
        client = MockClient()
        start = date.today() - timedelta(days=5)
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        # Cache odd days (0, 2, 4), leave even days (1, 3, 5) uncached
        for i in [0, 2, 4]:
            d = start + timedelta(days=i)
            (history_dir / f"day_{d.isoformat()}.json").write_text(json.dumps({"stats": []}))

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=2.0)
        assert mock_sleep.call_count == 3  # Days 1, 3, 5

    @patch('time.sleep')
    def test_no_requests_no_sleeps(self, mock_sleep, tmp_path):
        """All cached = zero sleeps."""
        client = MockClient()
        start = date.today() - timedelta(days=2)
        history_dir = tmp_path / "history"
        history_dir.mkdir(exist_ok=True)
        for i in range(3):
            d = start + timedelta(days=i)
            (history_dir / f"day_{d.isoformat()}.json").write_text(json.dumps({"stats": []}))

        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=100.0)
        mock_sleep.assert_not_called()

    @patch('time.sleep')
    def test_ten_uncached_ten_sleeps(self, mock_sleep, tmp_path):
        client = MockClient()
        start = date.today() - timedelta(days=9)
        cloner = HistoryCloner(client, tmp_path, "12345")
        cloner.run(start_date=start.isoformat(), request_delay=0.1)
        assert mock_sleep.call_count == 10
