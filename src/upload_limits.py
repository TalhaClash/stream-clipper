"""Per-user daily YouTube Shorts upload limit.

Editors may post at most DAILY_UPLOAD_LIMIT Shorts per day (IST day).
Admins have NO limit. Counts live in upload_counts.json (bind-mounted,
chmod 600) so they survive restarts and deploys. Only a successful
YouTube upload is counted; a failed attempt never consumes the quota.
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

DAILY_UPLOAD_LIMIT = 10
COUNTS_FILE = os.path.join(os.path.dirname(__file__), "upload_counts.json")
_LOCK = threading.Lock()
_KEEP_DAYS = 7
# India has no DST, so a fixed UTC+5:30 offset is exact forever.
_IST = timezone(timedelta(hours=5, minutes=30))


def _today() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d")


def _load() -> dict:
    if not os.path.exists(COUNTS_FILE):
        return {}
    try:
        with open(COUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _save(data: dict) -> None:
    # In-place write: never os.replace over a bind-mounted file (EBUSY).
    with open(COUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(COUNTS_FILE, 0o600)
    except OSError:
        pass


def _prune(data: dict) -> dict:
    cutoff = (datetime.now(_IST).date() - timedelta(days=_KEEP_DAYS)).isoformat()
    for username in list(data):
        days = data[username]
        if not isinstance(days, dict):
            data[username] = {}
            continue
        for day in list(days):
            if day < cutoff:
                del days[day]
        if not days:
            del data[username]
    return data


def get_uploads_today(username: str) -> int:
    with _LOCK:
        return int(_load().get(username, {}).get(_today(), 0) or 0)


def record_upload(username: str) -> int:
    """Count one successful upload for today. Returns today's total."""
    with _LOCK:
        data = _prune(_load())
        today = _today()
        days = data.setdefault(username, {})
        days[today] = int(days.get(today, 0) or 0) + 1
        _save(data)
        return days[today]


def remaining_today(username: str) -> int:
    return max(0, DAILY_UPLOAD_LIMIT - get_uploads_today(username))
