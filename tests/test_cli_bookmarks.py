import json
import os
import unittest

from tests.helpers import page, raw_post, response, users_response
from tests.test_cli import CliBase
from fav import paths
from fav import store as st


class BookmarkCliTest(CliBase):
    def sync_one(self):
        self.transport.push(page([raw_post("1", text="Agent first languages")]), users_response(("42", "example")))
        return self.run_cli("bookmarks", "sync")

    def test_sync_then_search_json(self):
        code, out, err = self.sync_one()
        self.assertEqual(code, 0, err)
        self.assertIn("1 new", out)
        code, out, err = self.run_cli("bookmarks", "search", "agent", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["items"][0]["url"], "https://x.com/example/status/1")
        self.assertEqual(data["items"][0]["origin"], "bookmark")
        self.assertEqual(data["last_sync"], "2026-10-02T12:00:00Z")
        self.assertEqual(len(self.transport.requests), 2)

    def test_search_plain_shows_last_sync(self):
        self.sync_one()
        self.clock.advance(hours=3)
        code, out, _ = self.run_cli("bookmarks", "search", "agent")
        self.assertEqual(code, 0)
        self.assertIn(" · bookmark · ", out)
        self.assertIn("last sync 3h ago", out)

    def test_list_never_synced(self):
        code, out, _ = self.run_cli("bookmarks", "list")
        self.assertEqual(code, 0)
        self.assertIn("no results", out)
        self.assertIn("last sync never", out)
        self.assertEqual(self.transport.requests, [])

    def test_folder_flag_shows_folder_sync(self):
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        s = st.Store(p.db_file)
        self.addCleanup(s.close)
        s.conn.execute("INSERT INTO bookmark_folders(id, name, fetched_at, members_synced_at) "
                       "VALUES ('f1', 'Ideas', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')")
        code, out, _ = self.run_cli("bookmarks", "list", "--folder", "f1", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["folder_synced_at"], "2026-10-01T00:00:00Z")
        code, out, _ = self.run_cli("bookmarks", "list", "--folder", "f1")
        self.assertIn("folder synced 1d ago", out)

    def test_sync_full_flag(self):
        self.transport.push(page([raw_post("1")]), response(200, {"meta": {"result_count": 0}}),
                            users_response(("42", "example")))
        code, out, err = self.run_cli("bookmarks", "sync", "--full")
        self.assertEqual(code, 0, err)
        self.assertIn("/2/users/7/bookmarks/folders", self.transport.paths())
        self.assertIn("0 removed", out)

    def test_folders(self):
        self.transport.push(response(200, {"data": [{"id": "f1", "name": "Ideas"}], "meta": {"result_count": 1}}))
        code, out, err = self.run_cli("bookmarks", "folders", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["folders"][0]["name"], "Ideas")

    def test_list_help_explains_first_seen(self):
        code, out, _ = self.run_cli("bookmarks", "list", "--help")
        self.assertEqual(code, 0)
        self.assertIn("when sync first saw", " ".join(out.split()))

    def test_search_syntax_error(self):
        code, _, err = self.run_cli("bookmarks", "search", '"open')
        self.assertEqual(code, 1)
        self.assertIn("X_QUERY_INVALID", err)

    def test_search_filters_parse(self):
        self.sync_one()
        code, out, err = self.run_cli("bookmarks", "search", "agent", "--from", "@example", "--since", "30d",
                                      "--until", "2026-10-03", "--limit", "5", "--include-removed", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)["items"]), 1)
        code, _, _ = self.run_cli("bookmarks", "search", "agent", "--since", "soon")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
