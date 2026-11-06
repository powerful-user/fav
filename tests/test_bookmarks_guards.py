import sqlite3
import unittest

from tests.helpers import NOW, page, raw_post, response, write_token
from tests.test_bookmarks_sync import SyncBase
from fav import bookmarks as bm
from fav.errors import XError


class PageBoundsTest(SyncBase):
    def cache_author(self):
        self.ctx.store.upsert_users([{"id": "42", "username": "example", "display_name": "Example"}], NOW)

    def test_oversized_bookmark_page_applies_nothing(self):
        self.seed(["a"])
        self.transport.push(self.bm_page(["n" + str(i) for i in range(101)]))
        with self.assertRaises(XError) as ctx:
            bm.sync(self.ctx)
        self.assertEqual(ctx.exception.code, "X_UPSTREAM")
        self.assertEqual(self.listed(), ["a"])

    def test_oversized_folder_page_is_not_applied(self):
        self.cache_author()
        self.transport.push(self.bm_page(["c"]), response(200, {"data": [{"id": "f1", "name": "Ideas"}]}),
                            response(200, {"data": [{"id": "m" + str(i)} for i in range(101)]}))
        result = bm.sync(self.ctx, full=True)
        self.assertIn("folders not synced: X_UPSTREAM", " ".join(result["summary"]))
        count = self.ctx.store.conn.execute("SELECT COUNT(*) FROM bookmark_folder_items").fetchone()[0]
        self.assertEqual(count, 0)

    def test_oversized_hydration_page_is_not_applied(self):
        self.cache_author()
        self.transport.push(self.bm_page(["c"]), response(200, {"data": [{"id": "f1", "name": "Ideas"}]}),
                            response(200, {"data": [{"id": "x1"}]}), page([raw_post("x1"), raw_post("x2")]))
        result = bm.sync(self.ctx, full=True)
        self.assertIn("folders not synced: X_UPSTREAM", " ".join(result["summary"]))
        self.assertIsNone(self.ctx.store.get_post("x1"))
        synced = self.ctx.store.conn.execute("SELECT members_synced_at FROM bookmark_folders").fetchone()[0]
        self.assertIsNone(synced)


class IdentifierTest(SyncBase):
    def test_folder_id_unsafe_for_a_path(self):
        self.transport.push(response(200, {"data": [{"id": "../f1", "name": "Ideas"}]}))
        with self.assertRaises(XError) as ctx:
            bm.list_folders(self.ctx)
        self.assertEqual(ctx.exception.code, "X_UPSTREAM")

    def test_user_id_unsafe_for_a_path(self):
        write_token(self.env, user_id="7/../8")
        with self.assertRaises(XError) as ctx:
            bm.sync(self.ctx)
        self.assertEqual(ctx.exception.code, "X_AUTH_REQUIRED")
        self.assertEqual(self.transport.requests, [])


class MalformedPageTest(SyncBase):
    """A page whose data is not a list, or is missing while meta counts results, is never read as empty."""

    def cache_author(self):
        self.ctx.store.upsert_users([{"id": "42", "username": "example", "display_name": "Example"}], NOW)

    def test_malformed_bookmark_page_removes_nothing(self):
        bad_pages = ({"data": "invalid", "meta": {"result_count": 3}}, {"meta": {"result_count": 3}})
        for payload in bad_pages:
            with self.subTest(payload=payload):
                self.seed(["a"])
                self.transport.push(response(200, payload))
                with self.assertRaises(XError) as ctx:
                    bm.sync(self.ctx, full=True)
                self.assertEqual(ctx.exception.code, "X_UPSTREAM")
                self.assertEqual(self.listed(), ["a"])

    def test_bookmark_page_count_that_disagrees_removes_nothing(self):
        bad_pages = ({"data": [], "meta": {"result_count": 3}},
                     {"data": [raw_post("a")], "meta": {"result_count": 2}},
                     {"data": [raw_post("a")], "meta": {"result_count": True}},
                     {"data": [], "meta": {"result_count": None}},
                     {"meta": {"result_count": None}})
        for payload in bad_pages:
            with self.subTest(payload=payload):
                self.seed(["a", "b"])
                self.transport.push(response(200, payload))
                with self.assertRaises(XError) as ctx:
                    bm.sync(self.ctx, full=True)
                self.assertEqual(ctx.exception.code, "X_UPSTREAM")
                self.assertEqual(self.listed(), ["a", "b"])

    def test_folder_page_count_that_disagrees(self):
        self.cache_author()
        self.transport.push(self.bm_page(["c"]), response(200, {"data": [{"id": "f1", "name": "Ideas"}]}),
                            response(200, {"data": [], "meta": {"result_count": 2}}))
        result = bm.sync(self.ctx, full=True)
        self.assertIn("folders not synced: X_UPSTREAM", " ".join(result["summary"]))
        synced = self.ctx.store.conn.execute("SELECT members_synced_at FROM bookmark_folders").fetchone()[0]
        self.assertIsNone(synced)

    def test_hydration_count_that_disagrees_is_not_applied(self):
        self.cache_author()
        self.transport.push(self.bm_page(["c"]), response(200, {"data": [{"id": "f1", "name": "Ideas"}]}),
                            response(200, {"data": [{"id": "x1"}]}),
                            response(200, {"data": [raw_post("x1")], "meta": {"result_count": 2}}))
        result = bm.sync(self.ctx, full=True)
        self.assertIn("folders not synced: X_UPSTREAM", " ".join(result["summary"]))
        self.assertIsNone(self.ctx.store.get_post("x1"))
        count = self.ctx.store.conn.execute("SELECT COUNT(*) FROM bookmark_folder_items").fetchone()[0]
        self.assertEqual(count, 0)

    def test_errors_only_hydration_batch_is_still_valid(self):
        self.cache_author()
        gone = {"resource_id": "x1", "title": "Not Found Error", "detail": "Could not find tweet with ids: [x1]."}
        self.transport.push(self.bm_page(["c"]), response(200, {"data": [{"id": "f1", "name": "Ideas"}]}),
                            response(200, {"data": [{"id": "x1"}]}), response(200, {"errors": [gone]}))
        result = bm.sync(self.ctx, full=True)
        self.assertNotIn("folders not synced", " ".join(result["summary"]))
        members = [r[0] for r in self.ctx.store.conn.execute("SELECT post_id FROM bookmark_folder_items")]
        self.assertEqual(members, ["x1"])

    def test_empty_bookmark_page_is_still_empty(self):
        self.transport.push(response(200, {"meta": {"result_count": 0}}), response(200, {"meta": {"result_count": 0}}))
        result = bm.sync(self.ctx, full=True)
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["summary"], ["synced: 0 new, 0 moved, 1 page, 0 removed, 0 beyond window"])

    def test_folder_page_without_data_but_counted(self):
        self.cache_author()
        self.transport.push(self.bm_page(["c"]), response(200, {"data": [{"id": "f1", "name": "Ideas"}]}),
                            response(200, {"meta": {"result_count": 2}}))
        result = bm.sync(self.ctx, full=True)
        self.assertIn("folders not synced: X_UPSTREAM", " ".join(result["summary"]))
        synced = self.ctx.store.conn.execute("SELECT members_synced_at FROM bookmark_folders").fetchone()[0]
        self.assertIsNone(synced)

    def test_malformed_hydration_page_is_not_applied(self):
        self.cache_author()
        self.transport.push(self.bm_page(["c"]), response(200, {"data": [{"id": "f1", "name": "Ideas"}]}),
                            response(200, {"data": [{"id": "x1"}]}), response(200, {"data": "invalid"}))
        result = bm.sync(self.ctx, full=True)
        self.assertIn("folders not synced: X_UPSTREAM", " ".join(result["summary"]))
        count = self.ctx.store.conn.execute("SELECT COUNT(*) FROM bookmark_folder_items").fetchone()[0]
        self.assertEqual(count, 0)


class HydrationIdentityTest(SyncBase):
    def test_post_not_requested_is_not_applied(self):
        self.ctx.store.upsert_users([{"id": "42", "username": "example", "display_name": "Example"}], NOW)
        self.transport.push(self.bm_page(["c"]), response(200, {"data": [{"id": "f1", "name": "Ideas"}]}),
                            response(200, {"data": [{"id": "x1"}]}), page([raw_post("x2")]))
        result = bm.sync(self.ctx, full=True)
        self.assertIn("folders not synced: X_UPSTREAM", " ".join(result["summary"]))
        self.assertIsNone(self.ctx.store.get_post("x2"))
        count = self.ctx.store.conn.execute("SELECT COUNT(*) FROM bookmark_folder_items").fetchone()[0]
        self.assertEqual(count, 0)

    def test_member_neither_returned_nor_reported_keeps_stored_membership(self):
        self.ctx.store.upsert_users([{"id": "42", "username": "example", "display_name": "Example"}], NOW)
        self.ctx.store.conn.execute("INSERT INTO bookmark_folders (id, name, fetched_at) VALUES ('f1', 'Ideas', ?)",
                                    (NOW,))
        self.ctx.store.conn.execute("INSERT INTO bookmark_folder_items (folder_id, post_id) VALUES ('f1', 'old')")
        self.transport.push(self.bm_page(["c"]), response(200, {"data": [{"id": "f1", "name": "Ideas"}]}),
                            response(200, {"data": [{"id": "x1"}, {"id": "x2"}]}), page([raw_post("x1")]))
        result = bm.sync(self.ctx, full=True)
        self.assertIn("folders not synced: X_UPSTREAM", " ".join(result["summary"]))
        members = [r[0] for r in self.ctx.store.conn.execute("SELECT post_id FROM bookmark_folder_items")]
        self.assertEqual(members, ["old"])
        synced = self.ctx.store.conn.execute("SELECT members_synced_at FROM bookmark_folders").fetchone()[0]
        self.assertIsNone(synced)

    def test_member_reported_in_errors_is_complete(self):
        self.ctx.store.upsert_users([{"id": "42", "username": "example", "display_name": "Example"}], NOW)
        gone = {"resource_id": "x2", "title": "Not Found Error", "detail": "Could not find tweet with ids: [x2]."}
        self.transport.push(self.bm_page(["c"]), response(200, {"data": [{"id": "f1", "name": "Ideas"}]}),
                            response(200, {"data": [{"id": "x1"}, {"id": "x2"}]}),
                            page([raw_post("x1")], errors=[gone]))
        result = bm.sync(self.ctx, full=True)
        self.assertNotIn("folders not synced", " ".join(result["summary"]))
        members = [r[0] for r in self.ctx.store.conn.execute(
            "SELECT post_id FROM bookmark_folder_items ORDER BY post_id")]
        self.assertEqual(members, ["x1", "x2"])

    def test_error_entry_without_a_string_resource_id_does_not_account(self):
        self.ctx.store.upsert_users([{"id": "42", "username": "example", "display_name": "Example"}], NOW)
        for err in ({"title": "Not Found Error"}, {"resource_id": 2, "title": "Not Found Error"},
                    {"resource_id": "x9", "title": "Not Found Error"}, "x2"):
            with self.subTest(err=err):
                with self.ctx.store.transaction(immediate=True):
                    self.ctx.store.conn.execute("DELETE FROM bookmark_folder_items")
                    self.ctx.store.conn.execute("DELETE FROM posts WHERE id IN ('x1', 'x2')")
                self.transport.push(self.bm_page(["c"]), response(200, {"data": [{"id": "f1", "name": "Ideas"}]}),
                                    response(200, {"data": [{"id": "x1"}, {"id": "x2"}]}),
                                    page([raw_post("x1")], errors=[err]))
                result = bm.sync(self.ctx, full=True)
                self.assertIn("folders not synced: X_UPSTREAM", " ".join(result["summary"]))
                count = self.ctx.store.conn.execute("SELECT COUNT(*) FROM bookmark_folder_items").fetchone()[0]
                self.assertEqual(count, 0)

    def test_id_that_is_not_a_string(self):
        for bad in ([], {}):
            with self.subTest(id=bad):
                self.ctx.store.upsert_users([{"id": "42", "username": "example", "display_name": "Example"}], NOW)
                self.transport.push(self.bm_page(["c"]), response(200, {"data": [{"id": "f1", "name": "Ideas"}]}),
                                    response(200, {"data": [{"id": "x1"}]}), response(200, {"data": [{"id": bad}]}))
                result = bm.sync(self.ctx, full=True)
                self.assertIn("folders not synced: X_UPSTREAM", " ".join(result["summary"]))


class QueryErrorTest(SyncBase):
    def test_schema_error_is_not_a_query_error(self):
        self.seed(["a"])
        self.ctx.store.conn.execute("ALTER TABLE bookmarks RENAME COLUMN seq TO seq_old")
        with self.assertRaises(sqlite3.OperationalError):
            bm.search_bookmarks(self.ctx.store, "post")

    def test_every_fts_parse_error_is_a_query_error(self):
        self.seed(["a"])
        for bad in ("NEAR(foo bar,abc)", "nosuchcol:post", '"open', "*", "post AND"):
            with self.subTest(query=bad):
                with self.assertRaises(XError) as ctx:
                    bm.search_bookmarks(self.ctx.store, bad)
                self.assertEqual(ctx.exception.code, "X_QUERY_INVALID")

    def test_unknown_fts_column_is_a_query_error(self):
        self.seed(["a"])
        with self.assertRaises(XError) as ctx:
            bm.search_bookmarks(self.ctx.store, "nosuchcol:post")
        self.assertEqual(ctx.exception.code, "X_QUERY_INVALID")


if __name__ == "__main__":
    unittest.main()
