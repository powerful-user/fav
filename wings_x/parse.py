import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from wings_x.errors import XError

_STATUS_HOSTS = {
    "x.com",
    "www.x.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
}
_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_DURATION = re.compile(r"^(\d+)([dhm])$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _bad(text):
    raise XError("X_BAD_INPUT", "unrecognized input: " + text)


def _split(text):
    candidate = text if "://" in text else "https://" + text
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
    except ValueError:
        _bad(text)
    return parts, host


def parse_post_ref(text: str) -> str:
    text = text.strip()
    if text.isdigit():
        return text
    parts, host = _split(text)
    if host not in _STATUS_HOSTS:
        _bad(text)
    match = re.match(r"/[^/]+/status/(\d+)", parts.path)
    if match is None:
        _bad(text)
    return match.group(1)


def _profile_host(text):
    parts, host = _split(text)
    if host not in _STATUS_HOSTS:
        return None
    return parts


def parse_user_ref(text: str) -> tuple[str, str]:
    text = text.strip()
    if not text:
        _bad(text)
    lower = text.lower()
    looks_like_url = "://" in text or any(
        lower == host or lower.startswith(host + "/") for host in _STATUS_HOSTS
    )
    if looks_like_url:
        parts = _profile_host(text)
        if parts is None:
            _bad(text)
        segments = [part for part in parts.path.split("/") if part]
        if not segments or _USERNAME.fullmatch(segments[0]) is None:
            _bad(text)
        return ("username", segments[0])
    if text.startswith("@"):
        name = text[1:]
        if _USERNAME.fullmatch(name) is None:
            _bad(text)
        return ("username", name)
    if text.isdigit():
        return ("id", text)
    if _USERNAME.fullmatch(text) is None:
        _bad(text)
    return ("username", text)


def parse_date(text: str) -> datetime:
    raw = text.strip()
    if not raw:
        _bad(text)
    if _DATE.fullmatch(raw):
        try:
            year, month, day = (int(part) for part in raw.split("-"))
            return datetime(year, month, day, tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            _bad(text)
    iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        moment = datetime.fromisoformat(iso)
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _bad(text)


def parse_since(text: str, now: datetime) -> datetime:
    raw = text.strip()
    match = _DURATION.fullmatch(raw)
    if match is not None:
        unit = match.group(2)
        try:
            count = int(match.group(1))
            if count <= 0:
                _bad(text)
            if unit == "d":
                return now - timedelta(days=count)
            if unit == "h":
                return now - timedelta(hours=count)
            return now - timedelta(minutes=count)
        except (ValueError, OverflowError):
            _bad(text)
    return parse_date(text)


def iso_z(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    moment = moment.replace(microsecond=0)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def post_url(post_id: str, username: str | None) -> str:
    if username:
        return "https://x.com/" + username + "/status/" + post_id
    return "https://x.com/i/status/" + post_id
