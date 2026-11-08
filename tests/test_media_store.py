import threading
import unittest

from tests.helpers import NOW, TempHome, fixture
from tests.media_helpers import GIF, PHOTO, PHOTO_4A, VIDEO, pending, photo, stored
from fav import bookmarks as bm
from fav import media, paths
from fav import store as st
from fav.errors import XError

UNAVAILABLE_4B = {"key": "3_4b", "unavailable": True}


class NormalizeMediaTest(unittest.TestCase):
    def test_fixture_items(self):
        index = media.media_index(fixture("media_lookup.json"))
        self.assertEqual(index, {"3_1": PHOTO, "13_2": VIDEO, "16_3": GIF, "3_4a": PHOTO_4A})

    def test_variant_order(self):
        raw = {"media_key": "13_9", "type": "video", "variants": [
            {"content_type": "application/x-mpegURL", "url": "https://v/a.m3u8"},
            {"content_type": "video/mp4", "url": "https://v/low.mp4", "bit_rate": 256000},
            {"content_type": "video/webm", "url": "https://v/b.webm", "bit_rate": 900000},
            {"content_type": "video/mp4", "url": "https://v/none.mp4"},
            {"content_type": "video/mp4", "url": "https://v/high.mp4", "bit_rate": 2176000},
        ]}
        urls = [v["url"] for v in media.normalize_media(raw)["variants"]]
        self.assertEqual(urls, ["https://v/high.mp4", "https://v/low.mp4", "https://v/none.mp4", "https://v/a.m3u8",
                                "https://v/b.webm"])

    def test_wrong_types_dropped(self):
        raw = {"media_key": "3_8", "type": 7, "url": None, "width": "1200", "height": 800.5, "alt_text": ["x"],
               "duration_ms": True, "preview_image_url": "https://p/8.jpg", "public_metrics": {"view_count": 1},
               "variants": [{"content_type": "video/mp4"}, "junk",
                            {"content_type": "video/mp4", "url": "https://v/8.mp4", "bit_rate": "high"}]}
        self.assertEqual(media.normalize_media(raw), {
            "key": "3_8", "preview_image_url": "https://p/8.jpg",
            "variants": [{"content_type": "video/mp4", "url": "https://v/8.mp4"}]})
        self.assertEqual(media.normalize_media({"media_key": "3_9", "variants": "none"}), {"key": "3_9"})
        self.assertEqual(media.normalize_media({"media_key": "3_9", "variants": [{"url": 1}]}), {"key": "3_9"})

    def test_malformed_entries_are_upstream(self):
        for includes in ({"media": [{"type": "photo"}]}, {"media": ["3_1"]}, {"media": [{"media_key": 31}]},
                         {"media": {"media_key": "3_1"}}, ["media"]):
            with self.subTest(includes=includes):
                with self.assertRaises(XError) as ctx:
                    media.media_index({"data": [], "includes": includes})
                self.assertEqual(ctx.exception.code, "X_UPSTREAM")
        self.assertEqual(media.media_index({"data": []}), {})
        self.assertEqual(media.media_index({"data": [], "includes": {"users": []}}), {})

    def test_pending(self):
        self.assertTrue(media.is_pending({"key": "3_1"}))
        self.assertFalse(media.is_pending(PHOTO))
        self.assertFalse(media.is_pending(UNAVAILABLE_4B))
        posts = [{"id": "1", "media": pending(["a", "b"]) + [PHOTO], "unavailable": None},
                 {"id": "2", "media": pending(["c"]), "unavailable": "deleted"},
                 {"id": "3", "unavailable": None}]
        self.assertEqual(media.pending_count(posts), 2)


class StoreMediaBase(TempHome):
    def setUp(self):
        super().setUp()
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        self.db_file = p.db_file
        self.store = st.Store(self.db_file)
        self.addCleanup(self.store.close)

    def media_of(self, post_id, store=None):
        return (store or self.store).get_post(post_id).get("media")

    def statements(self, work):
        seen = []
        self.store.conn.set_trace_callback(seen.append)
        try:
            work()
        finally:
            self.store.conn.set_trace_callback(None)
        return [s.strip().upper() for s in seen]


class UpsertMediaTest(StoreMediaBase):
    def test_resave_same_keys_keeps_detail(self):
        self.store.upsert_posts([stored("2001", [PHOTO])], NOW)
        self.store.upsert_posts([stored("2001", pending(["3_1"]), text="newer text")], NOW)
        self.assertEqual(self.media_of("2001"), [PHOTO])
        self.assertEqual(self.store.get_post("2001")["text"], "newer text")

    def test_different_keys_replace(self):
        self.store.upsert_posts([stored("2001", [PHOTO])], NOW)
        self.store.upsert_posts([stored("2001", pending(["3_1", "3_5"]))], NOW)
        self.assertEqual(self.media_of("2001"), pending(["3_1", "3_5"]))
        self.store.upsert_posts([stored("2001", None)], NOW)
        self.assertIsNone(self.media_of("2001"))

    def test_key_order_counts(self):
        self.store.upsert_posts([stored("2004", [PHOTO, PHOTO_4A])], NOW)
        self.store.upsert_posts([stored("2004", pending(["3_4a", "3_1"]))], NOW)
        self.assertEqual(self.media_of("2004"), pending(["3_4a", "3_1"]))

    def test_upsert_opens_immediate_transaction(self):
        seen = self.statements(lambda: self.store.upsert_posts([stored("2001", pending(["3_1"]))], NOW))
        self.assertTrue(any(s.startswith("BEGIN IMMEDIATE") for s in seen), seen)

    def test_upsert_inside_caller_transaction(self):
        with self.store.transaction(immediate=True):
            self.store.upsert_posts([stored("2001", [PHOTO])], NOW)
            self.store.upsert_posts([stored("2001", pending(["3_1"]))], NOW)
        self.assertEqual(self.media_of("2001"), [PHOTO])


class MergeMediaTest(StoreMediaBase):
    def test_merge_by_key(self):
        self.store.upsert_posts([stored("2004", pending(["3_4a", "3_4b"]))], NOW)
        changed = self.store.merge_media({"2004": ["3_4a", "3_4b"]}, {"3_4a": PHOTO_4A})
        self.assertEqual(changed, [])
        self.assertEqual(self.media_of("2004"), [PHOTO_4A, UNAVAILABLE_4B])
        self.assertEqual(self.store.get_post("2004")["text"], "stored text 2004")
        self.assertEqual(self.store.get_post("2004")["created_at"], "2026-10-01T10:00:00Z")

    def test_resolved_item_x_omits_keeps_detail(self):
        self.store.upsert_posts([stored("2005", [PHOTO] + pending(["3_5"]))], NOW)
        self.store.merge_media({"2005": ["3_1", "3_5"]}, {"3_5": photo("3_5")})
        self.assertEqual(self.media_of("2005"), [PHOTO, photo("3_5")])

    def test_changed_keys_replace_the_list(self):
        self.store.upsert_posts([stored("2001", pending(["3_1"])), stored("2002", pending(["13_2"]))], NOW)
        changed = self.store.merge_media({"2001": ["3_1", "3_7"], "2002": []}, {"3_1": PHOTO})
        self.assertEqual(changed, ["2001", "2002"])
        self.assertEqual(self.media_of("2001"), [PHOTO, {"key": "3_7", "unavailable": True}])
        self.assertIsNone(self.media_of("2002"))

    def test_unknown_post_is_skipped(self):
        self.assertEqual(self.store.merge_media({"999": ["3_1"]}, {"3_1": PHOTO}), [])
        self.assertIsNone(self.store.get_post("999"))

    def test_merge_opens_immediate_transaction(self):
        self.store.upsert_posts([stored("2001", pending(["3_1"]))], NOW)
        seen = self.statements(lambda: self.store.merge_media({"2001": ["3_1"]}, {"3_1": PHOTO}))
        self.assertTrue(any(s.startswith("BEGIN IMMEDIATE") for s in seen), seen)

    def test_reset_unavailable(self):
        self.store.upsert_posts([stored("2004", [PHOTO_4A, UNAVAILABLE_4B]), stored("2006", [UNAVAILABLE_4B])], NOW)
        self.store.reset_unavailable_media(["2004"])
        self.assertEqual(self.media_of("2004"), [PHOTO_4A, {"key": "3_4b"}])
        self.assertEqual(self.media_of("2006"), [UNAVAILABLE_4B])


class AltTextSearchTest(StoreMediaBase):
    def bookmark(self, post):
        bm.apply_incremental(self.store, [post["id"]], [post["id"]], [post], NOW)

    def found(self, query):
        return [item["id"] for item in bm.search_bookmarks(self.store, query)]

    def test_alt_text_is_indexed(self):
        self.bookmark(stored("2001", pending(["3_1"]), text="plain words"))
        self.assertEqual(self.found("agent"), [])
        self.store.merge_media({"2001": ["3_1"]}, {"3_1": PHOTO})
        self.assertEqual(self.found("agent"), ["2001"])
        self.assertEqual(self.found('"agent loop"'), ["2001"])
        (row,) = self.store.conn.execute("SELECT text FROM bookmark_fts").fetchall()
        self.assertEqual(row["text"], "plain words\nDiagram of the agent loop")

    def test_resync_keeps_alt_text_indexed(self):
        self.bookmark(stored("2001", pending(["3_1"]), text="plain words"))
        self.store.merge_media({"2001": ["3_1"]}, {"3_1": PHOTO})
        self.bookmark(stored("2001", pending(["3_1"]), text="plain words"))
        self.assertEqual(self.found("diagram"), ["2001"])

    def test_text_without_alt_is_unchanged(self):
        self.bookmark(stored("2002", pending(["13_2"]), text="video words"))
        self.store.merge_media({"2002": ["13_2"]}, {"13_2": VIDEO})
        (row,) = self.store.conn.execute("SELECT text FROM bookmark_fts").fetchall()
        self.assertEqual(row["text"], "video words")


class MediaRaceTest(StoreMediaBase):
    """One connection writes while a second, in another thread, starts its own write.

    Under BEGIN IMMEDIATE the second waits for the first and then lands. Under a deferred BEGIN, a read
    followed by a write fails with SQLITE_BUSY_SNAPSHOT once the other connection has committed."""

    def race(self, first, second):
        state = {"thread": None}
        errors = []

        def run_second():
            other = st.Store(self.db_file)
            try:
                second(other)
            except BaseException as e:  # reported by the assertion below
                errors.append(e)
            finally:
                other.close()

        def trace(sql):
            text = sql.lstrip().upper()
            if state["thread"] is not None or "POSTS" not in text:
                return
            if not text.startswith(("INSERT", "UPDATE", "REPLACE")):
                return
            state["thread"] = threading.Thread(target=run_second)
            state["thread"].start()
            state["thread"].join(0.5)

        self.store.conn.set_trace_callback(trace)
        try:
            first(self.store)
        finally:
            self.store.conn.set_trace_callback(None)
        self.assertIsNotNone(state["thread"], "the first writer never wrote to posts")
        state["thread"].join(15)
        self.assertFalse(state["thread"].is_alive())
        self.assertEqual(errors, [])

    def test_resave_racing_merge(self):
        self.store.upsert_posts([stored("2001", pending(["3_1"]))], NOW)
        self.race(lambda s: s.upsert_posts([stored("2001", pending(["3_1"]), text="resaved")], NOW),
                  lambda s: s.merge_media({"2001": ["3_1"]}, {"3_1": PHOTO}))
        self.assertEqual(self.media_of("2001"), [PHOTO])
        self.assertEqual(self.store.get_post("2001")["text"], "resaved")

    def test_merge_racing_resave(self):
        self.store.upsert_posts([stored("2001", pending(["3_1"]))], NOW)
        self.race(lambda s: s.merge_media({"2001": ["3_1"]}, {"3_1": PHOTO}),
                  lambda s: s.upsert_posts([stored("2001", pending(["3_1"]), text="resaved")], NOW))
        self.assertEqual(self.media_of("2001"), [PHOTO])
        self.assertEqual(self.store.get_post("2001")["text"], "resaved")


if __name__ == "__main__":
    unittest.main()
