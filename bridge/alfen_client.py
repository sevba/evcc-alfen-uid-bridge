"""
Alfen Single Pro-line local HTTPS API client.

Session discipline:
  - All requests must go over the same TCP connection (keep-alive).
  - Only one management session is allowed at a time on the charger.
  - Login/logout only when handling a connect event; never held open at idle.
  - Rate-limited to LOGIN_RATE_MAX logins per LOGIN_RATE_WINDOW_S seconds.
"""

import collections
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

# Full-UID patterns (preferred — these always contain the complete tag UID)
_RE_NFC_READER = re.compile(r"Reader \d+ Got NFC tag:\s*(\S+)", re.I)
_RE_AUTH_LINE = re.compile(r"\bTag\s+(\S+)\s+is authorised", re.I)

# Fallback: state-line pattern. Has socket number but UID may be truncated
# (firmware renders short-form UIDs without padding).
_RE_SOCKET = re.compile(r"Socket #(\d+)", re.I)
_RE_STATE_TAG = re.compile(r"\btag:\s*([0-9A-Fa-f]+)", re.I)

_ISO_LEN = 24  # chars in "2026-06-24T10:00:00.000Z" or "2026-06-24T10:00:00+0200"

# Look back this many seconds before the connect timestamp when searching for tags,
# because the RFID tap is logged before EVCC fires the connected=true event.
_LOOKBACK_S = 300


def _parse_log_line(raw: str) -> Optional[Tuple[int, datetime, str]]:
    """Return (log_id, timestamp, message) or None if the line can't be parsed.

    Actual Alfen log format (verified from live device):
      {lid}_{ISO8601Z}:{LEVEL}:{file}:{linenum}:{message}
    Example:
      843136_2026-06-24T11:34:59.535Z:INFO:taskMain.c:5996:Socket #1: ...
    """
    try:
        lid_str, rest = raw.split("_", 1)
        lid = int(lid_str)
        ts_str = rest[:_ISO_LEN]
        # The char at position _ISO_LEN is ':' (verified); skip it.
        rest = rest[_ISO_LEN + 1:]
        # rest = "LEVEL:filename:linenum:message"
        _, _, _, message = rest.split(":", 3)
        # Alfen uses Z suffix for UTC; fromisoformat only handles Z in Python 3.11+.
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return lid, ts, message
    except Exception:
        return None


def _extract_tag_entry(message: str, socket: int, full_uid_only: bool = False) -> Optional[str]:
    """Return the RFID UID from a log message, or None.

    Three patterns, in preference order:
    1. NFC reader line — full UID, no socket prefix (tag.c):
         Reader 0 Got NFC tag: 04AABBCCDDEEFF
    2. Auth/whitelist line — full UID, no socket prefix (tag.c):
         Tag 04AABBCCDDEEFF is authorised by server, white list updated
    3. State line — has socket, but UID may be TRUNCATED by firmware (taskMain.c):
         Socket #1: main state: ..., tag: 1234

    Patterns 1 and 2 are accepted for any socket. Pattern 3 is a fallback for
    normal (short-window) connects. For extended-lookback scans (full_uid_only=True)
    pattern 3 is skipped — state lines from earlier sessions would produce stale or
    truncated UIDs that override the correct NFC reader entry.
    """
    # Pattern 1: NFC reader — full UID, always accept
    m = _RE_NFC_READER.search(message)
    if m:
        return m.group(1)

    # Pattern 2: Auth/whitelist — full UID, always accept
    m = _RE_AUTH_LINE.search(message)
    if m:
        return m.group(1)

    # Pattern 3: State line — skip for extended lookback scans
    if not full_uid_only:
        m_sock = _RE_SOCKET.search(message)
        if m_sock and int(m_sock.group(1)) == socket:
            m_tag = _RE_STATE_TAG.search(message)
            if m_tag:
                uid = m_tag.group(1)
                log.debug("alfen: tag from state-line (may be truncated): %s", uid)
                return uid

    return None


def uid_hash(uid: str) -> str:
    """Short SHA-256 hash for privacy-safe logging."""
    return hashlib.sha256(uid.encode()).hexdigest()[:12]


class RateLimiter:
    def __init__(self, max_calls: int, window_s: int):
        self._max = max_calls
        self._window = window_s
        self._timestamps: collections.deque = collections.deque()

    def acquire(self) -> bool:
        """Return True and record the call, or False if rate limit is exceeded."""
        now = time.monotonic()
        cutoff = now - self._window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= self._max:
            return False
        self._timestamps.append(now)
        return True

    def wait_s(self) -> float:
        """Seconds until the oldest call exits the window."""
        if not self._timestamps:
            return 0.0
        oldest = self._timestamps[0]
        return max(0.0, (oldest + self._window) - time.monotonic())


class AlfenClient:
    def __init__(self, host: str, username: str, password: str,
                 socket: int, tls_verify: bool,
                 rate_limiter: RateLimiter, log_uid_plaintext: bool = False):
        self._base = f"https://{host}"
        self._username = username
        self._password = password
        self._socket = socket
        self._tls_verify = tls_verify
        self._limiter = rate_limiter
        self._log_uid_plaintext = log_uid_plaintext
        self._session: Optional[requests.Session] = None

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.verify = self._tls_verify
        return s

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def login(self) -> bool:
        """Login to the Alfen API. Returns True on success."""
        if not self._limiter.acquire():
            wait = self._limiter.wait_s()
            log.warning("alfen: login rate limit reached, waiting %.1fs", wait)
            time.sleep(wait)
            if not self._limiter.acquire():
                log.error("alfen: still rate limited after wait, aborting")
                return False

        self._session = self._new_session()
        try:
            resp = self._session.post(
                self._url("/api/login"),
                json={
                    "username": self._username,
                    "password": self._password,
                    "displayname": "evcc-uid-bridge",
                },
                timeout=10,
            )
            resp.raise_for_status()
            log.debug("alfen: logged in")
            return True
        except Exception as exc:
            log.error("alfen: login failed: %s", exc)
            self._close_session()
            return False

    def logout(self):
        """Logout and close the TCP connection."""
        if not self._session:
            return
        try:
            self._session.post(self._url("/api/logout"), json=None, timeout=5)
            log.debug("alfen: logged out")
        except Exception as exc:
            log.warning("alfen: logout error (ignored): %s", exc)
        finally:
            self._close_session()

    def _close_session(self):
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    def _get_log_page(self, offset: int) -> Optional[str]:
        """Fetch a page of the Alfen event log (raw text)."""
        try:
            resp = self._session.get(
                self._url("/api/log"),
                params={"offset": offset},
                timeout=10,
            )
            if resp.status_code == 401:
                return None  # signal re-login needed
            resp.raise_for_status()
            return resp.text
        except requests.HTTPError as exc:
            log.error("alfen: log fetch HTTP error: %s", exc)
            return None
        except Exception as exc:
            log.error("alfen: log fetch error: %s", exc)
            return None

    def get_latest_tag(self, since: datetime, lookback_s: int = _LOOKBACK_S,
                       max_pages: int = 20) -> Optional[str]:
        """
        Scan the device log for the most recent RFID tag on our socket
        within the session window.

        The RFID tap is logged BEFORE EVCC fires the connected=true event,
        so we look back `lookback_s` seconds before `since`. Pass a large
        lookback_s (e.g. 259200) on startup to recover sessions from hours ago.

        max_pages caps how many 128-entry pages are fetched. Raise it for
        extended-lookback scans where the tap may be deep in the log history.

        Returns the UID string with the highest log-ID found, or None.
        """
        since_aware = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
        from datetime import timedelta
        window_start = since_aware - timedelta(seconds=lookback_s)

        best_lid = -1
        best_uid: Optional[str] = None
        offset = 0
        pages_searched = 0

        while pages_searched < max_pages:
            raw = self._get_log_page(offset)
            if raw is None:
                return None

            lines = raw.splitlines()
            if not lines:
                break

            hit_before_window = False
            for line in reversed(lines):  # most recent first within each page
                line = line.strip()
                if not line:
                    continue
                parsed = _parse_log_line(line)
                if parsed is None:
                    continue
                lid, ts, message = parsed

                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                if ts < window_start:
                    hit_before_window = True
                    break  # entries are too old — stop paginating

                uid = _extract_tag_entry(message, self._socket,
                                         full_uid_only=(lookback_s > _LOOKBACK_S))
                if uid and lid > best_lid:
                    best_lid = lid
                    best_uid = uid
                    uid_label = uid if self._log_uid_plaintext else uid_hash(uid)
                    log.debug("alfen: tag candidate uid=%s lid=%d ts=%s",
                              uid_label, lid, ts.isoformat())

            if hit_before_window:
                break

            offset += 128
            pages_searched += 1

        return best_uid

    def get_bo_connection(self) -> Optional[str]:
        """Return the BOConnection value from /api/info, or None on error.

        Returns "online" when connected to the back office (OCPP CSMS),
        a different string (e.g. "offline") when not connected.
        """
        try:
            resp = self._session.get(self._url("/api/info"), timeout=5)
            if resp.status_code == 401:
                log.warning("alfen: get_bo_connection 401")
                return None
            resp.raise_for_status()
            data = resp.json()
            return data.get("BOConnection")
        except Exception as exc:
            log.error("alfen: get_bo_connection error: %s", exc)
            return None

    def get_property(self, prop_id: str) -> Optional[object]:
        """Fetch a single Alfen property value from /api/prop.

        Returns the first value element, or None on any error or missing data.
        Response format: [{"id": "...", "value": [<val>], ...}]
        """
        try:
            resp = self._session.get(
                self._url("/api/prop"),
                params={"ids[]": prop_id},
                timeout=5,
            )
            if resp.status_code == 401:
                log.warning("alfen: get_property 401 for %s", prop_id)
                return None
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                values = data[0].get("value", [])
                if values:
                    return values[0]
            return None
        except Exception as exc:
            log.error("alfen: get_property(%s) error: %s", prop_id, exc)
            return None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.logout()
