import http.server
import threading
import unittest
import urllib.request
from unittest import mock

from tests.helpers import CONFIG_TOML, Clock, FakeResponse, FakeTransport, TempHome, response, write_config
from fav import budget as bd
from fav import client as cl
from fav import config as cfg
from fav import paths
from fav import store as st
from fav.errors import XError


class ClientTest(TempHome):
    def setUp(self):
        super().setUp()
        write_config(self.env)
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        self.config = cfg.load_config(p.config_file)
        self.store = st.Store(p.db_file)
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.budget = bd.Budget(self.store, self.config, invocation_id="inv", now_fn=self.clock,
                                pid_alive=lambda pid: True)
        self.sleeps = []
        self.transport = FakeTransport()
        self.client = cl.XClient(self.transport, token=lambda: "tok-1", budget=self.budget, sleep=self.sleeps.append)

    def rows(self):
        return [dict(r) for r in self.store.conn.execute("SELECT * FROM ledger ORDER BY id")]

    def search(self):
        return self.client.get("search", "/2/tweets/search/recent", {"query": "agents", "max_results": 25},
                               kind="post_read", max_resources=25)

    def test_success(self):
        self.transport.push(response(200, {"data": [{"id": "1"}, {"id": "2"}], "meta": {"result_count": 2}},
                                     {"x-transaction-id": "abc"}))
        payload = self.search()
        self.assertEqual(payload["meta"]["result_count"], 2)
        (req,) = self.transport.requests
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["url"], "https://api.x.com/2/tweets/search/recent?max_results=25&query=agents")
        self.assertEqual(req["headers"]["Authorization"], "Bearer tok-1")
        self.assertTrue(req["headers"]["User-Agent"].startswith("wings-x/"))
        self.assertEqual(req["timeout"], 20)
        self.assertIsNone(req["body"])
        (r,) = self.rows()
        self.assertAlmostEqual(r["reserved_usd"], 0.125)
        self.assertAlmostEqual(r["est_usd"], 0.010)
        self.assertEqual(r["result_count"], 2)
        self.assertEqual(r["x_request_id"], "abc")
        self.assertEqual(r["state"], "settled")
        self.assertEqual(r["command"], "search")

    def test_none_params_dropped_and_no_query(self):
        self.transport.push(response(200, {"data": {"id": "1"}}))
        self.client.get("post", "/2/tweets/1", {"expansions": None}, kind="post_read", max_resources=1)
        self.assertEqual(self.transport.requests[0]["url"], "https://api.x.com/2/tweets/1")

    def test_includes_are_charged(self):
        self.transport.push(response(200, {"data": {"id": "1"}, "includes": {"tweets": [{"id": "9"}],
                                                                           "users": [{"id": "42"}]}}))
        self.client.get("post", "/2/tweets/1", None, kind="post_read", max_resources=4)
        r = self.rows()[0]
        self.assertAlmostEqual(r["est_usd"], 0.020)
        self.assertEqual(r["resources"], 3)

    def test_renamed_includes_are_charged(self):
        self.transport.push(response(200, {"data": {"id": "1"}, "includes": {"posts": [{"id": "9"}]}}))
        self.client.get("post", "/2/tweets/1", None, kind="post_read", max_resources=4)
        self.assertAlmostEqual(self.rows()[0]["est_usd"], 0.010)

    def test_budget_flag_covers_retries(self):
        config = cfg.load_config(write_config(self.env, CONFIG_TOML.replace("1.00", "0")))
        b = bd.Budget(self.store, config, invocation_id="inv-b", extra_usd=0.25, now_fn=self.clock,
                      pid_alive=lambda pid: True)
        client = cl.XClient(self.transport, token=lambda: "tok-1", budget=b, sleep=self.sleeps.append)
        self.transport.push(response(503), response(200, {"data": []}))
        client.get("search", "/2/tweets/search/recent", {"query": "a"}, kind="post_read", max_resources=25)
        with self.assertRaises(XError) as ctx:
            client.get("search", "/2/tweets/search/recent", {"query": "a"}, kind="post_read", max_resources=25)
        self.assertEqual(ctx.exception.code, "X_BUDGET_EXCEEDED")
        self.assertEqual(len(self.transport.requests), 2)

    def test_refused_before_request(self):
        with self.assertRaises(XError) as ctx:
            self.client.get("search", "/2/tweets/search/recent", {"query": "a"}, kind="post_read", max_resources=300)
        self.assertEqual(ctx.exception.code, "X_BUDGET_EXCEEDED")
        self.assertEqual(self.transport.requests, [])

    def test_5xx_retried_once(self):
        self.transport.push(response(503), response(200, {"data": []}))
        self.search()
        self.assertEqual(len(self.transport.requests), 2)
        self.assertEqual(self.sleeps, [2])
        self.assertEqual([(r["state"], r["error_code"]) for r in self.rows()],
                         [("settled", "X_UPSTREAM"), ("settled", None)])

    def test_5xx_twice(self):
        self.transport.push(response(503), response(502))
        with self.assertRaises(XError) as ctx:
            self.search()
        self.assertEqual(ctx.exception.code, "X_UPSTREAM")
        self.assertEqual(len(self.transport.requests), 2)
        self.assertEqual(self.sleeps, [2])

    def test_network_error_retried(self):
        self.transport.push(cl.TransportError("connection reset"), response(200, {"data": []}))
        self.search()
        self.assertIsNone(self.rows()[0]["http_status"])
        self.assertEqual(self.rows()[0]["error_code"], "X_UPSTREAM")

    def test_rate_limited(self):
        self.transport.push(response(429, {"title": "Too Many Requests", "detail": "Too Many Requests"},
                                     {"x-rate-limit-reset": "1790943300"}))
        with self.assertRaises(XError) as ctx:
            self.search()
        self.assertEqual(ctx.exception.code, "X_RATE_LIMITED")
        self.assertEqual(ctx.exception.retry_at, "2026-10-02T12:15:00Z")
        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.rows()[0]["error_code"], "X_RATE_LIMITED")

    def assertMaps(self, status, payload, code, headers=None):
        self.transport.push(response(status, payload, headers))
        with self.assertRaises(XError) as ctx:
            self.search()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_error_mapping(self):
        self.assertMaps(401, {"title": "Unauthorized", "detail": "Unauthorized"}, "X_AUTH_EXPIRED")
        self.assertMaps(402, {"title": "Payment Required", "detail": "Your enrolled account does not have any credits"},
                        "X_CREDITS_EXHAUSTED")
        self.assertMaps(403, {"title": "Client Forbidden", "reason": "client-not-enrolled",
                              "type": "https://api.twitter.com/2/problems/client-forbidden",
                              "detail": "This request must be made using an approved developer account."},
                        "X_API_ACCESS_REQUIRED")
        self.assertMaps(403, {"title": "Authorization Error",
                              "detail": "Sorry, you are not authorized to see the Post with id: [5]."}, "X_PROTECTED")
        self.assertMaps(403, {"title": "Forbidden", "detail": "Your account has exhausted its credits."},
                        "X_CREDITS_EXHAUSTED")
        self.assertMaps(403, {"title": "Forbidden", "detail": "Something else."}, "X_API_ACCESS_REQUIRED")
        self.assertMaps(404, {"title": "Not Found Error", "detail": "Could not find tweet"}, "X_NOT_FOUND")
        e = self.assertMaps(400, {"title": "Invalid Request",
                                  "detail": "One or more parameters to your request was invalid.",
                                  "errors": [{"message": "There were errors processing your request: "
                                                         "Ambiguous use of and as a keyword"}]},
                            "X_QUERY_INVALID")
        self.assertIn("Ambiguous use of and", e.message)

    def test_error_message_hygiene(self):
        e = self.assertMaps(404, {"title": "Not Found Error", "detail": "gone", "echo": "tok-1"}, "X_NOT_FOUND",
                            {"x-transaction-id": "req-9"})
        self.assertIn("req-9", e.message)
        self.assertIn("gone", e.message)
        self.assertNotIn("tok-1", e.message)
        self.assertNotIn("Bearer", e.message)

    def test_non_json_error_body(self):
        self.transport.push(FakeResponse(404, {}, b"<html>oops tok-1</html>"))
        with self.assertRaises(XError) as ctx:
            self.search()
        self.assertEqual(ctx.exception.code, "X_NOT_FOUND")
        self.assertNotIn("<html>", ctx.exception.message)

    def test_header_names_case_insensitive(self):
        self.transport.push(response(429, {"title": "Too Many Requests"}, {"X-Rate-Limit-Reset": "1790943300"}))
        with self.assertRaises(XError) as ctx:
            self.search()
        self.assertEqual(ctx.exception.retry_at, "2026-10-02T12:15:00Z")

    def test_get_exempt(self):
        self.transport.push(response(200, {"data": {"balance": 12.5}}))
        payload = self.client.get_exempt("usage", "/2/usage/credits", None, bearer="app-1")
        self.assertEqual(payload, {"data": {"balance": 12.5}})
        self.assertEqual(self.transport.requests[0]["headers"]["Authorization"], "Bearer app-1")
        (r,) = self.rows()
        self.assertEqual(r["state"], "settled")
        self.assertEqual(r["est_usd"], 0.0)
        self.assertAlmostEqual(self.budget.committed_today(), 0.0)

    def test_get_exempt_error(self):
        self.transport.push(response(401, {"title": "Unauthorized"}))
        with self.assertRaises(XError) as ctx:
            self.client.get_exempt("usage", "/2/usage/credits", None, bearer="app-1")
        self.assertEqual(ctx.exception.code, "X_AUTH_EXPIRED")
        self.assertEqual(self.rows()[0]["error_code"], "X_AUTH_EXPIRED")


class _Recorder(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        target = getattr(self.server, "redirect_to", None)
        if target:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class UrllibTransportTest(unittest.TestCase):
    def setUp(self):
        # The module opener honours HTTP_PROXY; with one set the loopback request would go to the proxy.
        opener = urllib.request.build_opener(cl._NoRedirect, urllib.request.ProxyHandler({}))
        patcher = mock.patch.object(cl, "_OPENER", opener)
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, redirect_to=None):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
        server.seen = []
        server.redirect_to = redirect_to
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server

    def test_redirect_is_not_followed_and_bearer_stays_put(self):
        other = self.serve()
        first = self.serve(redirect_to="http://127.0.0.1:%d/landed" % other.server_port)
        resp = cl.UrllibTransport().send("GET", "http://127.0.0.1:%d/2/tweets/search/recent" % first.server_port,
                                         {"Authorization": "Bearer tok-secret"}, None, 5)
        self.assertEqual(resp.status, 302)
        self.assertEqual(other.seen, [])
        self.assertEqual(first.seen, [("/2/tweets/search/recent", "Bearer tok-secret")])


if __name__ == "__main__":
    unittest.main()
