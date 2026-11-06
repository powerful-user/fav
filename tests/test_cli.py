import io
import json
import os
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.helpers import (CONFIG_TOML, SCOPE, Clock, FakeTransport, TempHome, config_dir, fixture, page, raw_post,
                           response, users_response, write_config, write_token)
from tests.media_helpers import lookup
from fav import cli
from fav.errors import XError

TOKEN_OK = {"token_type": "bearer", "expires_in": 7200, "access_token": "tok-2", "scope": SCOPE,
            "refresh_token": "refresh-2"}
ME = {"data": {"id": "7", "username": "me", "name": "Me"}}


class CliBase(TempHome):
    def setUp(self):
        super().setUp()
        write_config(self.env)
        write_token(self.env)
        self.clock = Clock()
        self.transport = FakeTransport()

    def run_cli(self, *argv, stdin="", getpass_fn=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), env=self.env, stdout=out, stderr=err, stdin=io.StringIO(stdin),
                        transport=self.transport, now_fn=self.clock, sleep=lambda s: None, getpass_fn=getpass_fn,
                        loopback=False)
        return code, out.getvalue(), err.getvalue()

    def token_path(self):
        return os.path.join(config_dir(self.env), "token.json")


class CliTest(CliBase):
    def test_help(self):
        code, out, _ = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("usage: wings x", out)
        for word in ("search", "post", "thread", "user", "bookmarks", "usage", "auth"):
            self.assertIn(word, out)

    def test_help_without_config(self):
        os.remove(os.path.join(config_dir(self.env), "config.toml"))
        code, out, _ = self.run_cli("search", "--help")
        self.assertEqual(code, 0)
        self.assertIn("--budget", out)
        self.assertIn("10-100", out)

    def test_unknown_command(self):
        code, _, err = self.run_cli("frobnicate")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_search_json(self):
        self.transport.push(response(200, fixture("search_recent.json")), users_response(("42", "example"), ("43", "other")),
                            lookup(("1002", ["3_1"])))
        code, out, err = self.run_cli("search", "agents", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["next"], "tok2")
        self.assertEqual(err, "")

    def test_search_plain(self):
        self.transport.push(response(200, fixture("search_recent.json")), users_response(("42", "example"), ("43", "other")),
                            lookup(("1002", ["3_1"])))
        code, out, _ = self.run_cli("search", "agents", "--limit", "10", "--since", "3d")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("@example · 2026-10-01 10:00Z · live · https://x.com/example/status/1001\n"))
        self.assertIn("next: tok2\n", out)
        self.assertEqual(self.transport.query(0)["max_results"], "10")

    def test_error_json(self):
        self.transport.push(response(429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": "1790943300"}))
        code, out, err = self.run_cli("search", "agents", "--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(data["error"]["code"], "X_RATE_LIMITED")
        self.assertEqual(data["error"]["retry_at"], "2026-10-02T12:15:00Z")
        self.assertEqual(err, "")

    def test_error_plain(self):
        self.transport.push(response(429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": "1790943300"}))
        code, out, err = self.run_cli("search", "agents")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("X_RATE_LIMITED: "))

    def test_budget_refusal_and_flag(self):
        write_config(self.env, CONFIG_TOML.replace("1.00", "0.10"))
        code, _, err = self.run_cli("search", "agents")
        self.assertEqual(code, 1)
        self.assertIn("X_BUDGET_EXCEEDED", err)
        self.assertEqual(self.transport.requests, [])
        self.transport.push(response(200, fixture("search_recent.json")), users_response(("42", "example"), ("43", "other")),
                            lookup(("1002", ["3_1"])))
        code, _, err = self.run_cli("search", "agents", "--budget", "0.50")
        self.assertEqual(code, 0, err)

    def test_bad_budget(self):
        code, _, _ = self.run_cli("search", "agents", "--budget", "-1")
        self.assertEqual(code, 2)

    def test_post_user_thread(self):
        self.transport.push(response(200, fixture("post_with_refs.json")), users_response(("42", "example"), ("44", "other")))
        code, out, err = self.run_cli("post", "1001", "--json")
        self.assertEqual(code, 0, err)
        self.assertIn("referenced_posts", json.loads(out))
        self.transport.push(response(200, {"data": {"id": "42", "username": "example", "name": "Example"}}))
        code, out, _ = self.run_cli("user", "@example")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("@example · Example · https://x.com/example\n"))
        self.transport.push(response(200, {"data": raw_post("100")}), page([]))
        code, out, err = self.run_cli("thread", "100", "--limit", "5", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual([p["id"] for p in json.loads(out)["items"]], ["100"])

    def test_auth_login_paste_back(self):
        os.remove(self.token_path())
        self.transport.push(response(200, TOKEN_OK), response(200, ME))
        with mock.patch("fav.auth.new_state", return_value="state-1"):
            code, out, err = self.run_cli("auth", "login",
                                          stdin="http://127.0.0.1:8765/callback?state=state-1&code=abc\n")
        self.assertEqual(code, 0, err)
        self.assertIn("https://x.com/i/oauth2/authorize?", out)
        self.assertIn("logged in as @me", out)
        with open(self.token_path(), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["access_token"], "tok-2")
        self.assertEqual(saved["user_id"], "7")
        self.assertEqual(self.transport.paths(), ["/2/oauth2/token", "/2/users/me"])
        self.assertEqual(self.transport.requests[1]["headers"]["Authorization"], "Bearer tok-2")

    def test_auth_login_json(self):
        os.remove(self.token_path())
        self.transport.push(response(200, TOKEN_OK), response(200, ME))
        with mock.patch("fav.auth.new_state", return_value="state-1"):
            code, out, err = self.run_cli("auth", "login", "--json",
                                          stdin="http://127.0.0.1:8765/callback?state=state-1&code=abc\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"username": "me", "user_id": "7"})
        self.assertIn("https://x.com/i/oauth2/authorize?", err)

    def test_auth_logout_and_app_token_json(self):
        self.transport.push(response(200, {"revoked": True}))
        code, out, _ = self.run_cli("auth", "logout", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"logged_out": True, "revoked": True})
        code, out, _ = self.run_cli("auth", "app-token", "--json", getpass_fn=lambda prompt: "app-1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"app_token": True})

    def test_auth_logout_local_io_json(self):
        os.remove(self.token_path())
        os.mkdir(self.token_path())
        code, out, err = self.run_cli("auth", "logout", "--json")
        self.assertEqual(code, 1)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], "X_LOCAL_IO")
        self.assertIn(self.token_path(), error["message"])
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.transport.requests, [])

    def test_auth_login_bad_paste(self):
        os.remove(self.token_path())
        with mock.patch("fav.auth.new_state", return_value="state-1"):
            code, _, err = self.run_cli("auth", "login", stdin="http://127.0.0.1:8765/callback?state=evil&code=abc\n")
        self.assertEqual(code, 1)
        self.assertIn("X_BAD_INPUT", err)
        self.assertFalse(os.path.exists(self.token_path()))

    def test_auth_status(self):
        self.transport.push(response(200, ME))
        code, out, _ = self.run_cli("auth", "status")
        self.assertEqual(code, 0)
        self.assertIn("@me", out)
        self.assertIn("expires 2099-01-01T00:00:00Z", out)
        self.assertIn("app token: missing", out)
        self.transport.push(response(200, ME))
        code, out, _ = self.run_cli("auth", "status", "--json")
        self.assertEqual(json.loads(out), {"username": "me", "user_id": "7", "scope": SCOPE,
                                           "expires_at": "2099-01-01T00:00:00Z", "app_token": False})

    def test_auth_status_scope_mismatch(self):
        write_token(self.env, scope=SCOPE + " tweet.write")
        self.transport.push(response(200, ME))
        code, _, err = self.run_cli("auth", "status")
        self.assertEqual(code, 1)
        self.assertIn("X_SCOPE_MISMATCH", err)

    def test_auth_app_token(self):
        code, out, _ = self.run_cli("auth", "app-token", getpass_fn=lambda prompt: "app-1")
        self.assertEqual(code, 0)
        self.assertIn("app token saved", out)
        with open(os.path.join(config_dir(self.env), "app-bearer"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "app-1")

    def test_auth_logout(self):
        self.transport.push(response(200, {"revoked": True}))
        code, out, _ = self.run_cli("auth", "logout")
        self.assertEqual(code, 0)
        self.assertIn("logged out", out)
        self.assertFalse(os.path.exists(self.token_path()))

    def test_not_logged_in(self):
        os.remove(self.token_path())
        code, _, err = self.run_cli("search", "agents")
        self.assertEqual(code, 1)
        self.assertIn("X_AUTH_REQUIRED", err)

    def test_usage_without_app_token(self):
        code, out, _ = self.run_cli("usage", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIsNone(data["x"])
        self.assertIn("app-bearer", data["x_note"])
        self.assertEqual(data["local"][0], {"day": "2026-10-02", "estimated_usd": 0.0, "reserved_usd": 0.0,
                                            "abandoned": 0,
                                            "charged": {"post_read": 0, "owned_read": 0, "user_read": 0,
                                                        "media_read": 0}})
        self.assertEqual(len(data["local"]), 7)
        self.assertEqual(data["ceiling_usd"], 1.0)
        self.assertEqual(data["remaining_usd"], 1.0)

    def test_usage_with_app_token(self):
        self.run_cli("auth", "app-token", getpass_fn=lambda prompt: "app-1")
        credits = {"data": {"total_balance": 12.5, "prepaid_balance": 12.5, "free_balance": 0, "free_grants": []}}
        self.transport.push(response(200, credits),
                            response(200, {"data": {"daily_project_usage": {"usage": []}}}))
        code, out, _ = self.run_cli("usage", "--days", "3", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["x"]["credits"], credits)
        self.assertEqual(self.transport.paths(), ["/2/usage/credits", "/2/usage/tweets"])
        self.assertEqual(self.transport.requests[0]["headers"]["Authorization"], "Bearer app-1")
        self.assertEqual(self.transport.query(1)["days"], "3")
        self.assertEqual(len(data["local"]), 3)
        self.transport.push(response(200, credits), response(200, {"data": {}}))
        code, out, _ = self.run_cli("usage")
        self.assertEqual(code, 0)
        self.assertIn("X balance $12.50", out)

    def test_usage_x_error_is_a_note(self):
        self.run_cli("auth", "app-token", getpass_fn=lambda prompt: "app-1")
        self.transport.push(response(401, {"title": "Unauthorized"}))
        code, out, _ = self.run_cli("usage", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIsNone(data["x"])
        self.assertIn("X_AUTH_EXPIRED", data["x_note"])

    def test_usage_plain(self):
        code, out, _ = self.run_cli("usage")
        self.assertEqual(code, 0)
        self.assertIn("2026-10-02", out)
        self.assertIn("ceiling $1.00", out)

    def test_config_missing(self):
        os.remove(os.path.join(config_dir(self.env), "config.toml"))
        code, _, err = self.run_cli("search", "agents")
        self.assertEqual(code, 1)
        self.assertIn("X_CONFIG", err)

    def test_environment_check(self):
        with mock.patch("fav.config.check_environment", side_effect=XError("X_ENVIRONMENT", "old")):
            code, _, err = self.run_cli("search", "agents")
        self.assertEqual(code, 1)
        self.assertIn("X_ENVIRONMENT", err)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LoopbackTest(CliBase):
    def setUp(self):
        super().setUp()
        os.remove(self.token_path())
        self.port = free_port()
        write_config(self.env, CONFIG_TOML.replace("8765", str(self.port)))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def login(self, stdin="", timeout=10.0):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("fav.auth.new_state", return_value="state-1"):
            code = cli.main(["auth", "login"], env=self.env, stdout=out, stderr=err, stdin=io.StringIO(stdin),
                            transport=self.transport, now_fn=self.clock, sleep=lambda s: None, loopback=True,
                            login_timeout=timeout)
        return code, out.getvalue(), err.getvalue()

    def hit(self, query):
        url = "http://127.0.0.1:" + str(self.port) + "/callback?" + query
        deadline = time.time() + 5
        while True:
            try:
                with self.opener.open(url, timeout=2) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                e.close()
                return e.code
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.05)

    def assertPortFree(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", self.port))

    def test_callback_logs_in(self):
        self.transport.push(response(200, TOKEN_OK), response(200, ME))
        statuses = []
        hitter = threading.Thread(target=lambda: statuses.append(self.hit("state=state-1&code=abc")))
        hitter.start()
        code, out, err = self.login()
        hitter.join()
        self.assertEqual(code, 0, err)
        self.assertIn("logged in as @me", out)
        self.assertEqual(statuses, [200])

    def test_wrong_state_is_ignored(self):
        self.transport.push(response(200, TOKEN_OK), response(200, ME))
        statuses = []

        def hits():
            statuses.append(self.hit("state=evil&code=bad"))
            statuses.append(self.hit("state=state-1&code=abc"))

        hitter = threading.Thread(target=hits)
        hitter.start()
        code, _, err = self.login()
        hitter.join()
        self.assertEqual(code, 0, err)
        self.assertEqual(statuses, [400, 200])
        form = self.transport.requests[0]["body"].decode("ascii")
        self.assertIn("code=abc", form)
        self.assertNotIn("code=bad", form)

    def test_stdin_wins_and_server_closes(self):
        self.transport.push(response(200, TOKEN_OK), response(200, ME))
        code, _, err = self.login(stdin="http://127.0.0.1:" + str(self.port) + "/callback?state=state-1&code=abc\n")
        self.assertEqual(code, 0, err)
        self.assertPortFree()

    def test_port_in_use_falls_back_to_stdin(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
            busy.bind(("127.0.0.1", self.port))
            busy.listen(1)
            self.transport.push(response(200, TOKEN_OK), response(200, ME))
            code, _, err = self.login(stdin="http://127.0.0.1:" + str(self.port) + "/callback?state=state-1&code=abc\n")
        self.assertEqual(code, 0, err)
        self.assertIn("loopback unavailable", err)

    def test_timeout(self):
        code, _, err = self.login(timeout=0.3)
        self.assertEqual(code, 1)
        self.assertIn("X_AUTH_REQUIRED", err)
        self.assertEqual(self.transport.requests, [])
        self.assertPortFree()


if __name__ == "__main__":
    unittest.main()
