import json
import unittest

from tests.helpers import fixture
from wings_x import normalize

EXAMPLE = {"id": "42", "username": "example", "display_name": "Example"}
OTHER = {"id": "44", "username": "other", "display_name": "Other"}


class NormalizeTest(unittest.TestCase):
    def test_search_item(self):
        out = normalize.normalize_response(fixture("search_recent.json"), {"42": EXAMPLE}, "live")
        a = out["items"][0]
        self.assertEqual(a["id"], "1001")
        self.assertEqual(a["url"], "https://x.com/example/status/1001")
        self.assertEqual(a["text"], "Agent-first languages are coming")
        self.assertEqual(a["author"], EXAMPLE)
        self.assertEqual(a["created_at"], "2026-10-01T10:00:00Z")
        self.assertEqual(a["conversation_id"], "1001")
        self.assertEqual(a["lang"], "en")
        self.assertEqual(a["metrics"], {"likes": 5, "reposts": 1, "replies": 2, "quotes": 0})
        self.assertEqual(a["referenced"], [{"type": "quoted", "id": "99", "url": "https://x.com/i/status/99"}])
        self.assertEqual(a["origin"], "live")
        self.assertEqual(a["source"], "x")
        self.assertIsNone(a["unavailable"])
        self.assertNotIn("bookmark", a)
        self.assertNotIn("media", a)
        self.assertEqual(out["next"], "tok2")
        self.assertEqual(out["included"], [])
        self.assertEqual(out["unavailable"], {})

    def test_unresolved_author_and_media(self):
        out = normalize.normalize_response(fixture("search_recent.json"), {"42": EXAMPLE}, "live")
        b = out["items"][1]
        self.assertEqual(b["author"], {"id": "43"})
        self.assertEqual(b["url"], "https://x.com/i/status/1002")
        self.assertEqual(b["media"], [{"key": "3_1"}])

    def test_post_with_refs(self):
        users = {"42": EXAMPLE, "44": OTHER}
        out = normalize.normalize_response(fixture("post_with_refs.json"), users, "live")
        (item,) = out["items"]
        self.assertEqual(
            item["referenced"],
            [
                {"type": "quoted", "id": "99", "url": "https://x.com/other/status/99"},
                {"type": "replied_to", "id": "98", "url": "https://x.com/i/status/98", "unavailable": "deleted"},
            ],
        )
        self.assertEqual([p["id"] for p in out["included"]], ["99"])
        self.assertEqual(out["included"][0]["url"], "https://x.com/other/status/99")
        self.assertEqual(out["included"][0]["origin"], "live")
        self.assertEqual(out["unavailable"], {"98": "deleted"})
        self.assertIsNone(out["next"])

    def test_minimal_post(self):
        item = normalize.normalize_post({"id": "5", "text": "t"}, {}, "bookmark")
        self.assertEqual(item, {"id": "5", "url": "https://x.com/i/status/5", "text": "t", "origin": "bookmark",
                                "source": "x", "unavailable": None})

    def test_empty_response(self):
        out = normalize.normalize_response({"meta": {"result_count": 0}}, {}, "live")
        self.assertEqual(out, {"items": [], "included": [], "unavailable": {}, "next": None})

    def test_errors_only(self):
        payload = {"errors": [{"title": "Authorization Error", "resource_id": "77",
                               "detail": "Sorry, you are not authorized to see the Post with id: [77]."}]}
        out = normalize.normalize_response(payload, {}, "live")
        self.assertEqual(out["items"], [])
        self.assertEqual(out["unavailable"], {"77": "protected"})

    def test_author_ids(self):
        self.assertEqual(normalize.author_ids(fixture("post_with_refs.json")), ["42", "44"])
        self.assertEqual(normalize.author_ids(fixture("search_recent.json")), ["42", "43"])
        self.assertEqual(normalize.author_ids({"meta": {}}), [])

    def test_bad_origin(self):
        with self.assertRaises(ValueError):
            normalize.normalize_post({"id": "5"}, {}, "elsewhere")

    def test_renamed_shape_is_equivalent(self):
        users = {"42": EXAMPLE, "44": OTHER}
        for name in ("search_recent.json", "post_with_refs.json"):
            legacy = fixture(name)
            text = json.dumps(legacy)
            text = text.replace('"referenced_tweets"', '"referenced_posts"').replace('"retweet_count"', '"repost_count"')
            text = text.replace('"tweets":', '"posts":')
            renamed = json.loads(text)
            self.assertNotEqual(renamed, legacy, name)
            self.assertEqual(normalize.normalize_response(renamed, users, "live"),
                             normalize.normalize_response(legacy, users, "live"), name)
            self.assertEqual(normalize.author_ids(renamed), normalize.author_ids(legacy), name)


if __name__ == "__main__":
    unittest.main()
