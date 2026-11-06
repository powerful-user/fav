import base64
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

from wings_x import __version__
from wings_x.client import TIMEOUT, TransportError
from wings_x.errors import XError, local_io_error
from wings_x.parse import iso_z
from wings_x.paths import atomic_write

REQUIRED_SCOPES = ("tweet.read", "users.read", "bookmark.read", "offline.access")
AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
REVOKE_URL = "https://api.x.com/2/oauth2/revoke"
REFRESH_MARGIN = timedelta(seconds=60)
MAX_EXPIRES_IN = 366 * 24 * 3600
LOGIN_HINT = "run wings x auth login"

# A bearer value goes into an HTTP header, so only visible ASCII is allowed.
_HEADER_SAFE = re.compile(r"^[\x21-\x7e]+$")


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_pkce(token_bytes=secrets.token_bytes):
    verifier = _b64url(token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def new_state():
    return secrets.token_urlsafe(24)


def authorize_url(config, state, challenge):
    query = urlencode(
        [
            ("response_type", "code"),
            ("client_id", config.client_id),
            ("redirect_uri", config.redirect_uri),
            ("scope", " ".join(REQUIRED_SCOPES)),
            ("state", state),
            ("code_challenge", challenge),
            ("code_challenge_method", "S256"),
        ]
    )
    return AUTHORIZE_URL + "?" + query


def _same(a, b):
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def parse_redirect(url, expected_state):
    if not isinstance(url, str):
        raise XError("X_BAD_INPUT", "redirect URL not recognized")
    try:
        query = urlsplit(url.strip()).query
    except ValueError:
        raise XError("X_BAD_INPUT", "redirect URL not recognized") from None
    if not query:
        raise XError("X_BAD_INPUT", "redirect URL not recognized; paste the whole URL from the address bar")
    params = dict((key, values[0]) for key, values in parse_qs(query).items())
    state = params.get("state")
    if state is None or not _same(state, expected_state):
        raise XError("X_BAD_INPUT", "redirect URL is not from this login (state does not match)")
    if "error" in params:
        raise XError("X_AUTH_REQUIRED", "X refused the authorization: " + params["error"])
    code = params.get("code")
    if not code:
        raise XError("X_BAD_INPUT", "redirect URL has no authorization code")
    return code


def check_scopes(scope):
    granted = set(scope.split()) if isinstance(scope, str) else set()
    required = set(REQUIRED_SCOPES)
    missing = sorted(required - granted)
    extra = sorted(granted - required)
    if not missing and not extra:
        return
    parts = []
    if missing:
        parts.append("missing " + " ".join(missing))
    if extra:
        parts.append("extra " + " ".join(extra))
    raise XError("X_SCOPE_MISMATCH", "granted scopes are not the four read scopes: " + "; ".join(parts))


def _as_utc(moment):
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_expires_at(text):
    try:
        return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except (TypeError, ValueError, OverflowError):
        return None


def _invalid_token_file():
    return XError("X_AUTH_REQUIRED", "token.json is invalid; " + LOGIN_HINT)


def _validate_token(data):
    if not isinstance(data, dict):
        raise _invalid_token_file()
    for key in ("access_token", "refresh_token"):
        value = data.get(key)
        if not isinstance(value, str) or not _HEADER_SAFE.match(value):
            raise _invalid_token_file()
    if not isinstance(data.get("expires_at"), str) or _parse_expires_at(data["expires_at"]) is None:
        raise _invalid_token_file()
    for key in ("scope", "user_id", "username"):
        if key in data and not isinstance(data[key], str):
            raise _invalid_token_file()
    return data


class TokenStore:
    def __init__(self, paths):
        self.paths = paths

    def load(self):
        try:
            with open(self.paths.token_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, RecursionError):
            # Unreadable (permissions, a directory) is handled like corrupt.
            raise _invalid_token_file() from None
        return _validate_token(data)

    def save(self, token):
        try:
            atomic_write(self.paths.token_file, json.dumps(token, sort_keys=True).encode("utf-8"))
        except OSError as exc:
            raise local_io_error("write", self.paths.token_file, exc) from None

    def delete(self):
        try:
            os.unlink(self.paths.token_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise local_io_error("delete", self.paths.token_file, exc) from None

    @contextmanager
    def lock(self):
        path = self.paths.token_lock
        # Opened on every call so flock also excludes other processes and threads.
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise local_io_error("lock", path, exc) from None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise local_io_error("lock", path, exc) from None
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _json_object(body):
    if not isinstance(body, (bytes, bytearray)) or not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _oauth_error(payload, status, *keys):
    parts = []
    for key in keys:
        value = payload.get(key) if payload else None
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return ": ".join(parts) or "HTTP " + str(status)


def _expires_in(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    # Truncation must not turn a sub-second lifetime into an already-expired token.
    if value < 1 or value > MAX_EXPIRES_IN:
        return None
    return int(value)


def _is_status(status, low, high):
    return isinstance(status, int) and not isinstance(status, bool) and low <= status <= high


class Auth:
    def __init__(self, paths, config, transport, now_fn):
        self.paths = paths
        self.config = config
        self.transport = transport
        self.now_fn = now_fn
        self.store = TokenStore(paths)

    def _now(self):
        return _as_utc(self.now_fn())

    def _post(self, url, form):
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "wings-x/" + __version__,
        }
        # Sent once: token and revoke requests are never retried.
        return self.transport.send("POST", url, headers, urlencode(form).encode("ascii"), TIMEOUT)

    def _token_fields(self, payload):
        """Access token, optional refresh token, expires_at and scope from a 200 body, or None."""
        if payload is None:
            return None
        access = payload.get("access_token")
        if not isinstance(access, str) or not _HEADER_SAFE.match(access):
            return None
        refresh = payload.get("refresh_token")
        if refresh is not None and (not isinstance(refresh, str) or not _HEADER_SAFE.match(refresh)):
            return None
        seconds = _expires_in(payload.get("expires_in"))
        if seconds is None:
            return None
        scope = payload.get("scope")
        if scope is not None and not isinstance(scope, str):
            return None
        return access, refresh, iso_z(self._now() + timedelta(seconds=seconds)), scope

    def exchange_code(self, code, verifier):
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.config.redirect_uri,
            "client_id": self.config.client_id,
            "code_verifier": verifier,
        }
        try:
            resp = self._post(TOKEN_URL, form)
        except TransportError:
            raise XError("X_UPSTREAM", "token exchange got no response from X; " + LOGIN_HINT + " again") from None
        payload = _json_object(resp.body)
        if not _is_status(resp.status, 200, 200):
            raise XError(
                "X_AUTH_REQUIRED",
                "token exchange failed: " + _oauth_error(payload, resp.status, "error", "error_description"),
            )
        fields = self._token_fields(payload)
        if fields is None or fields[1] is None:
            raise XError("X_UPSTREAM", "token exchange response from X is invalid; " + LOGIN_HINT + " again")
        access, refresh, expires_at, scope = fields
        check_scopes(scope)
        return {"access_token": access, "refresh_token": refresh, "expires_at": expires_at, "scope": scope}

    def save_login(self, token, user):
        user_id = user.get("id") if isinstance(user, dict) else None
        username = user.get("username") if isinstance(user, dict) else None
        if not isinstance(user_id, str) or not user_id or not isinstance(username, str) or not username:
            raise XError("X_UPSTREAM", "X returned an invalid account for /2/users/me")
        data = dict(token, user_id=user_id, username=username)
        _validate_token(data)
        with self.store.lock():
            self.store.save(data)

    def _identity(self, key):
        token = self.store.load()
        if token is None or not token.get(key):
            raise XError("X_AUTH_REQUIRED", "not signed in; " + LOGIN_HINT)
        return token[key]

    def user_id(self):
        return self._identity("user_id")

    def username(self):
        return self._identity("username")

    def _fresh(self, token):
        return _parse_expires_at(token["expires_at"]) - self._now() > REFRESH_MARGIN

    def access_token(self):
        token = self.store.load()
        if token is None:
            raise XError("X_AUTH_REQUIRED", "not signed in; " + LOGIN_HINT)
        if self._fresh(token):
            return token["access_token"]
        with self.store.lock():
            # Decide only from this read: another process may have refreshed or logged out.
            token = self.store.load()
            if token is None:
                raise XError("X_AUTH_REQUIRED", "not signed in; " + LOGIN_HINT)
            if self._fresh(token):
                return token["access_token"]
            return self._refresh(token)

    def _refresh(self, token):
        unknown = (
            "the token refresh outcome is unknown; if the next call fails with X_AUTH_EXPIRED, " + LOGIN_HINT
        )
        form = {"grant_type": "refresh_token", "refresh_token": token["refresh_token"],
                "client_id": self.config.client_id}
        try:
            resp = self._post(TOKEN_URL, form)
        except TransportError:
            raise XError("X_UPSTREAM", "token refresh got no response from X; " + unknown) from None
        payload = _json_object(resp.body)
        if not _is_status(resp.status, 200, 200):
            error = payload.get("error") if payload else None
            if error == "invalid_grant" and resp.status in (400, 401):
                raise XError("X_AUTH_EXPIRED", "X rejected the refresh token (invalid_grant); " + LOGIN_HINT)
            raise XError("X_AUTH_EXPIRED", "token refresh failed: " + _oauth_error(payload, resp.status, "error")
                         + "; " + LOGIN_HINT)
        fields = self._token_fields(payload)
        if fields is None:
            raise XError("X_UPSTREAM", "token refresh response from X is invalid; " + unknown)
        access, refresh, expires_at, scope = fields
        updated = dict(token, access_token=access, expires_at=expires_at)
        if refresh:
            updated["refresh_token"] = refresh
        if scope is not None:
            updated["scope"] = scope
        self.store.save(updated)
        return access

    def logout(self):
        with self.store.lock():
            try:
                try:
                    token = self.store.load()
                except XError:
                    return False
                if token is None:
                    return False
                form = {"token": token["refresh_token"], "token_type_hint": "refresh_token",
                        "client_id": self.config.client_id}
                try:
                    resp = self._post(REVOKE_URL, form)
                except TransportError:
                    return False
                return _is_status(resp.status, 200, 200)
            finally:
                self.store.delete()

    def save_app_bearer(self, value):
        stripped = value.strip() if isinstance(value, str) else ""
        if not stripped:
            raise XError("X_BAD_INPUT", "app bearer token is empty")
        if not _HEADER_SAFE.match(stripped):
            raise XError("X_BAD_INPUT", "app bearer token has characters a bearer token cannot contain")
        try:
            atomic_write(self.paths.app_bearer_file, stripped.encode("ascii"))
        except OSError as exc:
            raise local_io_error("write", self.paths.app_bearer_file, exc) from None

    def load_app_bearer(self):
        try:
            with open(self.paths.app_bearer_file, encoding="utf-8") as fh:
                value = fh.read().strip()
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise XError("X_AUTH_REQUIRED", "app-bearer is invalid; run wings x auth app-token") from None
        if not value:
            return None
        if not _HEADER_SAFE.match(value):
            raise XError("X_AUTH_REQUIRED", "app-bearer is invalid; run wings x auth app-token")
        return value
