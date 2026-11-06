import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections import namedtuple
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import urlencode

from fav import __version__
from fav.errors import XError
from fav.parse import iso_z

Response = namedtuple("Response", "status headers body")

BASE_URL = "https://api.x.com"
TIMEOUT = 20
RETRY_DELAY = 2
USER_AGENT = "wings-x/" + __version__

_EPOCH_SECONDS = re.compile(r"^\d{1,12}$")


class TransportError(Exception):
    """A request that got no HTTP response. The message is a short reason without secrets."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies Authorization onto a redirect to any host, so a 3xx comes back as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class UrllibTransport:
    def send(self, method, url, headers, body, timeout):
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with _OPENER.open(request, timeout=timeout) as resp:
                return Response(resp.status, dict(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as e:
            try:
                return Response(e.code, dict(e.headers.items()) if e.headers else {}, e.read())
            except (OSError, http.client.HTTPException) as read_error:
                raise TransportError(type(read_error).__name__) from None
            finally:
                e.close()
        except (OSError, http.client.HTTPException) as e:
            # URLError, TimeoutError, socket.timeout and ConnectionError are all OSError.
            raise TransportError(type(e).__name__) from None


def _header(headers, name):
    if not headers:
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == wanted:
            return value if isinstance(value, str) else None
    return None


def _json_or_none(body):
    if not isinstance(body, (bytes, bytearray)) or not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        return None


def _retry_at(headers):
    value = _header(headers, "x-rate-limit-reset")
    if value is None or not _EPOCH_SECONDS.match(value.strip()):
        return None
    try:
        return iso_z(datetime.fromtimestamp(int(value.strip()), timezone.utc))
    except (ValueError, OverflowError, OSError):
        return None


def _message(status, headers, payload):
    parts = []
    if isinstance(payload, dict):
        for key in ("title", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        errors = payload.get("errors")
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, dict):
                    value = item.get("message")
                    if isinstance(value, str) and value.strip():
                        parts.append(value.strip())
    message = ": ".join(parts) or "HTTP " + str(status)
    request_id = _header(headers, "x-transaction-id")
    if request_id and request_id.strip():
        message += " (request " + request_id.strip() + ")"
    return message


def _mentions(payload, key, *needles):
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return any(needle in lowered for needle in needles)


def _forbidden_code(payload):
    if _mentions(payload, "reason", "client-not-enrolled", "client-forbidden") or _mentions(
        payload, "type", "client-not-enrolled", "client-forbidden"
    ):
        return "X_API_ACCESS_REQUIRED"
    if _mentions(payload, "detail", "credit"):
        return "X_CREDITS_EXHAUSTED"
    if (isinstance(payload, dict) and payload.get("title") == "Authorization Error") or _mentions(
        payload, "detail", "not authorized to see"
    ):
        return "X_PROTECTED"
    return "X_API_ACCESS_REQUIRED"


def map_error(status: int, headers: Mapping[str, str], body: bytes) -> XError:
    payload = _json_or_none(body)
    message = _message(status, headers, payload)
    if status == 400:
        return XError("X_QUERY_INVALID", message)
    if status == 401:
        return XError("X_AUTH_EXPIRED", message)
    if status == 402:
        return XError("X_CREDITS_EXHAUSTED", message)
    if status == 403:
        return XError(_forbidden_code(payload), message)
    if status == 404:
        return XError("X_NOT_FOUND", message)
    if status == 429:
        return XError("X_RATE_LIMITED", message, retry_at=_retry_at(headers))
    return XError("X_UPSTREAM", message)


def _ids(items):
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    return [item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)]


def _charges(kind, payload):
    charges = [(kind, resource_id) for resource_id in _ids(payload.get("data"))]
    includes = payload.get("includes")
    if isinstance(includes, dict):
        for key in ("tweets", "posts"):
            charges.extend(("post_read", resource_id) for resource_id in _ids(includes.get(key)))
        charges.extend(("user_read", resource_id) for resource_id in _ids(includes.get("users")))
        media = includes.get("media")
        if isinstance(media, list):
            charges.extend(
                ("media_read", item["media_key"])
                for item in media
                if isinstance(item, dict) and isinstance(item.get("media_key"), str)
            )
    return charges


def _result_count(payload):
    data = payload.get("data")
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return 1
    return 0


def _elapsed_ms(started):
    return int((time.monotonic() - started) * 1000)


class XClient:
    def __init__(self, transport, *, token, budget, sleep=time.sleep, base_url=BASE_URL):
        self.transport = transport
        self.token = token
        self.budget = budget
        self.sleep = sleep
        self.base_url = base_url

    def _url(self, path, params):
        pairs = sorted((key, str(value)) for key, value in (params or {}).items() if value is not None)
        url = self.base_url + path
        if pairs:
            url += "?" + urlencode(pairs)
        return url

    def _send(self, url, bearer):
        headers = {
            "Authorization": "Bearer " + bearer,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        return self.transport.send("GET", url, headers, None, TIMEOUT)

    def get(self, command, path, params, *, kind=None, max_resources=None, resources=None):
        """GET with a reservation: the count form (kind, max_resources) or the resource form (resources)."""
        count_form = kind is not None or max_resources is not None
        if resources is not None:
            if count_form:
                raise ValueError("pass kind and max_resources, or resources, not both")
            resources = list(resources)
            # The resource form's only caller is the lookup by ID, so its data is read as posts.
            kind = "post_read"

            def reserve():
                return self.budget.reserve_resources(command, path, resources)
        else:
            if kind is None or max_resources is None:
                raise ValueError("pass kind and max_resources, or resources")
            worst = self.budget.worst_case(kind, max_resources)

            def reserve():
                return self.budget.reserve(command, path, worst)
        url = self._url(path, params)
        for attempt in (1, 2):
            # Token first: an auth failure then leaves no open reservation.
            bearer = self.token()
            row_id = reserve()
            started = time.monotonic()
            try:
                resp = self._send(url, bearer)
            except TransportError as e:
                self.budget.settle(row_id, charges=[], http_status=None, duration_ms=_elapsed_ms(started),
                                   result_count=None, error_code="X_UPSTREAM")
                err = XError("X_UPSTREAM", "network error talking to X: " + str(e))
            else:
                duration = _elapsed_ms(started)
                status = resp.status
                if not isinstance(status, int) or isinstance(status, bool):
                    self.budget.settle(row_id, charges=[], http_status=None, duration_ms=duration,
                                       result_count=None, error_code="X_UPSTREAM")
                    raise XError("X_UPSTREAM", "X returned an invalid HTTP status")
                request_id = _header(resp.headers, "x-transaction-id")
                if 200 <= status <= 299:
                    payload = _json_or_none(resp.body)
                    if not isinstance(payload, dict):
                        self.budget.settle(row_id, charges=[], http_status=status, duration_ms=duration,
                                           result_count=None, x_request_id=request_id, error_code="X_UPSTREAM")
                        raise XError("X_UPSTREAM", "X returned a response that is not a JSON object")
                    self.budget.settle(row_id, charges=_charges(kind, payload), http_status=status,
                                       duration_ms=duration, result_count=_result_count(payload),
                                       x_request_id=request_id)
                    return payload
                err = map_error(status, resp.headers, resp.body)
                self.budget.settle(row_id, charges=[], http_status=status, duration_ms=duration,
                                   result_count=None, x_request_id=request_id, error_code=err.code)
                if not 500 <= status <= 599:
                    raise err
            if attempt == 1:
                self.sleep(RETRY_DELAY)
        raise err

    def get_exempt(self, command, path, params, *, bearer):
        url = self._url(path, params)
        started = time.monotonic()
        try:
            resp = self._send(url, bearer)
        except TransportError as e:
            self.budget.record_exempt(command, path, http_status=None, duration_ms=_elapsed_ms(started),
                                      error_code="X_UPSTREAM")
            raise XError("X_UPSTREAM", "network error talking to X: " + str(e)) from None
        duration = _elapsed_ms(started)
        status = resp.status
        if not isinstance(status, int) or isinstance(status, bool):
            self.budget.record_exempt(command, path, http_status=None, duration_ms=duration, error_code="X_UPSTREAM")
            raise XError("X_UPSTREAM", "X returned an invalid HTTP status")
        if 200 <= status <= 299:
            payload = _json_or_none(resp.body)
            if not isinstance(payload, dict):
                self.budget.record_exempt(command, path, http_status=status, duration_ms=duration,
                                          error_code="X_UPSTREAM")
                raise XError("X_UPSTREAM", "X returned a response that is not a JSON object")
            self.budget.record_exempt(command, path, http_status=status, duration_ms=duration)
            return payload
        err = map_error(status, resp.headers, resp.body)
        self.budget.record_exempt(command, path, http_status=status, duration_ms=duration, error_code=err.code)
        raise err
