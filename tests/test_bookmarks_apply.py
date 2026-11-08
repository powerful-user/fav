import unittest
from datetime import datetime, timezone
from unittest import mock

from tests.helpers import NOW, TempHome
from fav import bookmarks as bm
from fav import normalize, paths
from fav import store as st
from fav.errors import XError

SEEDED = "2026-10-01T00:00:00Z"


def make_posts(*ids):
    return [normalize.normalize_post({"id": i, "text": "post " + i, "author_id": "42",
                                      "created_at": "2026-10-01T10:00:00.000Z"}, {}, "bookmark") for i in ids]


class ApplyBase(TempHome):
    def setUp(self):
        super().setUp()
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        self.store = st.Store(p.db_file)
        self.addCleanup(self.store.close)

    def rows(self):
        return dict((r["post_id"], dict(r)) for r in self.store.conn.execute("SELECT * FROM bookmarks"))

    def order(self):
        return [r[0] for r in self.store.conn.execute(
            "SELECT post_id FROM bookmarks WHERE state != 'removed' ORDER BY seq DESC")]

    def seed(self, ids):
        bm.apply_incremental(self.store, list(ids), list(ids), make_posts(*ids), SEEDED)


class FindStopTest(unittest.TestCase):
    KNOWN = set("k" + str(i) for i in range(20))

    def test_run_of_ten(self):
        ids = ["n1", "k3", "n2"] + ["k" + str(i) for i in range(10, 20)]
        self.assertEqual(bm.find_stop(ids, self.KNOWN), 3)

    def test_nine_is_not_enough(self):
        ids = ["n1"] + ["k" + str(i) for i in range(9)]
        self.assertIsNone(bm.find_stop(ids, self.KNOWN))

    def test_custom_run(self):
        self.assertEqual(bm.find_stop(["k1", "k2"], {"k1", "k2"}, run=2), 0)

    def test_empty(self):
        self.assertIsNone(bm.find_stop([], self.KNOWN))


class IncrementalTest(ApplyBase):
    def test_first_sync_order(self):
        ids = ["n" + str(i) for i in range(1, 151)]
        bm.apply_incremental(self.store, ids, ids, make_posts(*ids), NOW)
        self.assertEqual(self.order(), ids)
        r = self.rows()["n1"]
        self.assertEqual((r["state"], r["first_seen_at"], r["last_seen_at"], r["state_at"]), ("active", NOW, NOW, NOW))
        self.assertEqual(self.store.get_state("last_sync_at"), NOW)
        self.assertEqual(self.store.get_post("n1")["text"], "post n1")

    def test_new_and_moved_go_on_top(self):
        old = ["k" + str(i) for i in range(20, 0, -1)]
        self.seed(old)
        new = ["k5"] + ["n" + str(i) for i in range(1, 151)]
        rest = [k for k in old if k != "k5"]
        bm.apply_incremental(self.store, new, new + rest, make_posts(*new[1:]), NOW)
        self.assertEqual(self.order(), new + rest)
        rows = self.rows()
        self.assertEqual(rows["k5"]["first_seen_at"], SEEDED)
        self.assertEqual(rows["k1"]["last_seen_at"], NOW)

    def test_removed_reappears_active(self):
        self.seed(["a", "b"])
        self.store.conn.execute("UPDATE bookmarks SET state = 'removed' WHERE post_id = 'a'")
        bm.apply_incremental(self.store, ["a"], ["a", "b"], [], NOW)
        self.assertEqual(self.rows()["a"]["state"], "active")
        self.assertEqual(self.rows()["a"]["state_at"], NOW)
        self.assertEqual(self.order(), ["a", "b"])

    def test_never_marks_removed(self):
        self.seed(["a", "b", "c"])
        bm.apply_incremental(self.store, [], ["a"], [], NOW)
        self.assertEqual(set(r["state"] for r in self.rows().values()), set(["active"]))

    def test_unavailable_marked_and_text_kept(self):
        self.seed(["a", "b"])
        bm.apply_incremental(self.store, [], ["b"], make_posts("b"), NOW, unavailable={"a": "protected"})
        got = self.store.get_post("a")
        self.assertEqual((got["unavailable"], got["text"]), ("protected", "post a"))
        self.assertIsNone(self.store.get_post("b")["unavailable"])


class FullTest(ApplyBase):
    def test_below_window_marks_removed(self):
        self.seed(["c", "b", "a"])
        result = bm.apply_full(self.store, ["c", "a"], make_posts("c", "a"), NOW)
        self.assertEqual(result, {"removed": 1, "beyond_window": 0, "seen": 2})
        rows = self.rows()
        self.assertEqual((rows["b"]["state"], rows["b"]["state_at"]), ("removed", NOW))
        self.assertEqual(self.order(), ["c", "a"])
        self.assertEqual(self.store.get_state("last_full_sync_at"), NOW)
        self.assertEqual(self.store.get_state("last_full_count"), "2")
        self.assertEqual(self.store.get_state("last_sync_at"), NOW)

    def test_full_window_marks_beyond_window(self):
        ids = ["p" + str(i) for i in range(805)]
        self.seed(ids)
        result = bm.apply_full(self.store, ids[:800], [], NOW)
        self.assertEqual(result, {"removed": 0, "beyond_window": 5, "seen": 800})
        self.assertEqual(self.rows()["p804"]["state"], "beyond_window")
        self.assertEqual(self.order()[:800], ids[:800])

    def test_incomplete_listing_never_removes(self):
        self.seed(["c", "b", "a"])
        result = bm.apply_full(self.store, ["c", "a"], [], NOW, complete=False)
        self.assertEqual(result, {"removed": 0, "beyond_window": 1, "seen": 2})
        self.assertEqual(self.rows()["b"]["state"], "beyond_window")

    def test_complete_listing_removes_beyond_window(self):
        self.seed(["c", "b", "a"])
        bm.apply_full(self.store, ["c", "a"], [], NOW, complete=False)
        result = bm.apply_full(self.store, ["c", "a"], [], NOW)
        self.assertEqual(result, {"removed": 1, "beyond_window": 0, "seen": 2})
        self.assertEqual(self.rows()["b"]["state"], "removed")

    def test_full_marks_unavailable(self):
        self.seed(["a", "b"])
        bm.apply_full(self.store, ["b"], make_posts("b"), NOW, unavailable={"a": "deleted"})
        got = self.store.get_post("a")
        self.assertEqual((got["unavailable"], got["text"]), ("deleted", "post a"))

    def test_seq_reassigned_from_position(self):
        self.seed(["a", "b", "c"])
        bm.apply_full(self.store, ["c", "b", "a"], [], NOW)
        self.assertEqual(self.order(), ["c", "b", "a"])

    def test_beyond_window_seen_again_is_active(self):
        ids = ["p" + str(i) for i in range(805)]
        self.seed(ids)
        bm.apply_full(self.store, ids[:800], [], NOW)
        result = bm.apply_full(self.store, ["p804"] + ids[:799], [], NOW)
        self.assertEqual(self.rows()["p804"]["state"], "active")
        self.assertEqual(self.rows()["p799"]["state"], "beyond_window")
        self.assertEqual(result["beyond_window"], 5)

    def test_full_is_atomic(self):
        self.seed(["a"])
        with mock.patch.object(bm, "refresh_fts", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                bm.apply_full(self.store, [], [], NOW)
        self.assertEqual(self.rows()["a"]["state"], "active")
        self.assertIsNone(self.store.get_state("last_full_sync_at"))


class SearchTest(ApplyBase):
    def setUp(self):
        super().setUp()
        self.store.upsert_users([{"id": "42", "username": "example", "display_name": "Example Person"},
                                 {"id": "43", "username": "other", "display_name": "Other"}], NOW)
        raw = [
            {"id": "1", "text": "Agent first programming languages", "author_id": "42",
             "created_at": "2026-09-01T00:00:00.000Z"},
            {"id": "2", "text": "Local-first agents and sync", "author_id": "43",
             "created_at": "2026-09-15T00:00:00.000Z"},
            {"id": "3", "text": "Café culture, unrelated", "author_id": "43",
             "created_at": "2026-09-20T00:00:00.000Z"},
            {"id": "4", "text": "agent agent agent tooling", "author_id": "42",
             "created_at": "2026-09-25T00:00:00.000Z"},
        ]
        users = self.store.get_users(["42", "43"])
        posts = [normalize.normalize_post(r, users, "bookmark") for r in raw]
        bm.apply_incremental(self.store, ["4", "3", "2", "1"], ["4", "3", "2", "1"], posts, NOW)

    def ids(self, items):
        return [p["id"] for p in items]

    def add_folder(self):
        self.store.conn.execute("INSERT INTO bookmark_folders(id, name, fetched_at, members_synced_at) "
                                "VALUES ('f1', 'Ideas', ?, ?)", (NOW, NOW))
        self.store.conn.execute("INSERT INTO bookmark_folder_items(folder_id, post_id) VALUES ('f1', '2')")

    def test_phrase(self):
        self.assertEqual(self.ids(bm.search_bookmarks(self.store, '"agent first"')), ["1"])

    def test_prefix_and_or(self):
        self.assertEqual(set(self.ids(bm.search_bookmarks(self.store, "agen*"))), set(["1", "2", "4"]))
        self.assertEqual(set(self.ids(bm.search_bookmarks(self.store, "cafe OR sync"))), set(["2", "3"]))

    def test_rank(self):
        self.assertEqual(self.ids(bm.search_bookmarks(self.store, "agent")), ["4", "1"])

    def test_author_indexed(self):
        self.assertEqual(set(self.ids(bm.search_bookmarks(self.store, "person"))), set(["1", "4"]))

    def test_from_user(self):
        self.assertEqual(self.ids(bm.search_bookmarks(self.store, "agen*", from_user="@Other")), ["2"])

    def test_dates(self):
        since = datetime(2026, 9, 10, tzinfo=timezone.utc)
        until = datetime(2026, 9, 20, tzinfo=timezone.utc)
        self.assertEqual(self.ids(bm.search_bookmarks(self.store, "agen*", since=since, until=until)), ["2"])

    def test_removed_excluded_unless_asked(self):
        self.store.conn.execute("UPDATE bookmarks SET state = 'removed' WHERE post_id = '4'")
        self.assertEqual(set(self.ids(bm.search_bookmarks(self.store, "agen*"))), set(["1", "2"]))
        self.assertEqual(set(self.ids(bm.search_bookmarks(self.store, "agen*", include_removed=True))),
                         set(["1", "2", "4"]))

    def test_beyond_window_included(self):
        self.store.conn.execute("UPDATE bookmarks SET state = 'beyond_window' WHERE post_id = '1'")
        (item,) = bm.search_bookmarks(self.store, '"agent first"')
        self.assertEqual(item["bookmark"]["state"], "beyond_window")

    def test_item_shape(self):
        (item,) = bm.search_bookmarks(self.store, '"agent first"')
        self.assertEqual(item["origin"], "bookmark")
        self.assertEqual(item["url"], "https://x.com/example/status/1")
        self.assertEqual(item["author"], {"id": "42", "username": "example", "display_name": "Example Person"})
        self.assertEqual(item["bookmark"], {"folder_ids": [], "first_seen_at": NOW, "state": "active", "state_at": NOW})

    def test_folder_filter(self):
        self.add_folder()
        items = bm.search_bookmarks(self.store, "agen*", folder="f1")
        self.assertEqual(self.ids(items), ["2"])
        self.assertEqual(items[0]["bookmark"]["folder_ids"], ["f1"])

    def test_syntax_error(self):
        with self.assertRaises(XError) as ctx:
            bm.search_bookmarks(self.store, '"unterminated')
        self.assertEqual(ctx.exception.code, "X_QUERY_INVALID")

    def test_limit(self):
        self.assertEqual(len(bm.search_bookmarks(self.store, "agen*", limit=2)), 2)

    def test_list(self):
        self.assertEqual(self.ids(bm.list_bookmarks(self.store)), ["4", "3", "2", "1"])
        self.assertEqual(self.ids(bm.list_bookmarks(self.store, limit=2, offset=1)), ["3", "2"])
        self.add_folder()
        self.assertEqual(self.ids(bm.list_bookmarks(self.store, folder="f1")), ["2"])

    def test_author_change_reindexes_without_manual_refresh(self):
        self.store.upsert_users([{"id": "43", "username": "other", "display_name": "Zed"}], NOW)
        self.assertEqual(set(self.ids(bm.search_bookmarks(self.store, "zed"))), set(["2", "3"]))

    def test_post_text_change_reindexes_without_manual_refresh(self):
        users = self.store.get_users(["43"])
        changed = normalize.normalize_post({"id": "3", "text": "Edited about teapots", "author_id": "43",
                                            "created_at": "2026-09-20T00:00:00.000Z"}, users, "bookmark")
        self.store.upsert_posts([changed], NOW)
        self.assertEqual(self.ids(bm.search_bookmarks(self.store, "teapots")), ["3"])
        self.assertEqual(self.ids(bm.search_bookmarks(self.store, "culture")), [])

    def test_non_bookmark_posts_not_indexed(self):
        self.store.upsert_posts(make_posts("loose"), NOW)
        self.assertEqual(self.ids(bm.search_bookmarks(self.store, "loose")), [])


if __name__ == "__main__":
    unittest.main()
