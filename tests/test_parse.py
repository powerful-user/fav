import sys
import unittest
from datetime import datetime, timedelta, timezone

from fav import parse
from fav.errors import XError

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class PostRefTest(unittest.TestCase):
    def test_forms(self):
        for text in [
            "1234567890",
            " 1234567890 ",
            "https://x.com/example/status/1234567890",
            "https://twitter.com/example/status/1234567890?s=20",
            "x.com/example/status/1234567890",
            "https://mobile.twitter.com/example/status/1234567890/photo/1",
            "https://x.com/i/status/1234567890",
            "https://www.x.com/example/status/1234567890",
        ]:
            self.assertEqual(parse.parse_post_ref(text), "1234567890", text)

    def test_bad(self):
        for text in ["", "abc", "https://example.com/a/status/1", "https://x.com/example", "12a",
                     "https://x.com/example/status/"]:
            with self.assertRaises(XError) as ctx:
                parse.parse_post_ref(text)
            self.assertEqual(ctx.exception.code, "X_BAD_INPUT", text)

    def test_urlsplit_error_is_bad_input(self):
        text = "https://[x.com/a/status/1"
        with self.assertRaises(XError) as ctx:
            parse.parse_post_ref(text)
        self.assertEqual(ctx.exception.code, "X_BAD_INPUT")
        self.assertEqual(ctx.exception.message, "unrecognized input: " + text)


class UserRefTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse.parse_user_ref("@Example_1"), ("username", "Example_1"))
        self.assertEqual(parse.parse_user_ref("Example_1"), ("username", "Example_1"))
        self.assertEqual(parse.parse_user_ref("https://x.com/Example_1"), ("username", "Example_1"))
        self.assertEqual(parse.parse_user_ref("@123"), ("username", "123"))
        self.assertEqual(parse.parse_user_ref("42"), ("id", "42"))

    def test_bad(self):
        for text in ["", "@", "@has space", "@" + "a" * 16, "@bad-dash"]:
            with self.assertRaises(XError) as ctx:
                parse.parse_user_ref(text)
            self.assertEqual(ctx.exception.code, "X_BAD_INPUT", text)

    def test_urlsplit_error_is_bad_input(self):
        for text in ("https://[x.com/a", "https://x.com/[a/status/1"):
            with self.assertRaises(XError) as ctx:
                parse.parse_user_ref(text)
            self.assertEqual(ctx.exception.code, "X_BAD_INPUT")
            self.assertEqual(ctx.exception.message, "unrecognized input: " + text)


class TimeTest(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(parse.parse_since("3d", NOW), NOW - timedelta(days=3))
        self.assertEqual(parse.parse_since("12h", NOW), NOW - timedelta(hours=12))
        self.assertEqual(parse.parse_since("30m", NOW), NOW - timedelta(minutes=30))

    def test_dates(self):
        self.assertEqual(parse.parse_since("2026-09-28", NOW), datetime(2026, 9, 28, tzinfo=timezone.utc))
        self.assertEqual(parse.parse_date("2026-09-28T06:30:00Z"), datetime(2026, 9, 28, 6, 30, tzinfo=timezone.utc))
        self.assertEqual(parse.parse_date("2026-09-28"), datetime(2026, 9, 28, tzinfo=timezone.utc))

    def test_bad(self):
        for text in ["", "3w", "yesterday", "2026-13-01", "-3d"]:
            with self.assertRaises(XError) as ctx:
                parse.parse_since(text, NOW)
            self.assertEqual(ctx.exception.code, "X_BAD_INPUT", text)

    def test_overflow_is_bad_input(self):
        # Pin Python's default limit so the 4301-digit counts reach int()'s limit under any PYTHONINTMAXSTRDIGITS.
        self.addCleanup(sys.set_int_max_str_digits, sys.get_int_max_str_digits())
        sys.set_int_max_str_digits(4300)
        for text in ("999999999999d", "999999999999h", "999999999999m", "9" * 4301 + "d", "9" * 4301 + "h", "9" * 4301 + "m"):
            with self.assertRaises(XError) as ctx:
                parse.parse_since(text, NOW)
            self.assertEqual(ctx.exception.code, "X_BAD_INPUT")
            self.assertEqual(ctx.exception.message, "unrecognized input: " + text)
        text = "0001-01-01T00:00:00+01:00"
        with self.assertRaises(XError) as ctx:
            parse.parse_date(text)
        self.assertEqual(ctx.exception.code, "X_BAD_INPUT")
        self.assertEqual(ctx.exception.message, "unrecognized input: " + text)
        with self.assertRaises(XError) as ctx:
            parse.iso_z(parse.parse_date(text))
        self.assertEqual(ctx.exception.code, "X_BAD_INPUT")
        self.assertEqual(ctx.exception.message, "unrecognized input: " + text)

    def test_iso_z(self):
        self.assertEqual(parse.iso_z(datetime(2026, 10, 2, 12, 0, 5, 123000, tzinfo=timezone.utc)), "2026-10-02T12:00:05Z")
        eastern = timezone(timedelta(hours=-4))
        self.assertEqual(parse.iso_z(datetime(2026, 10, 2, 8, 0, tzinfo=eastern)), "2026-10-02T12:00:00Z")


class UrlTest(unittest.TestCase):
    def test_post_url(self):
        self.assertEqual(parse.post_url("99", "other"), "https://x.com/other/status/99")
        self.assertEqual(parse.post_url("99", None), "https://x.com/i/status/99")


if __name__ == "__main__":
    unittest.main()
