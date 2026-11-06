import io
import json
import unittest
from datetime import datetime, timezone

from tests.helpers import Clock, FakeTransport, TempHome, fixture, response, users_response, write_config, write_token
from tests.media_helpers import GIF, PHOTO, VIDEO, charge, lookup, pending
from wings_x import cli, output, paths
from wings_x import store as st

NOW = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)
HEAD = "@example · 2026-10-02 12:00Z · live · https://x.com/example/status/123\n"


def item(media, **extra):
    post = {"id": "123", "url": "https://x.com/example/status/123", "text": "Post body",
            "author": {"id": "42", "username": "example"}, "created_at": "2026-10-02T12:00:00Z",
            "origin": "live", "source": "x", "unavailable": None, "media": media}
    post.update(extra)
    return post


class MediaLinesTest(unittest.TestCase):
    def render(self, post):
        return output.render_plain({"items": [post]}, NOW)

    def test_lines_come_before_references(self):
        post = item([PHOTO, VIDEO, GIF, {"key": "3_9"}, {"key": "3_4b", "unavailable": True}],
                    referenced=[{"type": "quoted", "id": "99", "url": "https://x.com/other/status/99"}])
        self.assertEqual(self.render(post), HEAD + "Post body\n"
                         "  ▣ photo 1200×800 https://pbs.twimg.com/media/p1.jpg\n"
                         "    alt: \"Diagram of the agent loop\"\n"
                         "  ▶ video 1280×720 0:47 preview https://pbs.twimg.com/ext_tw_video_thumb/2/pu/img/v.jpg\n"
                         "  ▶ GIF 498×280 preview https://pbs.twimg.com/tweet_video_thumb/g.jpg\n"
                         "  · 1 attachment pending\n"
                         "  · 1 attachment unavailable on X\n"
                         "  ↳ quotes @other · https://x.com/other/status/99\n")

    def test_partial_detail_and_plurals(self):
        post = item([{"key": "3_1", "type": "photo", "alt_text": "line one\nline two"},
                     {"key": "13_2", "type": "video", "duration_ms": 3725000, "width": 1280},
                     {"key": "7_1", "url": "https://pbs.twimg.com/media/x.jpg"}] + pending(["a", "b"]))
        self.assertEqual(self.render(post), HEAD + "Post body\n"
                         "  ▣ photo\n"
                         "    alt: \"line one line two\"\n"
                         "  ▶ video 62:05\n"
                         "  ▣ media https://pbs.twimg.com/media/x.jpg\n"
                         "  · 2 attachments pending\n")

    def test_no_media_no_lines(self):
        self.assertEqual(self.render(item(None)), HEAD + "Post body\n")
        self.assertEqual(self.render(item([])), HEAD + "Post body\n")


class MediaFooterTest(unittest.TestCase):
    def test_pending_and_error_after_authors(self):
        result = {"items": [], "authors_pending": 3, "media_pending": 2, "media_error": "X_RATE_LIMITED",
                  "estimated_cost_usd": 0.01}
        self.assertEqual(output.render_plain(result, NOW), "no results\n\n"
                                                           "authors pending: 3\n"
                                                           "media pending: 2\n"
                                                           "media lookup failed: X_RATE_LIMITED\n"
                                                           "estimated cost $0.010\n")

    def test_zero_pending_prints_nothing(self):
        self.assertEqual(output.render_plain({"items": [], "media_pending": 0}, NOW), "no results\n")

    def test_sync_reports_the_failure_once(self):
        result = {"summary": ["synced: 1 new, 0 moved, 1 page", "media lookup failed: X_RATE_LIMITED"], "items": [],
                  "media_pending": 1, "media_error": "X_RATE_LIMITED"}
        text = output.render_plain(result, NOW)
        self.assertEqual(text.count("media lookup failed: X_RATE_LIMITED"), 1)
        self.assertIn("media pending: 1\n", text)


class MediaCliTest(TempHome):
    def setUp(self):
        super().setUp()
        write_config(self.env)
        write_token(self.env)
        self.transport = FakeTransport()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), env=self.env, stdout=out, stderr=err, stdin=io.StringIO(""),
                        transport=self.transport, now_fn=Clock(), sleep=lambda s: None, loopback=False)
        return code, out.getvalue(), err.getvalue()

    def push_search(self):
        self.transport.push(response(200, fixture("search_recent.json")),
                            users_response(("42", "example"), ("43", "other")), lookup(("1002", ["3_1"])))

    def test_search_json(self):
        self.push_search()
        code, out, err = self.run_cli("search", "agents", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["items"][1]["media"], [PHOTO])
        self.assertEqual(data["media_pending"], 0)
        self.assertNotIn("media_error", data)

    def test_search_plain(self):
        self.push_search()
        code, out, err = self.run_cli("search", "agents")
        self.assertEqual(code, 0, err)
        self.assertIn("Reply about MCP harnesses\n"
                      "  ▣ photo 1200×800 https://pbs.twimg.com/media/p1.jpg\n"
                      "    alt: \"Diagram of the agent loop\"\n"
                      "  ↳ replies to https://x.com/i/status/900\n", out)

    def seed_charged(self):
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        s = st.Store(p.db_file)
        try:
            charge(s, "post_read", ["1", "2"])
            charge(s, "user_read", ["42"])
            charge(s, "media_read", ["3_1", "3_2", "3_3"])
            charge(s, "owned_read", ["9"], day="2026-10-01")
        finally:
            s.close()

    def test_usage_json_counts_charged_by_kind(self):
        self.seed_charged()
        code, out, err = self.run_cli("usage", "--days", "3", "--json")
        self.assertEqual(code, 0, err)
        local = json.loads(out)["local"]
        self.assertEqual([day["charged"] for day in local], [
            {"post_read": 2, "owned_read": 0, "user_read": 1, "media_read": 3},
            {"post_read": 0, "owned_read": 1, "user_read": 0, "media_read": 0},
            {"post_read": 0, "owned_read": 0, "user_read": 0, "media_read": 0},
        ])
        self.assertEqual(self.transport.requests, [])

    def test_usage_plain_counts(self):
        self.seed_charged()
        code, out, err = self.run_cli("usage", "--days", "2")
        self.assertEqual(code, 0, err)
        self.assertIn("2026-10-02  est $0.000  reserved $0.000  abandoned 0  charged post 2 owned 0 user 1 media 3\n"
                      "2026-10-01  est $0.000  reserved $0.000  abandoned 0  charged post 0 owned 1 user 0 media 0\n",
                      out)


if __name__ == "__main__":
    unittest.main()
