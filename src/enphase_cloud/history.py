"""Historical data clone from Enlighten — trickle-download system lifetime.

Downloads production, consumption, battery, and grid data in 7-day windows,
paging from system install date to now. Respectful rate: ~1 request per 30s,
completes a full system clone in a few hours.

Progress is tracked in a JSON file so it can resume across restarts.

Uses the portal today.json endpoint which provides 15-min interval data
per day. One request per day of history.
"""

import json
import logging
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class HistoryCloner:
    """Background task that downloads complete system history."""

    def __init__(self, enlighten_client, cache_dir: Path, site_id: str):
        self.client = enlighten_client
        self.cache_dir = cache_dir
        self.site_id = site_id
        self.history_dir = cache_dir / "history"
        self.history_dir.mkdir(exist_ok=True)
        self.progress_file = cache_dir / "history_progress.json"
        self.progress = self._load_progress()
        self._running = False
        self._on_progress: Callable | None = None

    def _load_progress(self) -> dict:
        if self.progress_file.exists():
            try:
                return json.loads(self.progress_file.read_text())
            except Exception:
                pass
        return {
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

    def _save_progress(self):
        try:
            self.progress_file.write_text(json.dumps(self.progress, indent=2, default=str))
        except Exception:
            pass

    @property
    def status(self) -> dict:
        p = self.progress.copy()
        p["running"] = self._running
        p["cached_days"] = len(list(self.history_dir.glob("day_*.json")))
        if p["days_total"] > 0:
            p["percent_complete"] = round(p["days_completed"] / p["days_total"] * 100, 1)
        else:
            p["percent_complete"] = 0
        return p

    def run(self, start_date: str | None = None, request_delay: float = 30.0):
        """Run the historical clone. Blocks until complete or error.

        Args:
            start_date: ISO date string (YYYY-MM-DD) for system install date.
                        If None, tries to detect from Enlighten data.
            request_delay: seconds between requests (default 30 = ~2 req/min)
        """
        self._running = True

        try:
            self.client._ensure_auth()

            # Determine date range
            if start_date:
                first_day = date.fromisoformat(start_date)
            else:
                first_day = self._detect_start_date()

            last_day = date.today()
            total_days = (last_day - first_day).days + 1

            # Resume from where we left off
            resume_date = first_day
            if self.progress.get("current_date"):
                try:
                    resume_date = date.fromisoformat(self.progress["current_date"])
                    # Move to next uncompleted day
                    resume_date += timedelta(days=1)
                except Exception:
                    pass

            self.progress.update({
                "state": "running",
                "start_date": str(first_day),
                "end_date": str(last_day),
                "days_total": total_days,
                "started_at": self.progress.get("started_at") or time.time(),
            })
            self._save_progress()

            log.info("History clone: %s → %s (%d days, resuming from %s)",
                     first_day, last_day, total_days, resume_date)

            current = resume_date
            while current <= last_day:
                # Check if we already have this day
                day_file = self.history_dir / f"day_{current.isoformat()}.json"
                if day_file.exists():
                    self.progress["days_completed"] = (current - first_day).days + 1
                    self.progress["current_date"] = str(current)
                    current += timedelta(days=1)
                    continue

                # Fetch this day's data
                try:
                    data = self._fetch_day(current)
                    if data:
                        day_file.write_text(json.dumps(data, default=str, indent=2))
                        log.info("  %s: OK (%d bytes)", current, len(json.dumps(data, default=str)))
                    else:
                        log.debug("  %s: no data", current)
                except Exception as e:
                    self.progress["errors"] += 1
                    self.progress["last_error"] = f"{current}: {type(e).__name__}: {str(e)[:100]}"
                    log.warning("  %s: %s", current, type(e).__name__)

                self.progress["days_completed"] = (current - first_day).days + 1
                self.progress["current_date"] = str(current)
                self.progress["last_request_at"] = time.time()
                self._save_progress()

                current += timedelta(days=1)

                # Rate limit — be respectful
                time.sleep(request_delay)

            self.progress["state"] = "complete"
            self._save_progress()
            log.info("History clone complete: %d days", total_days)

        except Exception as e:
            self.progress["state"] = "error"
            self.progress["last_error"] = str(e)[:200]
            self._save_progress()
            log.exception("History clone error")
        finally:
            self._running = False

    def _detect_start_date(self) -> date:
        """Try to detect system install date from Enlighten data."""
        try:
            # The lifetime_energy endpoint has the earliest data
            data = self.client.get_lifetime_energy()
            if isinstance(data, dict):
                # Look for earliest non-zero production
                if "production" in data:
                    for entry in data.get("production", []):
                        ts = entry.get("end_at") or entry.get("timestamp")
                        if ts and entry.get("value", 0) > 0:
                            return datetime.fromtimestamp(ts).date()
                # Or check start_date field
                if "start_date" in data:
                    return date.fromisoformat(data["start_date"])
        except Exception:
            pass

        # Fallback: 1 year ago
        log.warning("Could not detect install date — defaulting to 1 year ago")
        return date.today() - timedelta(days=365)

    def _fetch_day(self, day: date) -> dict | None:
        """Fetch a single day's data from Enlighten.

        Uses the today.json endpoint with a date parameter to get
        15-minute interval data for any historical date.
        """
        import requests

        self.client._ensure_auth()
        sid = self.client._session.site_id

        # The today.json endpoint accepts start_date/end_date params
        url = f"https://enlighten.enphaseenergy.com/pv/systems/{sid}/today.json"
        resp = self.client._session.session.get(
            url,
            params={"start_date": day.isoformat(), "end_date": day.isoformat()},
            headers=self.client._headers(),
            timeout=20,
        )

        if resp.status_code == 200:
            data = resp.json()
            data["_cloned_date"] = day.isoformat()
            data["_cloned_at"] = time.time()
            return data
        elif resp.status_code == 404:
            return None
        else:
            resp.raise_for_status()
            return None
