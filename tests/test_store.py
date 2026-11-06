import os
import sqlite3
import stat
import unittest
from datetime import datetime, timezone
from unittest import mock

from tests.helpers import NOW, TempHome
from wings_x import paths
from wings_x import store as st

POST = {
    "id": "1001",
    "url": "https://x.com/example/status/1001",
    "text": "hello agents",
    "author": {"id": "42", "username": "example", "display_name": "Example"},
    "created_at": "2026-10-01T10:00:00Z",
    "conversation_id": "1001",
    "lang": "en",
    "metrics": {"likes": 1, "reposts": 0, "replies": 0, "quotes": 0},
    "referenced": [{"type": "quoted", "id": "99", "url": "https://x.com/i/status/99"}],
    "origin": "live",
    "source": "x",
    "unavailable": None,
}

TABLES = ["posts", "users", "bookmarks", "bookmark_folders", "bookmark_folder_items", "bookmark_fts", "ledger",
          "charged", "sync_state"]


class StoreTest(TempHome):
    def open(self):
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        s = st.Store(p.db_file)
        self.addCleanup(s.close)
        return s, p

    def test_file_mode_and_pragmas(self):
        s, p = self.open()
        self.assertEqual(stat.S_IMODE(os.stat(p.db_file).st_mode), 0o600)
        self.assertEqual(s.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(s.conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_schema(self):
        s, _ = self.open()
        self.assertEqual(s.schema_version(), st.SCHEMA_VERSION)
        names = set(r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
        for table in TABLES:
            self.assertIn(table, names)
        columns = lambda table: [r[1] for r in s.conn.execute("PRAGMA table_info(" + table + ")")]
        self.assertEqual(columns("ledger"), ["id", "invocation_id", "pid", "at", "command", "endpoint", "state",
                                             "reserved_usd", "resources", "est_usd", "duration_ms", "result_count",
                                             "x_request_id", "http_status", "error_code"])
        self.assertEqual(columns("bookmarks"), ["post_id", "seq", "first_seen_at", "last_seen_at", "state", "state_at"])
        self.assertEqual(columns("posts"), ["id", "url", "text", "author_id", "created_at", "conversation_id", "lang",
                                            "metrics_json", "media_json", "referenced_json", "fetched_at", "unavailable"])
        self.assertEqual(columns("users"), ["id", "username", "display_name", "fetched_at"])
        self.assertEqual(columns("bookmark_folders"), ["id", "name", "fetched_at", "members_synced_at"])
        self.assertEqual(columns("bookmark_folder_items"), ["folder_id", "post_id"])
        self.assertEqual(columns("charged"), ["utc_day", "kind", "resource_id"])
        self.assertEqual(columns("sync_state"), ["key", "value"])
        self.assertEqual(columns("bookmark_fts"), ["post_id", "text", "author"])

    def test_reopen_keeps_data(self):
        s, p = self.open()
        s.upsert_posts([POST], NOW)
        s.close()
        again = st.Store(p.db_file)
        self.addCleanup(again.close)
        self.assertEqual(again.schema_version(), st.SCHEMA_VERSION)
        self.assertEqual(again.get_post("1001")["text"], "hello agents")

    def test_post_round_trip(self):
        s, _ = self.open()
        s.upsert_users([{"id": "42", "username": "example", "display_name": "Example"}], NOW)
        s.upsert_posts([POST], NOW)
        self.assertEqual(s.get_post("1001"), POST)

    def test_unresolved_author(self):
        s, _ = self.open()
        s.upsert_posts([POST], NOW)
        got = s.get_post("1001")
        self.assertEqual(got["author"], {"id": "42"})
        self.assertEqual(got["url"], "https://x.com/i/status/1001")

    def test_origin_and_missing(self):
        s, _ = self.open()
        s.upsert_posts([POST], NOW)
        self.assertEqual(s.get_post("1001", origin="bookmark")["origin"], "bookmark")
        self.assertIsNone(s.get_post("nope"))

    def test_minimal_post_omits_absent_fields(self):
        s, _ = self.open()
        s.upsert_posts([{"id": "5", "url": "https://x.com/i/status/5", "origin": "live", "source": "x",
                         "unavailable": None}], NOW)
        self.assertEqual(s.get_post("5"), {"id": "5", "url": "https://x.com/i/status/5", "origin": "live",
                                           "source": "x", "unavailable": None})

    def test_unavailable(self):
        s, _ = self.open()
        s.upsert_posts([POST], NOW)
        s.mark_unavailable("1001", "deleted")
        got = s.get_post("1001")
        self.assertEqual(got["unavailable"], "deleted")
        self.assertEqual(got["text"], "hello agents")
        s.upsert_posts([POST], NOW)
        self.assertIsNone(s.get_post("1001")["unavailable"])
        with self.assertRaises(ValueError):
            s.mark_unavailable("1001", "lost")

    def test_users(self):
        s, _ = self.open()
        s.upsert_users([{"id": "1", "username": "one", "display_name": "One"}], "2026-10-01T00:00:00Z")
        s.upsert_users([{"id": "2", "username": "two", "display_name": "Two"}], "2026-08-01T00:00:00Z")
        self.assertEqual(s.get_users(["1", "9"]), {"1": {"id": "1", "username": "one", "display_name": "One"}})
        now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
        self.assertEqual(s.users_needing_lookup(["3", "1", "2", "3"], now), ["3", "2"])

    def test_state(self):
        s, _ = self.open()
        self.assertIsNone(s.get_state("last_sync_at"))
        s.set_state("last_sync_at", NOW)
        s.set_state("last_sync_at", "2026-10-03T00:00:00Z")
        self.assertEqual(s.get_state("last_sync_at"), "2026-10-03T00:00:00Z")

    def test_transaction_rolls_back(self):
        s, _ = self.open()
        with self.assertRaises(RuntimeError):
            with s.transaction():
                s.set_state("a", "1")
                s.upsert_posts([POST], NOW)
                raise RuntimeError("boom")
        self.assertIsNone(s.get_state("a"))
        self.assertIsNone(s.get_post("1001"))

    def test_transaction_immediate_commits(self):
        s, _ = self.open()
        with s.transaction(immediate=True):
            s.set_state("a", "1")
        self.assertEqual(s.get_state("a"), "1")
        self.assertFalse(s.conn.in_transaction)

    def test_second_open_survives_stale_version_read(self):
        s, p = self.open()
        self.assertEqual(s.schema_version(), 1)
        s.close()
        real = st.Store.schema_version
        calls = {"n": 0}

        def stale_once(store):
            calls["n"] += 1
            if calls["n"] == 1:
                return 0
            return real(store)

        with mock.patch.object(st.Store, "schema_version", stale_once):
            second = st.Store(p.db_file)
        self.addCleanup(second.close)
        self.assertEqual(second.schema_version(), 1)

    def _open_fails_and_closes(self, connect):
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        captured = []

        def capture(*args, **kwargs):
            conn = connect(*args, **kwargs)
            captured.append(conn)
            return conn

        return p, captured, capture

    def test_init_closes_connection_when_migrate_fails(self):
        _path, captured, capture = self._open_fails_and_closes(sqlite3.connect)
        with mock.patch("wings_x.store.sqlite3.connect", capture):
            with mock.patch.object(st.Store, "_migrate", side_effect=RuntimeError("migrate failed")):
                with self.assertRaises(RuntimeError) as ctx:
                    st.Store(_path.db_file)
        self.assertEqual(str(ctx.exception), "migrate failed")
        self.assertEqual(len(captured), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            captured[0].execute("SELECT 1")

    def test_init_closes_connection_when_pragma_fails(self):
        real_connect = sqlite3.connect
        real_conns = []

        class Proxy:
            def __init__(self, conn):
                object.__setattr__(self, "_conn", conn)

            def execute(self, sql, *params, **kw):
                if isinstance(sql, str) and sql.startswith("PRAGMA journal_mode"):
                    raise sqlite3.OperationalError("pragma failed")
                return self._conn.execute(sql, *params, **kw)

            def close(self):
                self._conn.close()

            def __setattr__(self, name, value):
                setattr(self._conn, name, value)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            real_conns.append(conn)
            return Proxy(conn)

        _path, _captured, capture = self._open_fails_and_closes(connect)
        with mock.patch("wings_x.store.sqlite3.connect", capture):
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                st.Store(_path.db_file)
        self.assertEqual(str(ctx.exception), "pragma failed")
        self.assertEqual(len(real_conns), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            real_conns[0].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
