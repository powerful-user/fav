"""Shared test helpers. No network and no real HOME."""
import json
import os
import shutil
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime, timedelta

FakeResponse = namedtuple("FakeResponse", "status headers body")

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

CONFIG_TOML = (
    'client_id = "test-client"\n'
    'redirect_uri = "http://127.0.0.1:8765/callback"\n'
    "daily_ceiling_usd = 1.00\n"
)

SCOPE = "tweet.read users.read bookmark.read offline.access"

TWEET_FIELDS = "attachments,author_id,conversation_id,created_at,lang,public_metrics,referenced_tweets"

NOW = "2026-10-02T12:00:00Z"


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def response(status, payload=None, headers=None):
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return FakeResponse(status, dict(headers or {}), body)


class FakeTransport:
    """Replays queued responses or exceptions in order and records requests."""

    def __init__(self, *items):
        self.queue = list(items)
        self.requests = []

    def push(self, *items):
        self.queue.extend(items)

    def send(self, method, url, headers, body, timeout):
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout}
        )
        if not self.queue:
            raise AssertionError("unexpected request: " + method + " " + url)
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def paths(self):
        """Request paths without host or query string, in order."""
        out = []
        for req in self.requests:
            out.append(req["url"].split("?", 1)[0].replace("https://api.x.com", ""))
        return out

    def query(self, index):
        from urllib.parse import parse_qs, urlsplit

        parsed = parse_qs(urlsplit(self.requests[index]["url"]).query)
        return dict((key, values[0]) for key, values in parsed.items())


class Clock:
    def __init__(self, iso="2026-10-02T12:00:00+00:00"):
        self.now = datetime.fromisoformat(iso)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)


class TempHome(unittest.TestCase):
    """A temporary HOME and XDG directories, exposed as self.env."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wings-x-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {
            "HOME": self.tmp,
            "XDG_CONFIG_HOME": os.path.join(self.tmp, "config"),
            "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
            "XDG_DATA_HOME": os.path.join(self.tmp, "data"),
        }


def config_dir(env):
    return os.path.join(env["XDG_CONFIG_HOME"], "wings", "x")


def write_config(env, text=CONFIG_TOML):
    directory = config_dir(env)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, "config.toml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def write_token(env, access="tok-1", refresh="refresh-1", expires_at="2099-01-01T00:00:00Z",
                scope=SCOPE, user_id="7", username="me"):
    directory = config_dir(env)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, "token.json")
    data = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "scope": scope,
        "user_id": user_id,
        "username": username,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.chmod(path, 0o600)
    return path


def raw_post(post_id, text=None, author_id="42", created_at="2026-10-01T10:00:00.000Z", **extra):
    post = {
        "id": post_id,
        "text": text if text is not None else "post " + post_id,
        "author_id": author_id,
        "created_at": created_at,
        "conversation_id": extra.pop("conversation_id", post_id),
    }
    post.update(extra)
    return post


def page(posts, next_token=None, includes=None, errors=None):
    payload = {"data": posts, "meta": {"result_count": len(posts)}}
    if next_token:
        payload["meta"]["next_token"] = next_token
    if includes:
        payload["includes"] = includes
    if errors:
        payload["errors"] = errors
    return response(200, payload)


def users_response(*pairs):
    """Pairs of (id, username) as a 200 response from /2/users."""
    data = [{"id": uid, "username": name, "name": name.title()} for uid, name in pairs]
    return response(200, {"data": data})
