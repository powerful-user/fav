import unittest
from unittest import mock

from tests.helpers import (CONFIG_TOML, NOW, Clock, FakeTransport, TempHome, fixture, page, raw_post, response,
                           users_response, write_config, write_token)
from tests.media_helpers import (GIF, MEDIA_FIELDS, PHOTO, PHOTO_4A, VIDEO, charge, lookup, not_found, pending,
                                 photo, raw_photo, stored)
from fav import budget as bd
from fav import commands, media


class LookupBase(TempHome):
    def setUp(self):
        super().setUp()
        write_token(self.env)
        self.clock = Clock()
        self.transport = FakeTransport()

    def context(self, text=CONFIG_TOML, extra=0.0):
        write_config(self.env, text)
        self.ctx = commands.build_context(self.env, transport=self.transport, now_fn=self.clock,
                                          sleep=lambda s: None, extra_usd=extra)
        self.addCleanup(self.ctx.store.close)
        return self.ctx

    def seed(self, *posts, read_today=True):
        self.ctx.store.upsert_posts(list(posts), NOW)
        if read_today:
            charge(self.ctx.store, "post_read", [p["id"] for p in posts])

    def media_of(self, post_id):
        return self.ctx.store.get_post(post_id).get("media")

    def ledger(self):
        return [dict(r) for r in self.ctx.store.conn.execute("SELECT * FROM ledger ORDER BY id")]


class LookupStepTest(LookupBase):
    def test_request_reservation_and_merge(self):
        self.context()
        self.seed(stored("2001", pending(["3_1"])), stored("2002", pending(["13_2"])), stored("2003", pending(["16_3"])))
        self.transport.push(lookup(("2001", ["3_1"]), ("2002", ["13_2"]), ("2003", ["16_3"])))
        step = media.lookup_media(self.ctx, "search", ["2001", "2002", "2003"])
        self.assertEqual(step, {"error": None, "notes": []})
        self.assertEqual(self.transport.paths(), ["/2/tweets"])
        self.assertEqual(self.transport.query(0), {"ids": "2001,2002,2003", "tweet.fields": "attachments",
                                                   "expansions": "attachments.media_keys",
                                                   "media.fields": MEDIA_FIELDS})
        (row,) = self.ledger()
        self.assertEqual((row["command"], row["endpoint"], row["state"]), ("search", "/2/tweets", "settled"))
        # Each post was read today already, so only its attachment is reserved.
        self.assertAlmostEqual(row["reserved_usd"], 0.015)
        self.assertAlmostEqual(row["est_usd"], 0.015)
        self.assertEqual([self.media_of(i) for i in ("2001", "2002", "2003")], [[PHOTO], [VIDEO], [GIF]])

    def test_lookup_data_never_reaches_upsert(self):
        self.context()
        self.seed(stored("2001", pending(["3_1"])))
        self.transport.push(lookup(("2001", ["3_1"])))
        with mock.patch.object(self.ctx.store, "upsert_posts", side_effect=AssertionError("lookup data upserted")):
            step = media.lookup_media(self.ctx, "search", ["2001"])
        self.assertIsNone(step["error"])
        post = self.ctx.store.get_post("2001")
        self.assertEqual(post["text"], "stored text 2001")
        self.assertEqual(post["author"], {"id": "42"})
        self.assertEqual(post["created_at"], "2026-10-01T10:00:00Z")
        self.assertEqual(post["metrics"]["likes"], 1)

    def test_reservation_lists_every_stored_key(self):
        self.context()
        self.seed(stored("2004", [PHOTO_4A] + pending(["3_4b"])), read_today=False)
        self.transport.push(lookup(("2004", ["3_4a", "3_4b"])))
        media.lookup_media(self.ctx, "bookmarks sync", ["2004"])
        (row,) = self.ledger()
        # The post at post_read and both attachments, the resolved one included.
        self.assertAlmostEqual(row["reserved_usd"], 0.015)
        self.assertLessEqual(row["est_usd"], row["reserved_usd"] + 1e-9)
        self.assertEqual(self.media_of("2004"), [PHOTO_4A, photo("3_4b")])

    def test_nothing_to_look_up(self):
        self.context()
        self.seed(stored("2001", [PHOTO]), stored("2004", [PHOTO_4A, {"key": "3_4b", "unavailable": True}]),
                  stored("2005", pending(["3_5"])), stored("2006", None))
        self.ctx.store.mark_unavailable("2005", "deleted")
        step = media.lookup_media(self.ctx, "search", ["2001", "2004", "2005", "2006", "2007"])
        self.assertEqual(step, {"error": None, "notes": []})
        self.assertEqual(self.transport.requests, [])

    def test_missing_key_becomes_unavailable_and_is_skipped(self):
        self.context()
        self.seed(stored("2004", pending(["3_4a", "3_4b"])))
        self.transport.push(lookup(("2004", ["3_4a", "3_4b"]), omit=["3_4b"]))
        media.lookup_media(self.ctx, "search", ["2004"])
        self.assertEqual(self.media_of("2004"), [PHOTO_4A, {"key": "3_4b", "unavailable": True}])
        media.lookup_media(self.ctx, "search", ["2004"])
        self.assertEqual(len(self.transport.requests), 1)

    def test_post_neither_returned_nor_reported_stays_pending(self):
        self.context()
        self.seed(stored("2001", pending(["3_1"])), stored("2002", pending(["13_2"])))
        self.transport.push(lookup(("2001", ["3_1"])))
        step = media.lookup_media(self.ctx, "search", ["2001", "2002"])
        self.assertIsNone(step["error"])
        self.assertEqual(self.media_of("2001"), [PHOTO])
        self.assertEqual(self.media_of("2002"), pending(["13_2"]))

    def test_reported_post_becomes_unavailable(self):
        self.context()
        self.seed(stored("2001", pending(["3_1"])), stored("2002", pending(["13_2"])))
        self.transport.push(lookup(("2001", ["3_1"]), errors=[not_found("2002")]))
        media.lookup_media(self.ctx, "search", ["2001", "2002"])
        self.assertEqual(self.ctx.store.get_post("2002")["unavailable"], "deleted")
        self.assertEqual(self.media_of("2002"), pending(["13_2"]))

    def test_changed_keys_are_noted(self):
        self.context()
        self.seed(stored("2001", pending(["3_1"])))
        self.transport.push(lookup(("2001", ["3_1", "3_7"])))
        step = media.lookup_media(self.ctx, "search", ["2001"])
        self.assertEqual(step, {"error": None, "notes": ["media keys changed for 2001"]})
        self.assertEqual(self.media_of("2001"), [PHOTO, photo("3_7")])
        (row,) = self.ledger()
        # Settlement records what X returned.
        self.assertAlmostEqual(row["est_usd"], 0.010)

    def test_malformed_media_is_a_failed_lookup(self):
        self.context()
        self.seed(stored("2001", pending(["3_1"])))
        self.transport.push(response(200, {"data": [{"id": "2001", "attachments": {"media_keys": ["3_1"]}}],
                                           "includes": {"media": [{"type": "photo"}]}}))
        step = media.lookup_media(self.ctx, "search", ["2001"])
        self.assertEqual(step, {"error": "X_UPSTREAM", "notes": []})
        self.assertEqual(self.media_of("2001"), pending(["3_1"]))

    def test_malformed_errors_is_a_failed_lookup(self):
        self.context()
        self.seed(stored("2001", pending(["3_1"])))
        for errors in ({}, 0, "", [1]):
            with self.subTest(errors=errors):
                self.transport.push(response(200, {"data": [{"id": "2001", "attachments": {"media_keys": ["3_1"]}}],
                                                   "includes": {"media": [raw_photo("3_1")]}, "errors": errors}))
                step = media.lookup_media(self.ctx, "search", ["2001"])
                self.assertEqual(step, {"error": "X_UPSTREAM", "notes": []})
                self.assertEqual(self.media_of("2001"), pending(["3_1"]))

    def test_x_error_stops_the_step(self):
        self.context()
        self.seed(stored("2001", pending(["3_1"])))
        self.transport.push(response(429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": "1790943300"}))
        step = media.lookup_media(self.ctx, "search", ["2001"])
        self.assertEqual(step, {"error": "X_RATE_LIMITED", "notes": []})
        self.assertEqual(self.media_of("2001"), pending(["3_1"]))

    def test_batches_of_100(self):
        self.context()
        ids = [str(3000 + i) for i in range(150)]
        self.seed(*[stored(i, pending(["3_" + i])) for i in ids])
        self.transport.push(lookup(*[(i, ["3_" + i]) for i in ids[:100]]),
                            lookup(*[(i, ["3_" + i]) for i in ids[100:]]))
        step = media.lookup_media(self.ctx, "search", ids)
        self.assertIsNone(step["error"])
        self.assertEqual([len(self.transport.query(n)["ids"].split(",")) for n in (0, 1)], [100, 50])
        self.assertEqual(self.transport.query(0)["ids"].split(","), ids[:100])
        self.assertEqual(self.media_of("3149"), [photo("3_3149")])

    def test_batch_fills_to_allowance(self):
        self.context(CONFIG_TOML.replace("1.00", "0.025"))
        # Not read today: each post costs 0.005 to look up plus 0.005 for its photo.
        self.seed(*[stored(i, pending(["3_" + i])) for i in ("4001", "4002", "4003", "4004")], read_today=False)
        self.transport.push(lookup(("4001", ["3_4001"]), ("4002", ["3_4002"])))
        step = media.lookup_media(self.ctx, "bookmarks sync", ["4001", "4002", "4003", "4004"])
        self.assertEqual(step, {"error": None, "notes": []})
        self.assertEqual(self.transport.query(0)["ids"], "4001,4002")
        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.media_of("4003"), pending(["3_4003"]))
        self.assertEqual(self.media_of("4004"), pending(["3_4004"]))

    def test_batch_counts_posts_read_today_at_zero(self):
        self.context(CONFIG_TOML.replace("1.00", "0.010"))
        # Read today: each post reserves only its photo, so both fit in 0.010. A worst-case fill would take one.
        self.seed(stored("4001", pending(["3_4001"])), stored("4002", pending(["3_4002"])))
        self.transport.push(lookup(("4001", ["3_4001"]), ("4002", ["3_4002"])))
        step = media.lookup_media(self.ctx, "search", ["4001", "4002"])
        self.assertEqual(step, {"error": None, "notes": []})
        self.assertEqual(self.transport.query(0)["ids"], "4001,4002")
        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.media_of("4002"), [photo("3_4002")])

    def test_post_over_allowance_ends_the_step(self):
        self.context(CONFIG_TOML.replace("1.00", "0.005"))
        self.seed(stored("4001", pending(["3_4001"])), read_today=False)
        step = media.lookup_media(self.ctx, "bookmarks sync", ["4001"])
        self.assertEqual(step, {"error": None, "notes": []})
        self.assertEqual(self.transport.requests, [])

    def test_raced_reservation_is_a_budget_stop(self):
        self.context()
        self.seed(stored("2001", pending(["3_1"])), read_today=False)
        other = bd.Budget(self.ctx.store, self.ctx.config, invocation_id="other", now_fn=self.clock)
        other.reserve("search", "/x", 0.995)
        # The step read a larger allowance before the other process reserved.
        with mock.patch.object(self.ctx.budget, "allowance", return_value=1.0):
            step = media.lookup_media(self.ctx, "search", ["2001"])
        self.assertEqual(step, {"error": None, "notes": []})
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(self.media_of("2001"), pending(["3_1"]))

    def test_retry_over_budget_is_a_budget_stop(self):
        self.context(CONFIG_TOML.replace("1.00", "0.00"), extra=0.005)
        self.seed(stored("2001", pending(["3_1"])))
        self.transport.push(response(503, {"title": "Service Unavailable"}))
        step = media.lookup_media(self.ctx, "search", ["2001"])
        self.assertEqual(step, {"error": None, "notes": []})
        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual(self.media_of("2001"), pending(["3_1"]))


class CommandMediaTest(LookupBase):
    def setUp(self):
        super().setUp()
        self.context()

    def test_search(self):
        self.transport.push(response(200, fixture("search_recent.json")), users_response(("42", "example"), ("43", "other")),
                            lookup(("1002", ["3_1"])))
        result = commands.search(self.ctx, "agents")
        self.assertEqual(self.transport.paths(), ["/2/tweets/search/recent", "/2/users", "/2/tweets"])
        self.assertEqual(self.transport.query(2)["ids"], "1002")
        # 1002 was read by the search itself, so only its photo is reserved.
        self.assertAlmostEqual(self.ledger()[2]["reserved_usd"], 0.005)
        self.assertNotIn("media", result["items"][0])
        self.assertEqual(result["items"][1]["media"], [PHOTO])
        self.assertEqual(result["media_pending"], 0)
        self.assertNotIn("media_error", result)
        self.assertAlmostEqual(result["estimated_cost_usd"], 0.035)

    def test_search_lookup_failure_still_succeeds(self):
        self.transport.push(response(200, fixture("search_recent.json")), users_response(("42", "example"), ("43", "other")),
                            response(500, {"title": "Internal Error"}), response(500, {"title": "Internal Error"}))
        result = commands.search(self.ctx, "agents")
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][1]["media"], pending(["3_1"]))
        self.assertEqual(result["media_pending"], 1)
        self.assertEqual(result["media_error"], "X_UPSTREAM")

    def test_media_pending_counts_attachments(self):
        posts = [raw_post("7001", attachments={"media_keys": ["3_a", "3_b"]}),
                 raw_post("7002", attachments={"media_keys": ["3_c", "3_d"]})]
        self.transport.push(page(posts), users_response(("42", "example")),
                            response(429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": "1790943300"}))
        result = commands.search(self.ctx, "agents")
        self.assertEqual(result["media_pending"], 4)
        self.assertEqual(result["media_error"], "X_RATE_LIMITED")

    def test_post_with_referenced_posts(self):
        payload = {"data": raw_post("5001", attachments={"media_keys": ["3_1"]},
                                    referenced_tweets=[{"type": "quoted", "id": "5002"}]),
                   "includes": {"tweets": [raw_post("5002", author_id="44", attachments={"media_keys": ["16_3"]})]}}
        self.transport.push(response(200, payload), users_response(("42", "example"), ("44", "other")),
                            lookup(("5001", ["3_1"]), ("5002", ["16_3"])))
        result = commands.post(self.ctx, "5001")
        self.assertEqual(self.transport.paths(), ["/2/tweets/5001", "/2/users", "/2/tweets"])
        self.assertEqual(self.transport.query(2)["ids"], "5001,5002")
        self.assertEqual(result["items"][0]["media"], [PHOTO])
        self.assertEqual(result["referenced_posts"][0]["media"], [GIF])
        self.assertEqual(result["media_pending"], 0)

    def test_thread(self):
        main = raw_post("6001", attachments={"media_keys": ["3_1"]})
        reply = raw_post("6002", conversation_id="6001", created_at="2026-10-01T11:00:00.000Z",
                         attachments={"media_keys": ["16_3"]}, referenced_tweets=[{"type": "replied_to", "id": "6001"}])
        self.transport.push(response(200, {"data": main}), page([reply]), users_response(("42", "example")),
                            lookup(("6001", ["3_1"]), ("6002", ["16_3"])))
        result = commands.thread(self.ctx, "6001")
        self.assertEqual(self.transport.paths(),
                         ["/2/tweets/6001", "/2/tweets/search/recent", "/2/users", "/2/tweets"])
        self.assertEqual(self.transport.query(3)["ids"], "6001,6002")
        self.assertEqual([item["media"] for item in result["items"]], [[PHOTO], [GIF]])
        self.assertEqual(result["media_pending"], 0)

    def test_thread_looks_up_in_fetch_order(self):
        root = raw_post("6001", attachments={"media_keys": ["3_1"]})
        main = raw_post("6002", conversation_id="6001", created_at="2026-10-01T11:00:00.000Z",
                        attachments={"media_keys": ["16_3"]}, referenced_tweets=[{"type": "replied_to", "id": "6001"}])
        self.transport.push(response(200, {"data": main}), response(200, {"data": root}), page([]),
                            users_response(("42", "example")), lookup(("6002", ["16_3"])))
        # The allowance covers one attachment: the requested post, fetched first, takes it, though the root shows first.
        with mock.patch.object(self.ctx.budget, "allowance", side_effect=[0.005, 0.0]):
            result = commands.thread(self.ctx, "6002")
        self.assertEqual(self.transport.query(4)["ids"], "6002")
        self.assertEqual([item["id"] for item in result["items"]], ["6001", "6002"])
        self.assertEqual([item["media"] for item in result["items"]], [pending(["3_1"]), [GIF]])
        self.assertEqual(result["media_pending"], 1)

    def test_changed_keys_become_a_note(self):
        self.transport.push(page([raw_post("7001", attachments={"media_keys": ["3_1"]})]),
                            users_response(("42", "example")), lookup(("7001", ["3_1", "3_7"])))
        result = commands.search(self.ctx, "agents")
        self.assertEqual(result["notes"], ["media keys changed for 7001"])
        self.assertEqual(result["items"][0]["media"], [PHOTO, photo("3_7")])

    def test_no_media_no_lookup(self):
        self.transport.push(page([raw_post("7001")]), users_response(("42", "example")))
        result = commands.search(self.ctx, "agents")
        self.assertEqual(self.transport.paths(), ["/2/tweets/search/recent", "/2/users"])
        self.assertEqual(result["media_pending"], 0)
        self.assertNotIn("notes", result)


if __name__ == "__main__":
    unittest.main()
