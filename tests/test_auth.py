import base64
import hashlib
import json
import os
import stat
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from tests.helpers import SCOPE, Clock, FakeTransport, TempHome, response, write_config, write_token
from wings_x import auth
from wings_x import config as cfg
from wings_x import paths
from wings_x.client import TransportError
from wings_x.errors import XError

TOKEN_OK = {"token_type": "bearer", "expires_in": 7200, "access_token": "tok-2", "scope": SCOPE,
            "refresh_token": "refresh-2"}


class PkceTest(unittest.TestCase):
    def test_pkce(self):
        verifier, challenge = auth.make_pkce(token_bytes=lambda n: b"\x01" * n)
        self.assertEqual(verifier, "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE")
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        self.assertEqual(challenge, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii"))

    def test_random_by_default(self):
        self.assertNotEqual(auth.make_pkce()[0], auth.make_pkce()[0])

    def test_new_state(self):
        state = auth.new_state()
        self.assertGreaterEqual(len(state), 22)
        self.assertNotEqual(state, auth.new_state())


class UrlTest(TempHome):
    def setUp(self):
        super().setUp()
        self.config = cfg.load_config(write_config(self.env))

    def test_authorize_url(self):
        url = auth.authorize_url(self.config, "state-1", "chal-1")
        parts = urlsplit(url)
        self.assertEqual(parts.scheme + "://" + parts.netloc + parts.path, "https://x.com/i/oauth2/authorize")
        query = dict((k, v[0]) for k, v in parse_qs(parts.query).items())
        self.assertEqual(query, {"response_type": "code", "client_id": "test-client",
                                 "redirect_uri": "http://127.0.0.1:8765/callback", "scope": SCOPE,
                                 "state": "state-1", "code_challenge": "chal-1", "code_challenge_method": "S256"})

    def test_parse_redirect(self):
        self.assertEqual(auth.parse_redirect("http://127.0.0.1:8765/callback?state=state-1&code=abc", "state-1"), "abc")
        self.assertEqual(auth.parse_redirect("  http://127.0.0.1:8765/callback?code=abc&state=state-1\n", "state-1"),
                         "abc")

    def test_parse_redirect_errors(self):
        cases = [
            ("http://127.0.0.1:8765/callback?state=other&code=abc", "X_BAD_INPUT"),
            ("http://127.0.0.1:8765/callback?state=state-1", "X_BAD_INPUT"),
            ("http://127.0.0.1:8765/callback?state=state-1&error=access_denied", "X_AUTH_REQUIRED"),
            ("not a url", "X_BAD_INPUT"),
        ]
        for url, code in cases:
            with self.assertRaises(XError) as ctx:
                auth.parse_redirect(url, "state-1")
            self.assertEqual(ctx.exception.code, code, url)


class ScopeTest(unittest.TestCase):
    def test_exact_any_order(self):
        auth.check_scopes("offline.access bookmark.read users.read tweet.read")

    def test_missing(self):
        with self.assertRaises(XError) as ctx:
            auth.check_scopes("tweet.read users.read offline.access")
        self.assertEqual(ctx.exception.code, "X_SCOPE_MISMATCH")
        self.assertIn("bookmark.read", ctx.exception.message)

    def test_extra(self):
        with self.assertRaises(XError) as ctx:
            auth.check_scopes(SCOPE + " tweet.write")
        self.assertEqual(ctx.exception.code, "X_SCOPE_MISMATCH")
        self.assertIn("tweet.write", ctx.exception.message)


class AuthTest(TempHome):
    def setUp(self):
        super().setUp()
        self.config = cfg.load_config(write_config(self.env))
        self.paths = paths.resolve_paths(self.env)
        paths.ensure_dirs(self.paths)
        self.clock = Clock()
        self.transport = FakeTransport()
        self.auth = auth.Auth(self.paths, self.config, self.transport, now_fn=self.clock)

    def token_file(self):
        with open(self.paths.token_file, encoding="utf-8") as fh:
            return json.load(fh)

    def form(self, index=0):
        body = self.transport.requests[index]["body"].decode("ascii")
        return dict((k, v[0]) for k, v in parse_qs(body).items())

    def test_exchange_code(self):
        self.transport.push(response(200, TOKEN_OK))
        token = self.auth.exchange_code("abc", "verif-1")
        self.assertEqual(token, {"access_token": "tok-2", "refresh_token": "refresh-2",
                                 "expires_at": "2026-10-02T14:00:00Z", "scope": SCOPE})
        req = self.transport.requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["url"], "https://api.x.com/2/oauth2/token")
        self.assertEqual(req["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(self.form(), {"grant_type": "authorization_code", "code": "abc",
                                       "redirect_uri": "http://127.0.0.1:8765/callback", "client_id": "test-client",
                                       "code_verifier": "verif-1"})
        self.assertFalse(os.path.exists(self.paths.token_file))

    def test_exchange_rejects_extra_scope(self):
        self.transport.push(response(200, dict(TOKEN_OK, scope=SCOPE + " tweet.write")))
        with self.assertRaises(XError) as ctx:
            self.auth.exchange_code("abc", "verif-1")
        self.assertEqual(ctx.exception.code, "X_SCOPE_MISMATCH")

    def test_exchange_error(self):
        self.transport.push(response(400, {"error": "invalid_request", "error_description": "bad code"}))
        with self.assertRaises(XError) as ctx:
            self.auth.exchange_code("abc", "verif-1")
        self.assertEqual(ctx.exception.code, "X_AUTH_REQUIRED")
        self.assertIn("invalid_request", ctx.exception.message)

    def test_unreadable_credential_files_need_login(self):
        os.mkdir(self.paths.token_file)
        os.mkdir(self.paths.app_bearer_file)
        for load in (self.auth.store.load, self.auth.load_app_bearer):
            with self.subTest(load=load.__name__):
                with self.assertRaises(XError) as ctx:
                    load()
                self.assertEqual(ctx.exception.code, "X_AUTH_REQUIRED")

    def test_exchange_rejects_expiry_under_one_second(self):
        self.transport.push(response(200, dict(TOKEN_OK, expires_in=0.5)))
        with self.assertRaises(XError) as ctx:
            self.auth.exchange_code("abc", "verif-1")
        self.assertEqual(ctx.exception.code, "X_UPSTREAM")

    def test_save_login(self):
        token = {"access_token": "tok-2", "refresh_token": "refresh-2", "expires_at": "2026-10-02T14:00:00Z",
                 "scope": SCOPE}
        self.auth.save_login(token, {"id": "7", "username": "me"})
        self.assertEqual(self.token_file(), dict(token, user_id="7", username="me"))
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.token_file).st_mode), 0o600)

    def test_user_identity(self):
        write_token(self.env)
        self.assertEqual(self.auth.user_id(), "7")
        self.assertEqual(self.auth.username(), "me")

    def test_user_identity_missing(self):
        with self.assertRaises(XError) as ctx:
            self.auth.user_id()
        self.assertEqual(ctx.exception.code, "X_AUTH_REQUIRED")

    def test_access_token_fresh(self):
        write_token(self.env, expires_at="2026-10-02T12:05:00Z")
        self.assertEqual(self.auth.access_token(), "tok-1")
        self.assertEqual(self.transport.requests, [])

    def test_access_token_missing(self):
        with self.assertRaises(XError) as ctx:
            self.auth.access_token()
        self.assertEqual(ctx.exception.code, "X_AUTH_REQUIRED")

    def test_refresh_when_near_expiry(self):
        write_token(self.env, expires_at="2026-10-02T12:00:30Z")
        self.transport.push(response(200, TOKEN_OK))
        self.assertEqual(self.auth.access_token(), "tok-2")
        self.assertEqual(self.transport.requests[0]["url"], "https://api.x.com/2/oauth2/token")
        self.assertEqual(self.form(), {"grant_type": "refresh_token", "refresh_token": "refresh-1",
                                       "client_id": "test-client"})
        saved = self.token_file()
        self.assertEqual(saved["access_token"], "tok-2")
        self.assertEqual(saved["refresh_token"], "refresh-2")
        self.assertEqual(saved["expires_at"], "2026-10-02T14:00:00Z")
        self.assertEqual(saved["user_id"], "7")
        self.assertEqual(saved["username"], "me")
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.token_file).st_mode), 0o600)

    def test_refresh_without_rotation_keeps_refresh_token(self):
        write_token(self.env, expires_at="2026-10-02T11:00:00Z")
        no_rotation = dict(TOKEN_OK)
        del no_rotation["refresh_token"]
        self.transport.push(response(200, no_rotation))
        self.auth.access_token()
        self.assertEqual(self.token_file()["refresh_token"], "refresh-1")

    def test_refresh_invalid_grant(self):
        write_token(self.env, expires_at="2026-10-02T11:00:00Z")
        self.transport.push(response(400, {"error": "invalid_grant",
                                           "error_description": "Value passed for the token was invalid."}))
        with self.assertRaises(XError) as ctx:
            self.auth.access_token()
        self.assertEqual(ctx.exception.code, "X_AUTH_EXPIRED")

    def test_lost_refresh_response_not_retried(self):
        write_token(self.env, expires_at="2026-10-02T11:00:00Z")
        before = self.token_file()
        self.transport.push(TransportError("timed out"))
        with self.assertRaises(XError) as ctx:
            self.auth.access_token()
        self.assertEqual(ctx.exception.code, "X_UPSTREAM")
        self.assertIn("auth login", ctx.exception.message)
        self.assertNotIn("refresh-1", ctx.exception.message)
        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.token_file(), before)

    def test_concurrent_refresh_makes_one_request(self):
        write_token(self.env, expires_at="2026-10-02T11:00:00Z")

        class Slow(FakeTransport):
            def send(self, method, url, headers, body, timeout):
                time.sleep(0.2)
                return FakeTransport.send(self, method, url, headers, body, timeout)

        transport = Slow(response(200, TOKEN_OK))
        results = []

        def worker():
            other = auth.Auth(self.paths, self.config, transport, now_fn=self.clock)
            results.append(other.access_token())

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, ["tok-2", "tok-2"])
        self.assertEqual(len(transport.requests), 1)

    def test_logout_while_refresh_waits(self):
        write_token(self.env, expires_at="2026-10-02T11:00:00Z")
        holder = auth.TokenStore(self.paths)
        entered = threading.Event()
        release = threading.Event()

        def logout_side():
            with holder.lock():
                entered.set()
                release.wait(5)
                holder.delete()

        side = threading.Thread(target=logout_side)
        side.start()
        entered.wait(5)
        errors = []

        def refresher():
            try:
                self.auth.access_token()
            except XError as e:
                errors.append(e.code)

        worker = threading.Thread(target=refresher)
        worker.start()
        time.sleep(0.2)
        release.set()
        side.join()
        worker.join()
        self.assertEqual(errors, ["X_AUTH_REQUIRED"])
        self.assertEqual(self.transport.requests, [])
        self.assertFalse(os.path.exists(self.paths.token_file))

    def test_logout(self):
        write_token(self.env)
        self.transport.push(response(200, {"revoked": True}))
        self.assertTrue(self.auth.logout())
        req = self.transport.requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["url"], "https://api.x.com/2/oauth2/revoke")
        self.assertEqual(self.form(), {"token": "refresh-1", "token_type_hint": "refresh_token",
                                       "client_id": "test-client"})
        self.assertFalse(os.path.exists(self.paths.token_file))

    def test_logout_revoke_failure_still_deletes(self):
        write_token(self.env)
        self.transport.push(response(500))
        self.assertFalse(self.auth.logout())
        self.assertEqual(len(self.transport.requests), 1)
        self.assertFalse(os.path.exists(self.paths.token_file))

    def test_logout_network_failure_still_deletes(self):
        write_token(self.env)
        self.transport.push(TransportError("down"))
        self.assertFalse(self.auth.logout())
        self.assertFalse(os.path.exists(self.paths.token_file))

    def test_logout_without_token(self):
        self.assertFalse(self.auth.logout())
        self.assertEqual(self.transport.requests, [])

    def test_app_bearer(self):
        self.assertIsNone(self.auth.load_app_bearer())
        self.auth.save_app_bearer("app-1\n")
        self.assertEqual(self.auth.load_app_bearer(), "app-1")
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.app_bearer_file).st_mode), 0o600)

    def test_token_save_failure_is_local_io(self):
        os.mkdir(self.paths.token_file)
        with self.assertRaises(XError) as ctx:
            self.auth.store.save({"access_token": "sentinel-access", "refresh_token": "sentinel-refresh"})
        self.assertEqual(ctx.exception.code, "X_LOCAL_IO")
        self.assertIn(self.paths.token_file, ctx.exception.message)
        self.assertNotIn("sentinel-access", str(ctx.exception))
        self.assertNotIn("sentinel-refresh", str(ctx.exception))

    def test_logout_delete_failure_is_local_io(self):
        os.mkdir(self.paths.token_file)
        with self.assertRaises(XError) as ctx:
            self.auth.logout()
        self.assertEqual(ctx.exception.code, "X_LOCAL_IO")
        self.assertIn(self.paths.token_file, ctx.exception.message)
        self.assertEqual(self.transport.requests, [])

    def test_token_lock_failure_is_local_io(self):
        os.mkdir(self.paths.token_lock)
        with self.assertRaises(XError) as ctx:
            with self.auth.store.lock():
                self.fail("lock body ran")
        self.assertEqual(ctx.exception.code, "X_LOCAL_IO")
        self.assertIn(self.paths.token_lock, ctx.exception.message)

    def test_error_inside_token_lock_is_not_translated(self):
        failure = OSError(5, "Input/output error")
        with self.assertRaises(OSError) as ctx:
            with self.auth.store.lock():
                raise failure
        self.assertIs(ctx.exception, failure)

    def test_app_bearer_save_failure_is_local_io(self):
        os.mkdir(self.paths.app_bearer_file)
        with self.assertRaises(XError) as ctx:
            self.auth.save_app_bearer("sentinel-bearer")
        self.assertEqual(ctx.exception.code, "X_LOCAL_IO")
        self.assertIn(self.paths.app_bearer_file, ctx.exception.message)
        self.assertNotIn("sentinel-bearer", str(ctx.exception))

    def test_app_bearer_rejects_empty(self):
        with self.assertRaises(XError) as ctx:
            self.auth.save_app_bearer("   ")
        self.assertEqual(ctx.exception.code, "X_BAD_INPUT")


if __name__ == "__main__":
    unittest.main()
