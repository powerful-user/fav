import fcntl
import os
import unittest
from unittest import mock

from tests.helpers import Clock, FakeTransport, TempHome, page, raw_post, response, write_config, write_token
from tests.media_helpers import GIF, PHOTO, PHOTO_4A, lookup, pending, photo, stored
from fav import bookmarks as bm
from fav import commands, media

SEEDED = "2026-10-01T00:00:00Z"
BOOKMARKS = "/2/users/7/bookmarks"
EMPTY = response(200, {"data": [], "meta": {"result_count": 0}})


def with_media(post_id, *keys):
    return raw_post(post_id, attachments={"media_keys": list(keys)})


class MediaSyncBase(TempHome):
    def setUp(self):
        super().setUp()
        write_config(self.env)
        write_token(self.env)
        self.clock = Clock()
        self.transport = FakeTransport()
        self.ctx = commands.build_context(self.env, transport=self.transport, now_fn=self.clock,
                                          sleep=lambda s: None)
        self.addCleanup(self.ctx.store.close)
        # The author is cached and fresh, so no sync below looks it up.
        self.ctx.store.upsert_users([{"id": "42", "username": "example", "display_name": "Example"}],
                                    "2026-10-02T00:00:00Z")

    def seed(self, *posts):
        """Store posts as bookmarks, the first one newest."""
        self.ctx.store.upsert_posts(list(posts), SEEDED)
        ids = [p["id"] for p in posts]
        bm.apply_incremental(self.ctx.store, ids, ids, [], SEEDED)

    def media_of(self, post_id):
        return self.ctx.store.get_post(post_id).get("media")

    def ledger(self):
        return [dict(r) for r in self.ctx.store.conn.execute("SELECT * FROM ledger ORDER BY id")]


class SyncMediaTest(MediaSyncBase):
    def test_one_media_step_after_applying(self):
        self.transport.push(page([with_media("b1", "3_1"), raw_post("b2"), with_media("b3", "16_3")]),
                            lookup(("b1", ["3_1"]), ("b3", ["16_3"])))
        result = bm.sync(self.ctx)
        self.assertEqual(self.transport.paths(), [BOOKMARKS, "/2/tweets"])
        self.assertEqual(self.transport.query(1)["ids"], "b1,b3")
        row = self.ledger()[1]
        self.assertEqual(row["command"], "bookmarks sync")
        # Bookmarks were charged as owned reads, so each post is reserved again at post_read.
        self.assertAlmostEqual(row["reserved_usd"], 0.020)
        self.assertEqual((result["new"], result["media_pending"]), (3, 0))
        self.assertNotIn("media_error", result)
        self.assertEqual(self.media_of("b1"), [PHOTO])
        self.assertEqual(self.media_of("b3"), [GIF])
        self.assertEqual([p["id"] for p in bm.search_bookmarks(self.ctx.store, '"agent loop"')], ["b1"])

    def test_media_step_holds_the_sync_lock(self):
        self.transport.push(page([with_media("b1", "3_1")]), lookup(("b1", ["3_1"])))
        held = []
        real = media.lookup_media

        def probed(*args, **kwargs):
            # A second open file description cannot take the lock while the sync holds it.
            fd = os.open(self.ctx.paths.sync_lock, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
            return real(*args, **kwargs)

        with mock.patch.object(media, "lookup_media", probed):
            bm.sync(self.ctx)
        self.assertEqual(held, [True])
        self.assertEqual(self.media_of("b1"), [PHOTO])

    def test_this_syncs_posts_first_then_newest_bookmarks(self):
        self.seed(stored("o1", pending(["3_o1"])), stored("o2", pending(["3_o2"])), stored("o3", pending(["3_o3"])),
                  stored("o4", pending(["3_o4"])), stored("o5", [PHOTO_4A]))
        self.ctx.store.mark_unavailable("o2", "deleted")
        self.ctx.store.conn.execute("UPDATE bookmarks SET state = 'removed' WHERE post_id = 'o4'")
        self.ctx.store.conn.execute("UPDATE bookmarks SET state = 'beyond_window' WHERE post_id = 'o3'")
        self.transport.push(page([with_media("n1", "3_n1")]),
                            lookup(("n1", ["3_n1"]), ("o1", ["3_o1"]), ("o3", ["3_o3"])))
        result = bm.sync(self.ctx)
        self.assertEqual(self.transport.query(1)["ids"], "n1,o1,o3")
        self.assertEqual(self.media_of("o3"), [photo("3_o3")])
        # o2 is deleted on X and o4 was removed: neither is looked up, and neither counts as pending.
        self.assertEqual(result["media_pending"], 0)

    def test_lookup_failure_leaves_bookmarks_applied(self):
        self.seed(stored("o1", pending(["3_o1", "3_o1b"])))
        self.transport.push(page([with_media("b1", "3_1")]),
                            response(429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": "1790943300"}))
        result = bm.sync(self.ctx)
        self.assertEqual([p["id"] for p in bm.list_bookmarks(self.ctx.store)], ["b1", "o1"])
        self.assertEqual(result["media_error"], "X_RATE_LIMITED")
        self.assertIn("media lookup failed: X_RATE_LIMITED", result["summary"])
        # Pending attachments across every active bookmark: b1's one and o1's two.
        self.assertEqual(result["media_pending"], 3)
        self.assertEqual(self.ctx.store.get_state("last_sync_at"), "2026-10-02T12:00:00Z")

    def test_lookup_takes_only_what_paging_left(self):
        write_config(self.env, 'client_id = "test-client"\ndaily_ceiling_usd = 0.105\n')
        ctx = commands.build_context(self.env, transport=self.transport, now_fn=self.clock, sleep=lambda s: None)
        self.addCleanup(ctx.store.close)
        # The page reserves its 0.10 worst case first and settles 0.010, leaving 0.095: nine lookups at 0.010 fit.
        ids = ["b" + str(i) for i in range(10)]
        self.transport.push(page([with_media(i, "3_" + i) for i in ids]), lookup(*[(i, ["3_" + i]) for i in ids[:9]]))
        result = bm.sync(ctx)
        self.assertEqual(self.transport.paths(), [BOOKMARKS, "/2/tweets"])
        self.assertEqual(self.transport.query(1)["ids"].split(","), ids[:9])
        self.assertEqual((result["new"], result["media_pending"]), (10, 1))
        self.assertNotIn("media_error", result)

    def test_full_sync_retries_unavailable_items(self):
        self.seed(stored("u1", [PHOTO_4A, {"key": "3_4b", "unavailable": True}]))
        self.transport.push(page([with_media("u1", "3_4a", "3_4b")]), EMPTY, lookup(("u1", ["3_4a", "3_4b"])))
        result = bm.sync(self.ctx, full=True)
        self.assertEqual(self.transport.paths(), [BOOKMARKS, BOOKMARKS + "/folders", "/2/tweets"])
        self.assertEqual(self.media_of("u1"), [PHOTO_4A, photo("3_4b")])
        self.assertEqual(result["media_pending"], 0)

    def test_incremental_sync_keeps_unavailable_items(self):
        self.seed(stored("u1", [PHOTO_4A, {"key": "3_4b", "unavailable": True}]))
        self.transport.push(page([with_media("u1", "3_4a", "3_4b")]))
        result = bm.sync(self.ctx)
        self.assertEqual(self.transport.paths(), [BOOKMARKS])
        self.assertEqual(self.media_of("u1"), [PHOTO_4A, {"key": "3_4b", "unavailable": True}])
        self.assertEqual(result["media_pending"], 0)

    def test_hydrated_folder_posts_follow_page_posts(self):
        self.transport.push(page([with_media("b1", "3_1")]),
                            response(200, {"data": [{"id": "f1", "name": "Ideas"}]}),
                            response(200, {"data": [{"id": "b1"}, {"id": "h1"}]}),
                            page([with_media("h1", "16_3")]),
                            lookup(("b1", ["3_1"]), ("h1", ["16_3"])))
        bm.sync(self.ctx, full=True)
        self.assertEqual(self.transport.paths(), [BOOKMARKS, BOOKMARKS + "/folders", BOOKMARKS + "/folders/f1",
                                                  "/2/tweets", "/2/tweets"])
        self.assertEqual(self.transport.query(3)["ids"], "h1")
        self.assertEqual(self.transport.query(4)["ids"], "b1,h1")
        self.assertEqual(self.media_of("h1"), [GIF])

    def test_folder_failure_keeps_earlier_hydrated_posts(self):
        self.transport.push(page([with_media("b1", "3_1")]),
                            response(200, {"data": [{"id": "f1", "name": "Ideas"}, {"id": "f2", "name": "Later"}]}),
                            response(200, {"data": [{"id": "b1"}, {"id": "h1"}]}),
                            page([with_media("h1", "16_3")]),
                            response(429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": "1790943300"}),
                            lookup(("b1", ["3_1"]), ("h1", ["16_3"])))
        result = bm.sync(self.ctx, full=True)
        # f1 and its hydrated h1 were stored before f2 failed, so h1 is still among this sync's posts.
        self.assertEqual(self.transport.query(5)["ids"], "b1,h1")
        self.assertEqual(self.media_of("h1"), [GIF])
        self.assertTrue(any(line.startswith("folders not synced: X_RATE_LIMITED") for line in result["summary"]))


if __name__ == "__main__":
    unittest.main()
