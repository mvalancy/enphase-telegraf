"""Enlighten portal API client — undocumented endpoints used by the mobile app.

Auth flow (from barneyonline/ha-enphase-energy reverse engineering):
  1. POST /login/login.json → session cookies + user data
  2. If MFA required: POST /app-api/validate_login_otp
  3. GET /app-api/search_sites.json → site IDs
  4. GET /app-api/jwt_token.json → bearer token for service endpoints
  5. Use session cookies + e-auth-token header for all data endpoints

Reference: docs/cloud/barneyonline-api-spec.md (3,290 lines of documented endpoints)
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://enlighten.enphaseenergy.com"
ENTREZ_URL = "https://entrez.enphaseenergy.com"


@dataclass
class EnlightenSession:
    """Authenticated session with the Enlighten portal."""
    email: str
    session: requests.Session = field(default_factory=requests.Session, repr=False)
    site_id: str | None = None
    user_id: str | None = None
    jwt_token: str | None = None
    xsrf_token: str | None = None
    authenticated: bool = False
    auth_time: float = 0.0


class EnlightenClient:
    """Client for the Enlighten web portal / mobile app APIs.

    Usage:
        client = EnlightenClient(email, password)
        client.login()
        data = client.get_site_data()
        battery = client.get_battery_settings()
        events = client.get_events()
    """

    SESSION_TTL = 3600  # re-login after 1 hour

    def __init__(self, email: str, password: str):
        self._email = email
        self._password = password
        self._session = EnlightenSession(email=email)

    @property
    def authenticated(self) -> bool:
        if not self._session.authenticated:
            return False
        if time.time() - self._session.auth_time > self.SESSION_TTL:
            return False
        return True

    def _ensure_auth(self):
        if not self.authenticated:
            self.login()

    # ── Authentication ─────────────────────────────

    def login(self) -> EnlightenSession:
        """Login to Enlighten portal. Returns session with cookies."""
        s = requests.Session()
        s.headers.update({
            "Accept": "application/json",
            "User-Agent": "EnphaseLocal/1.0",
        })

        # Step 1: Login
        log.info("Logging into Enlighten as %s", self._email)
        resp = s.post(f"{BASE_URL}/login/login.json", data={
            "user[email]": self._email,
            "user[password]": self._password,
        }, allow_redirects=False)

        if resp.status_code == 401:
            raise AuthError("Invalid credentials")

        login_data = {}
        try:
            login_data = resp.json()
        except Exception:
            pass

        # Check for MFA
        if login_data.get("mfa_required"):
            raise MFARequired("MFA required — not supported in automated mode")

        # Extract session data
        session_id = login_data.get("session_id", "")
        user_id = str(login_data.get("user_id", login_data.get("manager_token", "")))

        # Step 2: Discover sites
        sites_resp = s.get(f"{BASE_URL}/app-api/search_sites.json",
                           params={"searchText": "", "favourite": "false"})
        sites = []
        try:
            sites_data = sites_resp.json()
            sites = sites_data.get("sites", [])
        except Exception:
            log.warning("Could not parse sites response")

        site_id = str(sites[0]["id"]) if sites else None

        # Step 3: Get JWT token
        jwt_token = None
        try:
            jwt_resp = s.get(f"{BASE_URL}/app-api/jwt_token.json")
            jwt_data = jwt_resp.json()
            jwt_token = jwt_data.get("token", "")
        except Exception:
            log.debug("Could not get JWT token from portal")

        # Extract XSRF token from cookies
        xsrf = s.cookies.get("_enlighten_4_session_xsrf", "")

        self._session = EnlightenSession(
            email=self._email,
            session=s,
            site_id=site_id,
            user_id=user_id,
            jwt_token=jwt_token,
            xsrf_token=xsrf,
            authenticated=True,
            auth_time=time.time(),
        )

        log.info("Enlighten login OK — site_id=%s, user_id=%s, sites=%d",
                 site_id, user_id, len(sites))
        return self._session

    def _headers(self) -> dict:
        """Standard headers for authenticated requests."""
        h = {}
        if self._session.jwt_token:
            h["e-auth-token"] = self._session.jwt_token
        if self._session.xsrf_token:
            h["x-xsrf-token"] = self._session.xsrf_token
        return h

    def _get(self, url: str, **kwargs) -> requests.Response:
        """Authenticated GET with rate limiting."""
        self._ensure_auth()
        time.sleep(0.5)  # be respectful to Enlighten servers
        resp = self._session.session.get(url, headers=self._headers(), timeout=15, **kwargs)
        resp.raise_for_status()
        return resp

    def _post(self, url: str, **kwargs) -> requests.Response:
        """Authenticated POST."""
        self._ensure_auth()
        time.sleep(0.5)
        resp = self._session.session.post(url, headers=self._headers(), timeout=15, **kwargs)
        resp.raise_for_status()
        return resp

    # ── Site Data ──────────────────────────────────

    def get_site_data(self) -> dict:
        """Full site data in one call — the portal's main data endpoint."""
        sid = self._session.site_id
        resp = self._get(f"{BASE_URL}/app-api/{sid}/data.json",
                         params={"app": "1", "device_status": "non_retired", "is_mobile": "0"})
        return resp.json()

    def get_today(self) -> dict:
        """Today's production/consumption/battery stats with 15-min intervals."""
        sid = self._session.site_id
        resp = self._get(f"{BASE_URL}/pv/systems/{sid}/today.json")
        return resp.json()

    def get_latest_power(self) -> dict:
        """Latest power readings (near real-time from cloud perspective)."""
        sid = self._session.site_id
        resp = self._get(f"{BASE_URL}/app-api/{sid}/get_latest_power")
        return resp.json()

    def get_lifetime_energy(self) -> dict:
        """Lifetime energy production data."""
        sid = self._session.site_id
        resp = self._get(f"{BASE_URL}/pv/systems/{sid}/lifetime_energy")
        return resp.json()

    # ── Device Inventory ───────────────────────────

    def get_devices(self) -> dict:
        """Full device inventory for the site."""
        sid = self._session.site_id
        resp = self._get(f"{BASE_URL}/app-api/{sid}/devices.json")
        return resp.json()

    def get_inverters(self) -> dict:
        """Microinverter inventory with production data."""
        sid = self._session.site_id
        resp = self._get(f"{BASE_URL}/app-api/{sid}/inverters.json")
        return resp.json()

    # ── Battery ────────────────────────────────────

    def get_battery_status(self) -> dict:
        """Battery status from cloud."""
        sid = self._session.site_id
        resp = self._get(f"{BASE_URL}/pv/settings/{sid}/battery_status.json")
        return resp.json()

    def get_battery_backup_history(self) -> dict:
        """Battery backup event history."""
        sid = self._session.site_id
        resp = self._get(f"{BASE_URL}/app-api/{sid}/battery_backup_history.json")
        return resp.json()

    def get_battery_settings(self) -> dict:
        """Full battery settings from batteryConfig service."""
        sid = self._session.site_id
        uid = self._session.user_id
        resp = self._get(
            f"{BASE_URL}/service/batteryConfig/api/v1/batterySettings/{sid}",
            params={"userId": uid, "source": "enho"},
        )
        return resp.json()

    def get_battery_schedules(self) -> dict:
        """List all battery charge/discharge schedules."""
        sid = self._session.site_id
        resp = self._get(f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/{sid}/schedules")
        return resp.json()

    # ── Grid ───────────────────────────────────────

    def get_grid_eligibility(self) -> dict:
        """Check if grid control is available for this site."""
        sid = self._session.site_id
        resp = self._get(f"{BASE_URL}/app-api/{sid}/grid_control_check.json")
        return resp.json()

    # ── Events & Alarms ────────────────────────────

    def get_events(self) -> dict:
        """Homeowner events (outages, meter issues, etc.)."""
        sid = self._session.site_id
        resp = self._get(
            f"{BASE_URL}/service/events-platform-service/v1.0/{sid}/events/homeowner")
        return resp.json()

    def get_alarms(self) -> dict:
        """Standing alarms from system dashboard."""
        sid = self._session.site_id
        resp = self._get(
            f"{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/{sid}/alarms")
        return resp.json()

    # ── System Dashboard ───────────────────────────

    def get_dashboard_summary(self) -> dict:
        """System dashboard summary."""
        sid = self._session.site_id
        resp = self._get(
            f"{BASE_URL}/service/system_dashboard/api_internal/cs/sites/{sid}/summary")
        return resp.json()

    def get_dashboard_status(self) -> dict:
        """System dashboard status (health, errors)."""
        sid = self._session.site_id
        resp = self._get(
            f"{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/{sid}/status")
        return resp.json()

    def get_device_tree(self) -> dict:
        """Device communication tree."""
        sid = self._session.site_id
        resp = self._get(
            f"{BASE_URL}/service/system_dashboard/api_internal/dashboard/sites/{sid}/devices-tree")
        return resp.json()

    # ── EV Charger ─────────────────────────────────

    def get_ev_charger_status(self) -> dict | None:
        """EV charger status (if installed)."""
        sid = self._session.site_id
        try:
            resp = self._get(f"{BASE_URL}/service/evse_controller/{sid}/ev_chargers/status")
            return resp.json()
        except Exception:
            return None

    def get_ev_charger_summary(self) -> dict | None:
        """EV charger summary with session data."""
        sid = self._session.site_id
        try:
            resp = self._get(
                f"{BASE_URL}/service/evse_controller/api/v2/{sid}/ev_chargers/summary")
            return resp.json()
        except Exception:
            return None

    # ── HEMS (Home Energy Management) ──────────────

    def get_hems_devices(self) -> dict | None:
        """HEMS device inventory (IQ Energy Router, heat pumps)."""
        sid = self._session.site_id
        try:
            resp = self._get(
                f"https://hems-integration.enphaseenergy.com/api/v1/hems/{sid}/hems-devices")
            return resp.json()
        except Exception:
            return None

    # ── Battery Control ─────────────────────────────

    def _battery_config_headers(self) -> dict:
        """Headers for batteryConfig service endpoints."""
        h = self._headers()
        h["Content-Type"] = "application/json"
        h["Origin"] = "https://battery-profile-ui.enphaseenergy.com"
        if self._session.user_id:
            h["username"] = self._session.user_id
        return h

    def set_battery_mode(self, mode: str) -> dict:
        """Set battery operating mode via cloud.

        Modes: 'self-consumption', 'savings', 'backup', 'economy'
        Maps to Enlighten's storage profile settings.
        """
        sid = self._session.site_id
        uid = self._session.user_id
        self._ensure_auth()
        time.sleep(0.5)
        resp = self._session.session.put(
            f"{BASE_URL}/service/batteryConfig/api/v1/batterySettings/{sid}",
            params={"userId": uid, "source": "enho"},
            headers=self._battery_config_headers(),
            json={"usage": mode},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def set_reserve_soc(self, percent: int) -> dict:
        """Set battery backup reserve percentage (0-100)."""
        sid = self._session.site_id
        uid = self._session.user_id
        self._ensure_auth()
        time.sleep(0.5)
        resp = self._session.session.put(
            f"{BASE_URL}/service/batteryConfig/api/v1/batterySettings/{sid}",
            params={"userId": uid, "source": "enho"},
            headers=self._battery_config_headers(),
            json={"battery_backup_percentage": percent},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def set_charge_from_grid(self, enabled: bool) -> dict:
        """Enable or disable charging battery from grid."""
        sid = self._session.site_id
        uid = self._session.user_id
        self._ensure_auth()
        time.sleep(0.5)
        payload = {"chargeFromGrid": enabled}
        if enabled:
            payload["acceptedItcDisclaimer"] = datetime.now().isoformat()
        resp = self._session.session.put(
            f"{BASE_URL}/service/batteryConfig/api/v1/batterySettings/{sid}",
            params={"userId": uid, "source": "enho"},
            headers=self._battery_config_headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def set_storm_guard(self, enabled: bool) -> dict:
        """Enable or disable storm guard (weather-based backup)."""
        sid = self._session.site_id
        uid = self._session.user_id
        self._ensure_auth()
        time.sleep(0.5)
        resp = self._session.session.put(
            f"{BASE_URL}/service/batteryConfig/api/v1/batterySettings/{sid}",
            params={"userId": uid, "source": "enho"},
            headers=self._battery_config_headers(),
            json={"severe_weather_watch": "enabled" if enabled else "disabled"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Schedule Control ──────────────────────────

    def create_schedule(self, schedule_type: str, start_time: str,
                        end_time: str, days: list[int], limit: int = 0) -> dict:
        """Create a battery charge/discharge schedule.

        Args:
            schedule_type: 'CFG' (charge from grid), 'DTG' (discharge to grid),
                           'RBD' (reserve battery discharge)
            start_time: 'HH:MM' format
            end_time: 'HH:MM' format
            days: list of day numbers (0=Sunday, 1=Monday, ... 6=Saturday)
            limit: power limit in watts (0 = unlimited)
        """
        sid = self._session.site_id
        self._ensure_auth()
        time.sleep(0.5)
        # Get timezone from site data
        tz = "America/Los_Angeles"  # default, should be read from site data
        try:
            site = cache_get("site_data") if 'cache_get' in dir() else None
            if site and site.get("data", {}).get("timezone"):
                tz = site["data"]["timezone"]
        except Exception:
            pass

        resp = self._session.session.post(
            f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/{sid}/schedules",
            headers=self._battery_config_headers(),
            json={
                "timezone": tz,
                "startTime": start_time,
                "endTime": end_time,
                "limit": limit,
                "scheduleType": schedule_type,
                "days": days,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_schedule(self, schedule_id: str) -> dict:
        """Delete a battery schedule by ID."""
        sid = self._session.site_id
        self._ensure_auth()
        time.sleep(0.5)
        resp = self._session.session.post(
            f"{BASE_URL}/service/batteryConfig/api/v1/battery/sites/{sid}/schedules/{schedule_id}/delete",
            headers=self._battery_config_headers(),
            json={},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ── EV Charger Control ────────────────────────

    def start_ev_charging(self, serial: str) -> dict | None:
        """Start EV charging (if charger installed)."""
        sid = self._session.site_id
        try:
            resp = self._post(
                f"{BASE_URL}/service/evse_controller/{sid}/ev_chargers/{serial}/start_charging")
            return resp.json()
        except Exception:
            return None

    def stop_ev_charging(self, serial: str) -> dict | None:
        """Stop EV charging."""
        sid = self._session.site_id
        try:
            resp = self._post(
                f"{BASE_URL}/service/evse_controller/{sid}/ev_chargers/{serial}/stop_charging")
            return resp.json()
        except Exception:
            return None

    # ── Live Streaming ─────────────────────────────

    def get_livestream_flags(self) -> dict:
        """Check if live streaming is available for this site."""
        sid = self._session.site_id
        resp = self._get(f"{BASE_URL}/app-api/{sid}/show_livestream")
        return resp.json()

    def get_live_status(self) -> dict | None:
        """Get live site status stream data.

        The Enlighten web UI has a "Live Status" feature that provides
        higher-frequency updates than the standard 15-min today.json.
        """
        sid = self._session.site_id
        try:
            # The HEMS live stream endpoints
            resp = self._session.session.put(
                f"https://hems-integration.enphaseenergy.com/api/v1/hems/{sid}/live-stream/status",
                headers=self._headers(),
                json={"status": True},
                timeout=15,
            )
            if resp.ok:
                return resp.json()
        except Exception:
            pass

        # Fallback: the SSE subscribe endpoint
        try:
            resp = self._get(
                f"{BASE_URL}/service/evse_sse/subscribeEvent",
                params={"key": sid},
            )
            return resp.json()
        except Exception:
            pass

        return None

    # ── Gateway Token ──────────────────────────────

    def get_gateway_token(self, serial: str) -> dict:
        """Get JWT token for local gateway access."""
        self._ensure_auth()
        resp = self._session.session.get(
            f"{BASE_URL}/entrez-auth-token",
            params={"serial_num": serial},
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Bulk Scrape ────────────────────────────────

    def scrape_all(self) -> dict:
        """Scrape everything available from the portal in one pass.

        Returns a dict with all data organized by category.
        Skips endpoints that fail (optional components not installed).
        """
        self._ensure_auth()
        result = {"scraped_at": time.time(), "site_id": self._session.site_id}

        endpoints = [
            ("site_data", self.get_site_data),
            ("today", self.get_today),
            ("latest_power", self.get_latest_power),
            ("lifetime_energy", self.get_lifetime_energy),
            ("devices", self.get_devices),
            ("inverters", self.get_inverters),
            ("battery_status", self.get_battery_status),
            ("battery_backup_history", self.get_battery_backup_history),
            ("battery_settings", self.get_battery_settings),
            ("battery_schedules", self.get_battery_schedules),
            ("grid_eligibility", self.get_grid_eligibility),
            ("events", self.get_events),
            ("alarms", self.get_alarms),
            ("dashboard_summary", self.get_dashboard_summary),
            ("dashboard_status", self.get_dashboard_status),
            ("device_tree", self.get_device_tree),
            ("ev_charger_status", self.get_ev_charger_status),
            ("ev_charger_summary", self.get_ev_charger_summary),
            ("hems_devices", self.get_hems_devices),
            ("livestream_flags", self.get_livestream_flags),
        ]

        for name, fn in endpoints:
            try:
                data = fn()
                if data is not None:
                    result[name] = data
                    log.info("  %s: OK", name)
                else:
                    log.debug("  %s: no data (component not installed?)", name)
            except Exception as e:
                log.debug("  %s: failed (%s)", name, type(e).__name__)
                result[name] = {"_error": str(e)[:200]}

        log.info("Scrape complete — %d/%d endpoints returned data",
                 sum(1 for k, v in result.items()
                     if k not in ("scraped_at", "site_id") and not isinstance(v, dict) or
                     (isinstance(v, dict) and "_error" not in v)),
                 len(endpoints))
        return result


class AuthError(Exception):
    pass

class MFARequired(AuthError):
    pass
