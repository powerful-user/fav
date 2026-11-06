import json
import unittest
from datetime import datetime, timezone

from tests.helpers import Clock, FakeTransport, TempHome, response, users_response, write_config
from wings_x import budget as bd
from wings_x import client as cl
from wings_x import config as cfg
from wings_x import output, paths, users
from wings_x import store as st
from wings_x.errors import XError


class UsersTest(TempHome):
    def setUp(self):
        super().setUp()
        write_config(self.env)
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        self.config = cfg.load_config(p.config_file)
        self.store = st.Store(p.db_file)
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.transport = FakeTransport()

    def client(self, extra=0.0):
        b = bd.Budget(self.store, self.config, invocation_id="inv", extra_usd=extra, now_fn=self.clock,
                      pid_alive=lambda pid: True)
        return cl.XClient(self.transport, token=lambda: "tok-1", budget=b, sleep=lambda s: None)

    def test_lookup_unknown_in_batches(self):
        ids = [str(i) for i in range(150)]
        self.store.upsert_users([{"id": "0", "username": "u0", "display_name": "U0"}], "2026-10-02T00:00:00Z")
        self.transport.push(users_response(*[(str(i), "u" + str(i)) for i in range(1, 101)]),
                            users_response(*[(str(i), "u" + str(i)) for i in range(101, 150)]))
        pending = users.resolve_authors(self.store, self.client(extra=1.0), ids, self.clock(), "search")
        self.assertEqual(pending, 0)
        self.assertEqual(self.transport.paths(), ["/2/users", "/2/users"])
        query = self.transport.query(0)
        self.assertEqual(query["ids"].split(","), [str(i) for i in range(1, 101)])
        self.assertEqual(query["user.fields"], "name,username")
        self.assertEqual(self.transport.query(1)["ids"].split(","), [str(i) for i in range(101, 150)])
        self.assertEqual(self.store.get_users(["149"])["149"], {"id": "149", "username": "u149", "display_name": "U149"})

    def test_cached_not_looked_up(self):
        self.store.upsert_users([{"id": "1", "username": "one", "display_name": "One"}], "2026-10-02T00:00:00Z")
        self.assertEqual(users.resolve_authors(self.store, self.client(), ["1", "1"], self.clock(), "search"), 0)
        self.assertEqual(self.transport.requests, [])

    def test_budget_short_leaves_pending(self):
        ids = [str(i) for i in range(120)]
        self.transport.push(users_response(*[(str(i), "u" + str(i)) for i in range(100)]))
        pending = users.resolve_authors(self.store, self.client(), ids, self.clock(), "search")
        self.assertEqual(pending, 20)
        self.assertEqual(len(self.transport.requests), 1)

    def test_ids_that_break_the_list_stay_pending(self):
        self.transport.push(users_response(("a7", "seven")))
        pending = users.resolve_authors(self.store, self.client(), ["a7", "1,2", "3\n"], self.clock(), "search")
        self.assertEqual(pending, 2)
        self.assertEqual(self.transport.query(0)["ids"], "a7")

    def test_unreturned_ids_count_as_pending(self):
        self.transport.push(response(200, {"data": [{"id": "1", "username": "one", "name": "One"}],
                                           "errors": [{"title": "Not Found Error", "resource_id": "2"}]}))
        self.assertEqual(users.resolve_authors(self.store, self.client(), ["1", "2"], self.clock(), "search"), 1)

    def test_other_errors_propagate(self):
        self.transport.push(response(401, {"title": "Unauthorized"}))
        with self.assertRaises(XError) as ctx:
            users.resolve_authors(self.store, self.client(), ["1"], self.clock(), "search")
        self.assertEqual(ctx.exception.code, "X_AUTH_EXPIRED")


NOW = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)

POST = {
    "id": "123",
    "url": "https://x.com/example/status/123",
    "text": "Post body",
    "author": {"id": "42", "username": "example", "display_name": "Example"},
    "created_at": "2026-10-02T12:00:00Z",
    "referenced": [{"type": "quoted", "id": "99", "url": "https://x.com/other/status/99"}],
    "origin": "live",
    "source": "x",
    "unavailable": None,
}


class OutputTest(unittest.TestCase):
    def test_plain_block(self):
        text = output.render_plain({"items": [POST], "next": None, "estimated_cost_usd": 0.005}, NOW)
        self.assertEqual(text, "@example · 2026-10-02 12:00Z · live · https://x.com/example/status/123\n"
                               "Post body\n"
                               "  ↳ quotes @other · https://x.com/other/status/99\n"
                               "\n"
                               "estimated cost $0.005\n")

    def test_plain_unresolved_bookmark_markers(self):
        item = {"id": "5", "url": "https://x.com/i/status/5", "text": "Saved", "author": {"id": "43"},
                "created_at": "2026-09-01T08:30:00Z",
                "referenced": [{"type": "replied_to", "id": "4", "url": "https://x.com/i/status/4",
                                "unavailable": "deleted"}],
                "origin": "bookmark", "source": "x", "unavailable": "deleted",
                "bookmark": {"folder_ids": [], "first_seen_at": "x", "state": "beyond_window", "state_at": "x"}}
        text = output.render_plain({"items": [item]}, NOW)
        self.assertEqual(text, "id:43 · 2026-09-01 08:30Z · bookmark · https://x.com/i/status/5 [deleted on X] "
                               "[beyond X's 800 window]\n"
                               "Saved\n"
                               "  ↳ replies to https://x.com/i/status/4 [deleted on X]\n")

    def test_plain_removed_and_reposts(self):
        item = {"id": "6", "url": "https://x.com/i/status/6", "origin": "bookmark", "source": "x",
                "unavailable": "protected", "referenced": [{"type": "retweeted", "id": "3",
                                                            "url": "https://x.com/i/status/3"}],
                "bookmark": {"folder_ids": [], "first_seen_at": "x", "state": "removed", "state_at": "x"}}
        text = output.render_plain({"items": [item]}, NOW)
        self.assertEqual(text, "? · bookmark · https://x.com/i/status/6 [protected on X] [removed]\n"
                               "  ↳ reposts https://x.com/i/status/3\n")

    def test_footer_order(self):
        result = {"summary": ["synced: 2 new, 0 moved, 1 page"], "items": [], "notes": ["replies older than 7 days are not searchable"],
                  "gaps": [{"after": "300", "missing": "200", "reason": "deleted"}], "next": "tok2", "truncated": True,
                  "authors_pending": 3, "last_sync": "2026-10-02T12:00:00Z", "folder_synced_at": None,
                  "estimated_cost_usd": 0.0123}
        self.assertEqual(output.render_plain(result, NOW), "synced: 2 new, 0 moved, 1 page\n"
                                                           "\n"
                                                           "note: replies older than 7 days are not searchable\n"
                                                           "gap: 200 (deleted) above 300\n"
                                                           "next: tok2\n"
                                                           "truncated: thread cap reached\n"
                                                           "authors pending: 3\n"
                                                           "last sync 3h ago\n"
                                                           "folder synced never\n"
                                                           "estimated cost $0.012\n")

    def test_no_results(self):
        self.assertEqual(output.render_plain({"items": [], "last_sync": None}, NOW), "no results\n\nlast sync never\n")

    def test_users_and_referenced_posts(self):
        user = {"id": "42", "username": "example", "display_name": "Example", "description": "hi",
                "url": "https://x.com/example", "metrics": {"followers": 10, "following": 2, "posts": 300}}
        self.assertEqual(output.render_plain({"users": [user]}, NOW),
                         "@example · Example · https://x.com/example\nhi\nfollowers 10 · following 2 · posts 300\n")
        ref = dict(POST, id="99", url="https://x.com/other/status/99", text="Original",
                   author={"id": "44", "username": "other", "display_name": "Other"}, referenced=[])
        text = output.render_plain({"items": [dict(POST, referenced=[])], "referenced_posts": [ref]}, NOW)
        self.assertEqual(text, "@example · 2026-10-02 12:00Z · live · https://x.com/example/status/123\nPost body\n"
                               "\nreferenced:\n\n"
                               "@other · 2026-10-02 12:00Z · live · https://x.com/other/status/99\nOriginal\n")

    def test_folders(self):
        result = {"folders": [{"id": "f1", "name": "Ideas", "members_synced_at": "2026-10-02T12:00:00Z"},
                              {"id": "f2", "name": "Later", "members_synced_at": None}],
                  "notes": ["folder list incomplete (page cap 1)"], "estimated_cost_usd": 0.001}
        self.assertEqual(output.render_plain(result, NOW), "f1 · Ideas · members synced 3h ago\n"
                                                           "f2 · Later · members synced never\n"
                                                           "\n"
                                                           "note: folder list incomplete (page cap 1)\n"
                                                           "estimated cost $0.001\n")

    def test_ago(self):
        self.assertEqual(output.ago(None, NOW), "never")
        self.assertEqual(output.ago("2026-10-02T14:59:40Z", NOW), "just now")
        self.assertEqual(output.ago("2026-10-02T14:55:00Z", NOW), "5m ago")
        self.assertEqual(output.ago("2026-10-02T12:00:00Z", NOW), "3h ago")
        self.assertEqual(output.ago("2026-09-30T15:00:00Z", NOW), "2d ago")

    def test_json(self):
        text = output.render_json({"items": [POST], "next": None})
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(json.loads(text), {"items": [POST], "next": None})
        self.assertIn("↳", output.render_plain({"items": [POST]}, NOW))
        self.assertNotIn("\\u", output.render_json({"items": [{"text": "café"}]}))


if __name__ == "__main__":
    unittest.main()
