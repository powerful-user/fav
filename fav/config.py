import math
import sqlite3
import sys
import tomllib
from dataclasses import dataclass

from fav.errors import XError

_CONFIG_KEYS = {
    "client_id",
    "redirect_uri",
    "daily_ceiling_usd",
    "search_limit_default",
    "thread_limit_default",
    "folder_pages_max",
    "prices",
}
_PRICE_KEYS = {"post_read", "owned_read", "user_read", "media_read"}


@dataclass(frozen=True)
class Prices:
    post_read: float = 0.005
    owned_read: float = 0.001
    user_read: float = 0.010
    media_read: float = 0.005


@dataclass(frozen=True)
class Config:
    client_id: str
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    daily_ceiling_usd: float = 1.0
    search_limit_default: int = 25
    thread_limit_default: int = 50
    folder_pages_max: int = 10
    prices: Prices = Prices()
    SEARCH_LIMIT_MAX = 100


def _config_error(field, detail):
    raise XError("X_CONFIG", field + " " + detail)


def _required_text(data, key):
    if key not in data:
        _config_error(key, "is required")
    value = data[key]
    if not isinstance(value, str) or value == "":
        _config_error(key, "must be a non-empty string")
    return value


def _optional_text(data, key, default):
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, str) or value == "":
        _config_error(key, "must be a non-empty string")
    return value


def _number(data, key, default, label=None):
    name = label or key
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _config_error(name, "must be a number")
    try:
        number = float(value)
    except OverflowError:
        _config_error(name, "must be a finite non-negative number")
    if not math.isfinite(number) or number < 0:
        _config_error(name, "must be a finite non-negative number")
    return number


def _bounded_int(data, key, default, low, high):
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        _config_error(key, "must be an integer")
    if value < low or value > high:
        _config_error(key, "is out of range")
    return value


def load_config(path: str) -> Config:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise XError("X_CONFIG", "missing config file " + path) from None
    except OSError as exc:
        raise XError("X_CONFIG", "cannot read config file " + path + ": " + (exc.strerror or type(exc).__name__)) from None
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise XError("X_CONFIG", "config.toml: " + str(exc)) from None
    if not isinstance(data, dict):
        raise XError("X_CONFIG", "config.toml must be a table")
    for key in data:
        if key not in _CONFIG_KEYS:
            raise XError("X_CONFIG", "unknown key " + key)
    prices_raw = data.get("prices", {})
    if not isinstance(prices_raw, dict):
        raise XError("X_CONFIG", "prices must be a table")
    for key in prices_raw:
        if key not in _PRICE_KEYS:
            raise XError("X_CONFIG", "unknown key prices." + key)
    prices = Prices(
        post_read=_number(prices_raw, "post_read", Prices.post_read, "prices.post_read"),
        owned_read=_number(prices_raw, "owned_read", Prices.owned_read, "prices.owned_read"),
        user_read=_number(prices_raw, "user_read", Prices.user_read, "prices.user_read"),
        media_read=_number(prices_raw, "media_read", Prices.media_read, "prices.media_read"),
    )
    return Config(
        client_id=_required_text(data, "client_id"),
        redirect_uri=_optional_text(data, "redirect_uri", Config.redirect_uri),
        daily_ceiling_usd=_number(data, "daily_ceiling_usd", Config.daily_ceiling_usd),
        search_limit_default=_bounded_int(data, "search_limit_default", Config.search_limit_default, 10, 100),
        thread_limit_default=_bounded_int(data, "thread_limit_default", Config.thread_limit_default, 1, 500),
        folder_pages_max=_bounded_int(data, "folder_pages_max", Config.folder_pages_max, 1, 100),
        prices=prices,
    )


def check_environment(version_info=sys.version_info, sqlite_module=sqlite3) -> None:
    if tuple(version_info[:2]) < (3, 11):
        raise XError("X_ENVIRONMENT", "Python 3.11 or later is required")
    conn = sqlite_module.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite_module.OperationalError:
        raise XError("X_ENVIRONMENT", "SQLite lacks FTS5") from None
    finally:
        conn.close()
