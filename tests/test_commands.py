import os
import unittest

from tests.helpers import (TWEET_FIELDS, Clock, FakeTransport, TempHome, fixture, page, raw_post, response,
                           users_response, write_config, write_token)
from tests.media_helpers import lookup
from fav import commands, paths
from fav.errors import XError


class CommandsBase(TempHome):
    def setUp(self):
        super().setUp()
        write_config(self.env)
        write_token(self.env)
        self.clock = Clock()
        self.transport = FakeTransport()
        self.ctx = commands.build_context(self.env, transport=self.transport, now_fn=self.clock, sleep=lambda s: None)
        self.addCleanup(self.ctx.store.close)

    def ledger(self):
        return [dict(r) for r in self.ctx.store.conn.execute("SELECT * FROM ledger ORDER BY id")]


class ContextTest(CommandsBase):
    def test_context(self):
        self.assertEqual(commands.TWEET_FIELDS, TWEET_FIELDS)
        self.assertEqual(self.ctx.config.client_id, "test-client")
        self.assertEqual(self.ctx.auth.user_id(), "7")
        self.assertEqual(self.ctx.client.get.__name__, "get")


class DatabaseOpenTest(TempHome):
    def setUp(self):
        super().setUp()
        write_config(self.env)
        self.paths = paths.resolve_paths(self.env)
        paths.ensure_dirs(self.paths)

    def open_error(self):
        with self.assertRaises(XError) as ctx:
            context = commands.build_context(self.env, transport=FakeTransport(), now_fn=Clock(),
                                             sleep=lambda s: None)
            context.store.close()
        self.assertEqual(ctx.exception.code, "X_LOCAL_IO")
        self.assertIn(self.paths.db_file, ctx.exception.message)
        return ctx.exception

    def test_database_directory_is_local_io(self):
        os.mkdir(self.paths.db_file)
        error = self.open_error()
        self.assertTrue(error.message.endswith(": SQLITE_CANTOPEN"), error.message)

    def test_non_database_file_is_local_io(self):
        with open(self.paths.db_file, "wb") as fh:
            fh.write(b"sentinel-db " * 400)
        error = self.open_error()
        self.assertTrue(error.message.endswith(": SQLITE_NOTADB"), error.message)
        self.assertNotIn("sentinel-db", error.message)


class SearchTest(CommandsBase):
    def test_search(self):
        self.transport.push(response(200, fixture("search_recent.json")), users_response(("42", "example"), ("43", "other")),
                            lookup(("1002", ["3_1"])))
        result = commands.search(self.ctx, "agents", since="3d", from_user="@example", lang="en")
        self.assertEqual(self.transport.paths(), ["/2/tweets/search/recent", "/2/users", "/2/tweets"])
        query = self.transport.query(0)
        self.assertEqual(query["query"], "(agents) from:example lang:en")
        self.assertEqual(query["max_results"], "25")
        self.assertEqual(query["start_time"], "2026-09-29T12:00:00Z")
        self.assertEqual(query["tweet.fields"], TWEET_FIELDS)
        self.assertNotIn("expansions", query)
        self.assertEqual(self.transport.query(1)["ids"], "42,43")
        self.assertEqual([p["url"] for p in result["items"]],
                         ["https://x.com/example/status/1001", "https://x.com/other/status/1002"])
        self.assertEqual(result["next"], "tok2")
        self.assertEqual(result["authors_pending"], 0)
        self.assertAlmostEqual(result["estimated_cost_usd"], 0.035)
        self.assertEqual(self.ctx.store.get_post("1001")["text"], "Agent-first languages are coming")

    def test_or_query_grouped_before_filters(self):
        self.transport.push(response(200, {"meta": {"result_count": 0}}))
        commands.search(self.ctx, "cats OR dogs", from_user="example")
        self.assertEqual(self.transport.query(0)["query"], "(cats OR dogs) from:example")

    def test_minimum_limit_requests_ten(self):
        self.transport.push(response(200, fixture("search_recent.json")), users_response(("42", "example"), ("43", "other")),
                            lookup(("1002", ["3_1"])))
        result = commands.search(self.ctx, "agents", limit=10)
        self.assertEqual(self.transport.query(0)["query"], "agents")
        self.assertEqual(self.transport.query(0)["max_results"], "10")
        self.assertEqual(len(result["items"]), 2)
        self.assertAlmostEqual(self.ledger()[0]["reserved_usd"], 0.05)

    def test_oversized_page_is_upstream_error(self):
        # Trimming would hide posts behind a next token that skips them; nothing is cached or looked up.
        self.transport.push(page([raw_post(str(1000 + i)) for i in range(11)], next_token="tok2"))
        with self.assertRaises(XError) as ctx:
            commands.search(self.ctx, "agents", limit=10)
        self.assertEqual(ctx.exception.code, "X_UPSTREAM")
        self.assertEqual(self.transport.paths(), ["/2/tweets/search/recent"])
        self.assertIsNone(self.ctx.store.get_post("1000"))

    def test_malformed_page_is_upstream_error(self):
        for payload in ({"data": "invalid"}, {"meta": {"result_count": 3, "next_token": "tok2"}}):
            with self.subTest(payload=payload):
                self.transport.push(response(200, payload))
                with self.assertRaises(XError) as ctx:
                    commands.search(self.ctx, "agents")
                self.assertEqual(ctx.exception.code, "X_UPSTREAM")

    def test_page_count_that_disagrees_is_upstream_error(self):
        # A listed page whose result_count does not match it is not read as complete.
        bad_counts = ({"data": [], "meta": {"result_count": 3}},
                      {"data": [raw_post("1001")], "meta": {"result_count": 2}},
                      {"data": [raw_post("1001")], "meta": {"result_count": "1"}},
                      {"data": [raw_post("1001")], "meta": {"result_count": True}},
                      {"data": [raw_post("1001")], "meta": {"result_count": 1.0}},
                      {"meta": {"result_count": False}},
                      {"data": [], "meta": {"result_count": None}},
                      {"meta": {"result_count": None}})
        for payload in bad_counts:
            with self.subTest(payload=payload):
                self.transport.push(response(200, payload))
                with self.assertRaises(XError) as ctx:
                    commands.search(self.ctx, "agents")
                self.assertEqual(ctx.exception.code, "X_UPSTREAM")
                self.assertEqual(ctx.exception.message, "X returned a response wings-x cannot read")
                self.assertIsNone(self.ctx.store.get_post("1001"))

    def test_page_without_count_is_unchanged(self):
        for payload in ({"data": [raw_post("1001")]}, {"data": [raw_post("1001")], "meta": {}}):
            with self.subTest(payload=payload):
                self.transport.push(response(200, payload), users_response(("42", "example")))
                result = commands.search(self.ctx, "agents")
                self.assertEqual([p["id"] for p in result["items"]], ["1001"])

    def test_limit_bounds(self):
        for bad in (0, 1, 9, 101):
            with self.assertRaises(XError) as ctx:
                commands.search(self.ctx, "agents", limit=bad)
            self.assertEqual(ctx.exception.code, "X_BAD_INPUT")
        self.assertEqual(self.transport.requests, [])

    def test_empty_query(self):
        with self.assertRaises(XError) as ctx:
            commands.search(self.ctx, "  ")
        self.assertEqual(ctx.exception.code, "X_BAD_INPUT")

    def test_next_and_until(self):
        self.transport.push(response(200, {"meta": {"result_count": 0}}))
        result = commands.search(self.ctx, "agents", until="2026-10-01", next_token="tok2")
        query = self.transport.query(0)
        self.assertEqual(query["next_token"], "tok2")
        self.assertEqual(query["end_time"], "2026-10-01T00:00:00Z")
        self.assertEqual(result["items"], [])
        self.assertIsNone(result["next"])


class PostTest(CommandsBase):
    def test_post(self):
        self.transport.push(response(200, fixture("post_with_refs.json")), users_response(("42", "example"), ("44", "other")))
        result = commands.post(self.ctx, "https://x.com/example/status/1001")
        self.assertEqual(self.transport.paths(), ["/2/tweets/1001", "/2/users"])
        query = self.transport.query(0)
        self.assertEqual(query["expansions"], "referenced_tweets.id")
        self.assertEqual(query["tweet.fields"], TWEET_FIELDS)
        (item,) = result["items"]
        self.assertEqual(item["url"], "https://x.com/example/status/1001")
        self.assertEqual(item["referenced"][0]["url"], "https://x.com/other/status/99")
        self.assertEqual(item["referenced"][1]["unavailable"], "deleted")
        self.assertEqual([p["url"] for p in result["referenced_posts"]], ["https://x.com/other/status/99"])
        self.assertAlmostEqual(self.ledger()[0]["reserved_usd"], 0.02)

    def test_unavailable_reference_marks_stored_post(self):
        self.ctx.store.upsert_posts([{"id": "98", "url": "https://x.com/i/status/98", "text": "kept",
                                      "origin": "live", "source": "x", "unavailable": None}], "2026-10-01T00:00:00Z")
        self.transport.push(response(200, fixture("post_with_refs.json")), users_response(("42", "example"), ("44", "other")))
        commands.post(self.ctx, "1001")
        self.assertEqual(self.ctx.store.get_post("98")["unavailable"], "deleted")
        self.assertEqual(self.ctx.store.get_post("98")["text"], "kept")

    def test_not_found_marks_stored_post(self):
        self.ctx.store.upsert_posts([{"id": "1001", "url": "https://x.com/i/status/1001", "text": "kept",
                                      "origin": "live", "source": "x", "unavailable": None}], "2026-10-01T00:00:00Z")
        self.transport.push(response(404, {"title": "Not Found Error", "detail": "Could not find tweet with id: [1001]."}))
        with self.assertRaises(XError) as ctx:
            commands.post(self.ctx, "1001")
        self.assertEqual(ctx.exception.code, "X_NOT_FOUND")
        self.assertEqual(self.ctx.store.get_post("1001")["unavailable"], "deleted")
        self.assertEqual(self.ctx.store.get_post("1001")["text"], "kept")

    def test_errors_only_payload(self):
        self.transport.push(response(200, {"errors": [{"title": "Authorization Error", "resource_id": "1001",
                                                       "detail": "Sorry, you are not authorized to see the Post with id: [1001]."}]}))
        with self.assertRaises(XError) as ctx:
            commands.post(self.ctx, "1001")
        self.assertEqual(ctx.exception.code, "X_PROTECTED")

    def test_bad_ref(self):
        with self.assertRaises(XError) as ctx:
            commands.post(self.ctx, "not-a-post")
        self.assertEqual(ctx.exception.code, "X_BAD_INPUT")
        self.assertEqual(self.transport.requests, [])


class UserTest(CommandsBase):
    PAYLOAD = {"data": {"id": "42", "username": "example", "name": "Example", "description": "hi",
                        "created_at": "2010-01-01T00:00:00.000Z",
                        "public_metrics": {"followers_count": 10, "following_count": 2, "tweet_count": 300}}}

    def test_by_username(self):
        self.transport.push(response(200, self.PAYLOAD))
        result = commands.user(self.ctx, "@example")
        self.assertEqual(self.transport.paths(), ["/2/users/by/username/example"])
        self.assertEqual(self.transport.query(0)["user.fields"], "created_at,description,name,public_metrics,username")
        self.assertEqual(result["users"], [{"id": "42", "username": "example", "display_name": "Example",
                                            "description": "hi", "created_at": "2010-01-01T00:00:00Z",
                                            "metrics": {"followers": 10, "following": 2, "posts": 300},
                                            "url": "https://x.com/example"}])
        self.assertIn("42", self.ctx.store.get_users(["42"]))
        self.assertAlmostEqual(self.ledger()[0]["reserved_usd"], 0.010)

    def test_by_id(self):
        self.transport.push(response(200, self.PAYLOAD))
        commands.user(self.ctx, "42")
        self.assertEqual(self.transport.paths(), ["/2/users/42"])

    def test_username_match_ignores_case(self):
        self.transport.push(response(200, self.PAYLOAD))
        result = commands.user(self.ctx, "@Example")
        self.assertEqual(result["users"][0]["id"], "42")

    def test_user_not_requested_is_upstream_error(self):
        # A different user than the one asked for is neither shown nor cached.
        for ref in ("43", "@other"):
            with self.subTest(ref=ref):
                self.transport.push(response(200, self.PAYLOAD))
                with self.assertRaises(XError) as ctx:
                    commands.user(self.ctx, ref)
                self.assertEqual(ctx.exception.code, "X_UPSTREAM")
                self.assertEqual(self.ctx.store.get_users(["42"]), {})


class ThreadTest(CommandsBase):
    def test_thread(self):
        self.transport.push(
            response(200, {"data": raw_post("300", conversation_id="100",
                                            referenced_tweets=[{"type": "replied_to", "id": "200"}])}),
            response(200, {"data": raw_post("100", created_at="2026-10-01T08:00:00.000Z")}),
            response(200, {"data": raw_post("200", conversation_id="100",
                                            referenced_tweets=[{"type": "replied_to", "id": "100"}])}),
            page([raw_post("301", conversation_id="100"), raw_post("300", conversation_id="100")]),
            users_response(("42", "example")),
        )
        result = commands.thread(self.ctx, "300")
        self.assertEqual(self.transport.paths(), ["/2/tweets/300", "/2/tweets/100", "/2/tweets/200",
                                                  "/2/tweets/search/recent", "/2/users"])
        query = self.transport.query(3)
        self.assertEqual(query["query"], "conversation_id:100")
        self.assertEqual(query["max_results"], "47")
        self.assertEqual([p["id"] for p in result["items"]], ["100", "200", "300", "301"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["notes"], [])

    def test_post_is_root(self):
        self.transport.push(response(200, {"data": raw_post("100")}), page([raw_post("101", conversation_id="100")]),
                            users_response(("42", "example")))
        result = commands.thread(self.ctx, "100")
        self.assertEqual(self.transport.paths(), ["/2/tweets/100", "/2/tweets/search/recent", "/2/users"])
        self.assertEqual(self.transport.query(1)["max_results"], "49")
        self.assertEqual([p["id"] for p in result["items"]], ["100", "101"])

    def test_reply_page_count_that_disagrees(self):
        self.transport.push(response(200, {"data": raw_post("100")}),
                            response(200, {"data": [], "meta": {"result_count": 2}}))
        with self.assertRaises(XError) as ctx:
            commands.thread(self.ctx, "100")
        self.assertEqual(ctx.exception.code, "X_UPSTREAM")
        self.assertEqual(self.transport.paths(), ["/2/tweets/100", "/2/tweets/search/recent"])

    def test_limit_caps_total(self):
        replies = [raw_post(str(n), conversation_id="100") for n in range(401, 411)]
        self.transport.push(
            response(200, {"data": raw_post("300", conversation_id="100",
                                            referenced_tweets=[{"type": "replied_to", "id": "200"}])}),
            response(200, {"data": raw_post("100")}),
            response(200, {"data": raw_post("200", conversation_id="100",
                                            referenced_tweets=[{"type": "replied_to", "id": "100"}])}),
            page(replies, next_token="more"),
            users_response(("42", "example")),
        )
        result = commands.thread(self.ctx, "300", limit=5)
        self.assertEqual(self.transport.query(3)["max_results"], "10")
        self.assertEqual([p["id"] for p in result["items"]], ["100", "200", "300", "401", "402"])
        self.assertTrue(result["truncated"])

    def test_limit_one_returns_only_the_post(self):
        self.transport.push(
            response(200, {"data": raw_post("300", conversation_id="100",
                                            referenced_tweets=[{"type": "replied_to", "id": "200"}])}),
            users_response(("42", "example")),
        )
        result = commands.thread(self.ctx, "300", limit=1)
        self.assertEqual(self.transport.paths(), ["/2/tweets/300", "/2/users"])
        self.assertEqual([p["id"] for p in result["items"]], ["300"])
        self.assertTrue(result["truncated"])

    def test_limit_two_adds_the_root_only(self):
        self.transport.push(
            response(200, {"data": raw_post("300", conversation_id="100",
                                            referenced_tweets=[{"type": "replied_to", "id": "200"}])}),
            response(200, {"data": raw_post("100")}),
            users_response(("42", "example")),
        )
        result = commands.thread(self.ctx, "300", limit=2)
        self.assertEqual(self.transport.paths(), ["/2/tweets/300", "/2/tweets/100", "/2/users"])
        self.assertEqual([p["id"] for p in result["items"]], ["100", "300"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["gaps"], [])

    def test_unavailable_ancestor(self):
        self.transport.push(
            response(200, {"data": raw_post("300", conversation_id="100",
                                            referenced_tweets=[{"type": "replied_to", "id": "200"}])}),
            response(200, {"data": raw_post("100")}),
            response(200, {"errors": [{"title": "Not Found Error", "resource_id": "200",
                                       "detail": "Could not find tweet with id: [200]."}]}),
            page([]),
            users_response(("42", "example")),
        )
        result = commands.thread(self.ctx, "300")
        self.assertEqual([p["id"] for p in result["items"]], ["100", "300"])
        self.assertEqual(result["gaps"], [{"after": "300", "missing": "200", "reason": "deleted"}])

    def test_ten_hop_cap(self):
        items = [response(200, {"data": raw_post("20", conversation_id="1",
                                                 referenced_tweets=[{"type": "replied_to", "id": "19"}])}),
                 response(200, {"data": raw_post("1")})]
        for n in range(19, 9, -1):
            items.append(response(200, {"data": raw_post(str(n), conversation_id="1",
                                                         referenced_tweets=[{"type": "replied_to", "id": str(n - 1)}])}))
        items += [page([]), users_response(("42", "example"))]
        self.transport.push(*items)
        result = commands.thread(self.ctx, "20")
        self.assertEqual(self.transport.paths()[2:12], ["/2/tweets/" + str(n) for n in range(19, 9, -1)])
        self.assertEqual(self.transport.paths()[12], "/2/tweets/search/recent")
        self.assertEqual(result["items"][0]["id"], "1")
        self.assertEqual([p["id"] for p in result["items"][1:]], [str(n) for n in range(10, 21)])
        self.assertEqual(result["gaps"], [{"after": "10", "missing": "9", "reason": "hop limit"}])

    def test_old_root_note(self):
        self.transport.push(response(200, {"data": raw_post("100", created_at="2026-09-20T00:00:00.000Z")}),
                            page([]), users_response(("42", "example")))
        result = commands.thread(self.ctx, "100")
        self.assertEqual(result["notes"], ["replies older than 7 days are not searchable"])


if __name__ == "__main__":
    unittest.main()
