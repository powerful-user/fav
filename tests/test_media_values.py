import unittest
from datetime import datetime, timezone

from wings_x import media, output

NOW = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)


class MediaNumbersTest(unittest.TestCase):
    def test_out_of_range_numbers_are_dropped(self):
        raw = {"media_key": "13_1", "type": "video", "width": -10, "height": 0, "duration_ms": -5,
               "variants": [{"content_type": "video/mp4", "url": "https://v/1.mp4", "bit_rate": -1}]}
        self.assertEqual(media.normalize_media(raw),
                         {"key": "13_1", "type": "video",
                          "variants": [{"content_type": "video/mp4", "url": "https://v/1.mp4"}]})

    def test_zero_length_and_bit_rate_are_kept(self):
        raw = {"media_key": "13_1", "type": "video", "width": 1, "height": 1, "duration_ms": 0,
               "variants": [{"content_type": "video/mp4", "url": "https://v/1.mp4", "bit_rate": 0}]}
        self.assertEqual(media.normalize_media(raw),
                         {"key": "13_1", "type": "video", "width": 1, "height": 1, "duration_ms": 0,
                          "variants": [{"content_type": "video/mp4", "url": "https://v/1.mp4", "bit_rate": 0}]})

    def test_plain_output_skips_a_stored_size_out_of_range(self):
        post = {"id": "123", "url": "https://x.com/ex/status/123", "text": "body",
                "author": {"id": "42", "username": "ex"}, "created_at": "2026-10-02T12:00:00Z",
                "origin": "live", "source": "x", "unavailable": None,
                "media": [{"key": "3_1", "type": "photo", "width": -10, "height": 20, "url": "https://p/1.jpg"},
                          {"key": "3_2", "type": "photo", "width": 0, "height": 20, "url": "https://p/2.jpg"}]}
        text = output.render_plain({"items": [post]}, NOW)
        self.assertIn("\n  ▣ photo https://p/1.jpg\n", text)
        self.assertIn("\n  ▣ photo https://p/2.jpg\n", text)


if __name__ == "__main__":
    unittest.main()
