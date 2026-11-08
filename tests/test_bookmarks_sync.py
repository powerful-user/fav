import fcntl
import os
import stat
import threading
import unittest
from unittest import mock

from tests.helpers import (CONFIG_TOML, NOW, TWEET_FIELDS, Clock, FakeTransport, TempHome, page, raw_post, response,
                           users_response, write_config, write_token)
from fav import bookmarks as bm
from fav import commands, normalize
from fav.errors import XError

SEEDED = "2026-10-01T00:00:00Z"


class SyncBase(TempHome):
    def setUp(self):
        super().setUp()
        write_config(self.env)
        write_token(self.env)
        self.clock = Clock()
        self.transport = FakeTransport()
        self.ctx = self.context()

    def context(self, extra=0.0):
        ctx = commands.build_context(self.env, transport=self.transport, now_fn=self.clock, sleep=lambda s: None,
                                     extra_usd=extra)
        self.addCleanup(ctx.store.close)
        return ctx

    def bm_page(self, ids, next_token=None, author="42"):
        return page([raw_post(i, author_id=author) for i in ids], next_token=next_token)

    def seed(self, ids):
        users = {"42": {"id": "42", "username": "example", "display_name": "Example"}}
        self.ctx.store.upsert_users(list(users.values()), SEEDED)
        posts = [normalize.normalize_post(raw_post(i), users, "bookmark") for i in ids]
        bm.apply_incremental(self.ctx.store, list(ids), list(ids), posts, SEEDED)

    def listed(self):
        return [p["id"] for p in bm.list_bookmarks(self.ctx.store, limit=1000)]


class IncrementalSyncTest(SyncBase):
    def test_first_sync_two_pages(self):
        ids = ["n" + str(i) for i in range(1, 151)]
        self.transport.push(self.bm_page(ids[:100], "p2"), self.bm_page(ids[100:]), users_response(("42", "example")))
        result = bm.sync(self.ctx)
        self.assertEqual(self.transport.paths(), ["/2/users/7/bookmarks", "/2/users/7/bookmarks", "/2/users"])
        first = self.transport.query(0)
        self.assertEqual(first["max_results"], "100")
        self.assertEqual(first["tweet.fields"], TWEET_FIELDS)
        self.assertNotIn("pagination_token", first)
        self.assertEqual(self.transport.query(1)["pagination_token"], "p2")
        self.assertEqual((result["new"], result["moved"], result["pages"], result["authors_pending"]), (150, 0, 2, 0))
        self.assertEqual(result["last_sync"], NOW)
        self.assertEqual(result["items"], [])
        self.assertTrue(result["summary"][0].startswith("synced: 150 new, 0 moved, 2 pages"))
        self.assertEqual(self.listed(), ids)
        self.assertEqual(bm.search_bookmarks(self.ctx.store, "n42")[0]["url"], "https://x.com/example/status/n42")
        self.assertEqual(self.ctx.store.get_post("n1")["origin"], "live")

    def test_stop_rule_with_rebookmark(self):
        old = ["k" + str(i) for i in range(20, 0, -1)]
        self.seed(old)
        new = ["n" + str(i) for i in range(1, 151)]
        rest = [k for k in old if k != "k5"]
        self.transport.push(self.bm_page(["k5"] + new[:99], "p2"), self.bm_page(new[99:] + rest, "p3"))
        result = bm.sync(self.ctx)
        self.assertEqual(len(self.transport.requests), 2)
        self.assertEqual((result["new"], result["moved"]), (150, 1))
        self.assertEqual(self.listed(), ["k5"] + new + rest)

    def test_failure_leaves_index_unchanged(self):
        self.seed(["a"])
        self.transport.push(self.bm_page(["n1"], "p2"), response(503), response(503))
        with self.assertRaises(XError) as ctx:
            bm.sync(self.ctx)
        self.assertEqual(ctx.exception.code, "X_UPSTREAM")
        self.assertEqual(self.listed(), ["a"])
        self.assertEqual(self.ctx.store.get_state("last_sync_at"), SEEDED)
        count = self.ctx.store.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        self.assertGreaterEqual(count, 3)

    def test_page_cap(self):
        pages = [self.bm_page(["p" + str(n) + "-" + str(i) for i in range(100)], "next" + str(n)) for n in range(8)]
        self.transport.push(*(pages + [users_response(("42", "example"))]))
        result = bm.sync(self.ctx)
        self.assertEqual(self.transport.paths().count("/2/users/7/bookmarks"), 8)
        self.assertEqual((result["pages"], result["new"]), (8, 800))
        self.assertIn("bookmark list incomplete (page cap 8)", result["summary"])

    def test_stop_run_on_eighth_page_is_not_capped(self):
        old = ["k" + str(i) for i in range(10, 0, -1)]
        self.seed(old)
        pages = [self.bm_page(["p" + str(n)], "next" + str(n)) for n in range(7)]
        self.transport.push(*(pages + [self.bm_page(old, "next7")]))
        result = bm.sync(self.ctx)
        self.assertEqual((result["pages"], result["new"], result["moved"]), (8, 7, 0))
        self.assertNotIn("bookmark list incomplete (page cap 8)", result["summary"])

    def test_eighth_page_without_token_is_not_capped(self):
        pages = [self.bm_page(["p" + str(n)], "next" + str(n)) for n in range(7)]
        self.transport.push(*(pages + [self.bm_page(["p7"]), users_response(("42", "example"))]))
        result = bm.sync(self.ctx)
        self.assertEqual((result["pages"], result["new"]), (8, 8))
        self.assertNotIn("bookmark list incomplete (page cap 8)", result["summary"])

    def test_unchanged_sync_moves_nothing(self):
        old = ["k" + str(i) for i in range(20, 0, -1)]
        self.seed(old)
        before = list(self.ctx.store.conn.execute("SELECT post_id, seq FROM bookmarks ORDER BY post_id"))
        self.transport.push(self.bm_page(old, "p2"))
        result = bm.sync(self.ctx)
        self.assertEqual(len(self.transport.requests), 1)
        self.assertEqual((result["new"], result["moved"]), (0, 0))
        after = list(self.ctx.store.conn.execute("SELECT post_id, seq FROM bookmarks ORDER BY post_id"))
        self.assertEqual([tuple(r) for r in after], [tuple(r) for r in before])

    def test_unavailable_bookmark_marked(self):
        self.seed(["a"])
        self.transport.push(page([raw_post("n1")], errors=[{"title": "Not Found Error", "resource_id": "a",
                                                             "detail": "Could not find tweet with id: [a]."}]))
        bm.sync(self.ctx)
        got = self.ctx.store.get_post("a")
        self.assertEqual((got["unavailable"], got["text"]), ("deleted", "post a"))

    def test_author_failure_after_apply_keeps_bookmarks(self):
        self.transport.push(page([raw_post("n1", author_id="99")]), response(503), response(503))
        result = bm.sync(self.ctx)
        self.assertEqual(self.listed(), ["n1"])
        self.assertEqual(result["authors_pending"], 1)
        self.assertIn("authors not resolved: X_UPSTREAM", " ".join(result["summary"]))

    def test_needs_login(self):
        import os
        os.remove(os.path.join(self.env["XDG_CONFIG_HOME"], "fav", "token.json"))
        with self.assertRaises(XError) as ctx:
            bm.sync(self.ctx)
        self.assertEqual(ctx.exception.code, "X_AUTH_REQUIRED")


class AuthorsTest(SyncBase):
    def test_short_budget_leaves_pending_then_resolves_first(self):
        write_config(self.env, CONFIG_TOML.replace("1.00", "0.15"))
        self.ctx = self.context()
        ids = ["n" + str(i) for i in range(20)]
        posts = [raw_post(i, author_id="a" + i) for i in ids]
        self.transport.push(page(posts))
        result = bm.sync(self.ctx)
        self.assertEqual((result["new"], result["authors_pending"]), (20, 20))
        self.assertEqual(self.transport.paths(), ["/2/users/7/bookmarks"])
        self.assertEqual(len(bm.pending_authors(self.ctx.store)), 20)
        second = self.context(extra=1.0)
        self.transport.push(users_response(*[("a" + i, "user" + i) for i in ids]), page(posts[:10]))
        result = bm.sync(second)
        self.assertEqual(self.transport.paths()[1:], ["/2/users", "/2/users/7/bookmarks"])
        self.assertEqual((result["new"], result["authors_pending"]), (0, 0))
        self.assertEqual([p["id"] for p in bm.search_bookmarks(second.store, "usern3")], ["n3"])


class FullSyncTest(SyncBase):
    def folder_response(self, ids, next_token=None):
        meta = {"result_count": len(ids)}
        if next_token:
            meta["next_token"] = next_token
        return response(200, {"data": [{"id": i} for i in ids], "meta": meta})

    def test_full_sync_with_folders(self):
        write_config(self.env, CONFIG_TOML + "folder_pages_max = 2\n")
        self.ctx = self.context()
        self.seed(["c", "b", "a"])
        self.transport.push(
            self.bm_page(["c", "a"]),
            response(200, {"data": [{"id": "f1", "name": "Ideas"}, {"id": "f2", "name": "Big"}],
                           "meta": {"result_count": 2}}),
            self.folder_response(["c", "x1"], "fp2"),
            self.folder_response(["x2"]),
            page([raw_post("x1"), raw_post("x2")]),
            self.folder_response(["a"], "g2"),
            self.folder_response(["c"], "g3"),
        )
        result = bm.sync(self.ctx, full=True)
        self.assertEqual(self.transport.paths(), [
            "/2/users/7/bookmarks", "/2/users/7/bookmarks/folders", "/2/users/7/bookmarks/folders/f1",
            "/2/users/7/bookmarks/folders/f1", "/2/tweets", "/2/users/7/bookmarks/folders/f2",
            "/2/users/7/bookmarks/folders/f2"])
        self.assertEqual(self.transport.query(4)["ids"], "x1,x2")
        self.assertEqual(self.transport.query(4)["tweet.fields"], TWEET_FIELDS)
        self.assertEqual(self.transport.query(3)["pagination_token"], "fp2")
        self.assertEqual((result["removed"], result["beyond_window"]), (1, 0))
        items = lambda folder: [r[0] for r in self.ctx.store.conn.execute(
            "SELECT post_id FROM bookmark_folder_items WHERE folder_id = ? ORDER BY post_id", (folder,))]
        synced = lambda folder: self.ctx.store.conn.execute(
            "SELECT members_synced_at FROM bookmark_folders WHERE id = ?", (folder,)).fetchone()[0]
        self.assertEqual(items("f1"), ["c", "x1", "x2"])
        self.assertEqual(synced("f1"), NOW)
        self.assertEqual(items("f2"), [])
        self.assertIsNone(synced("f2"))
        joined = " ".join(result["summary"])
        self.assertIn("f2", joined)
        self.assertIn("incomplete", joined)
        self.assertIsNotNone(self.ctx.store.get_post("x1"))
        self.assertNotIn("x1", self.listed())
        self.assertNotIn("b", self.listed())

    def test_full_failure_skips_reconciliation(self):
        self.seed(["a", "b"])
        self.transport.push(self.bm_page(["a"], "p2"), response(503), response(503))
        with self.assertRaises(XError):
            bm.sync(self.ctx, full=True)
        self.assertEqual(self.listed(), ["a", "b"])

    def test_full_capped_listing_is_not_proof_of_removal(self):
        self.seed(["old"])
        pages = [self.bm_page(["p" + str(n)], "next" + str(n)) for n in range(8)]
        self.transport.push(*(pages + [response(200, {"meta": {"result_count": 0}})]))
        result = bm.sync(self.ctx, full=True)
        self.assertEqual(self.transport.paths().count("/2/users/7/bookmarks"), 8)
        self.assertEqual((result["removed"], result["beyond_window"]), (0, 1))
        self.assertIn("bookmark list incomplete (page cap 8)", result["summary"])

    def test_full_eighth_page_without_token_is_complete(self):
        self.seed(["old"])
        pages = [self.bm_page(["p" + str(n)], "next" + str(n)) for n in range(7)]
        self.transport.push(*(pages + [self.bm_page(["p7"]), response(200, {"meta": {"result_count": 0}})]))
        result = bm.sync(self.ctx, full=True)
        self.assertEqual((result["pages"], result["removed"], result["beyond_window"]), (8, 1, 0))
        self.assertNotIn("bookmark list incomplete (page cap 8)", result["summary"])

    def test_folder_failure_after_apply_keeps_bookmarks(self):
        self.seed(["c", "b", "a"])
        self.transport.push(self.bm_page(["c", "a"]), response(503), response(503))
        result = bm.sync(self.ctx, full=True)
        self.assertEqual(self.listed(), ["c", "a"])
        self.assertEqual(result["removed"], 1)
        self.assertIn("folders not synced: X_UPSTREAM", " ".join(result["summary"]))

    def test_folder_list_cap_reported(self):
        write_config(self.env, CONFIG_TOML + "folder_pages_max = 1\n")
        self.ctx = self.context()
        self.transport.push(response(200, {"data": [{"id": "f1", "name": "Ideas"}],
                                           "meta": {"result_count": 1, "next_token": "n2"}}))
        result = bm.list_folders(self.ctx)
        self.assertEqual([f["id"] for f in result["folders"]], ["f1"])
        self.assertIn("folder list incomplete (page cap 1)", result["notes"])
        self.assertEqual(len(self.transport.requests), 1)

    def test_list_folders(self):
        self.transport.push(
            response(200, {"data": [{"id": "f1", "name": "Ideas"}], "meta": {"result_count": 1, "next_token": "n2"}}),
            response(200, {"data": [{"id": "f2", "name": "Later"}], "meta": {"result_count": 1}}),
        )
        result = bm.list_folders(self.ctx)
        self.assertEqual(result["folders"], [{"id": "f1", "name": "Ideas", "members_synced_at": None},
                                             {"id": "f2", "name": "Later", "members_synced_at": None}])
        self.assertEqual(self.transport.paths(), ["/2/users/7/bookmarks/folders", "/2/users/7/bookmarks/folders"])
        self.assertEqual(self.transport.query(1)["pagination_token"], "n2")


class SyncLockTest(SyncBase):
    def test_second_sync_waits_and_reads_state_after_the_first(self):
        old = ["k" + str(i) for i in range(10, 0, -1)]
        self.seed(old)
        lock_path = self.ctx.paths.sync_lock
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        real_flock = fcntl.flock
        real_flock(fd, fcntl.LOCK_EX)
        self.transport.push(self.bm_page(["n1"] + old))
        waiting = threading.Event()

        def flock(f, operation):
            # Signals the sync's attempt on the lock this test holds; the real call then blocks.
            target = f if isinstance(f, int) else f.fileno()
            if operation & fcntl.LOCK_EX and os.path.samestat(os.fstat(target), os.stat(lock_path)):
                waiting.set()
            return real_flock(f, operation)

        patcher = mock.patch("fcntl.flock", flock)
        patcher.start()
        self.addCleanup(patcher.stop)
        results, errors = [], []

        def worker():
            try:
                ctx = commands.build_context(self.env, transport=self.transport, now_fn=self.clock,
                                             sleep=lambda s: None)
                try:
                    results.append(bm.sync(ctx))
                finally:
                    ctx.store.close()
            except BaseException as e:
                errors.append(e)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        # Cleanups run last first: release the lock, then wait for the worker, before the store and home go.
        self.addCleanup(thread.join, 5)
        self.addCleanup(real_flock, fd, fcntl.LOCK_UN)
        self.assertTrue(waiting.wait(5), "sync never asked for the sync lock")
        self.assertEqual(self.transport.requests, [])
        # Another sync's result lands while this one waits: n1 is already active when it reads the index.
        self.seed(["n1"])
        self.clock.advance(minutes=5)
        real_flock(fd, fcntl.LOCK_UN)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual((results[0]["new"], results[0]["moved"]), (0, 0))
        # The waiting sync reads the clock after it takes the lock.
        self.assertEqual(results[0]["last_sync"], "2026-10-02T12:05:00Z")

    def test_lock_held_through_every_write(self):
        self.seed(["old"])
        self.transport.push(self.bm_page(["n1"]), response(200, {"meta": {"result_count": 0}}))
        held = []

        def probe():
            # A second open file description cannot take the lock while the sync holds it.
            fd = os.open(self.ctx.paths.sync_lock, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)

        def watch(name):
            real = getattr(bm, name)

            def watched(*args, **kwargs):
                probe()
                return real(*args, **kwargs)

            patcher = mock.patch.object(bm, name, watched)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Pending authors, the bookmark apply, folders, then the new posts' authors.
        for name in ("resolve_authors", "apply_full", "_sync_folders"):
            watch(name)
        bm.sync(self.ctx, full=True)
        self.assertEqual(held, [True, True, True, True])

    def test_lock_released_after_failure(self):
        self.transport.push(response(503), response(503))
        with self.assertRaises(XError):
            bm.sync(self.ctx)
        fd = os.open(self.ctx.paths.sync_lock, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(stat.S_IMODE(os.stat(self.ctx.paths.sync_lock).st_mode), 0o600)

    def test_sync_lock_failure_is_local_io(self):
        os.mkdir(self.ctx.paths.sync_lock)
        with self.assertRaises(XError) as ctx:
            bm.sync(self.ctx)
        self.assertEqual(ctx.exception.code, "X_LOCAL_IO")
        self.assertIn(self.ctx.paths.sync_lock, ctx.exception.message)
        self.assertEqual(self.transport.requests, [])


if __name__ == "__main__":
    unittest.main()
