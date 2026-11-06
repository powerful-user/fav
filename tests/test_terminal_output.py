import json
import unittest
import unicodedata
from datetime import datetime, timezone

from tests.helpers import response
from tests.test_cli import CliBase
from wings_x import output

NOW = datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc)


def controls(text):
    """Control characters other than newline and tab: what a terminal would act on."""
    return [c for c in text if unicodedata.category(c) == "Cc" and c not in "\n\t"]


# Unicode's bidirectional formatting characters: they reorder how a terminal shows the text around them.
BIDI = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"


def bidi(text):
    return [c for c in text if c in BIDI]


class TerminalSafeTest(unittest.TestCase):
    def test_controls_are_shown_escaped(self):
        self.assertEqual(output.terminal_safe("a\x1b[2Jb\x07c\x7fd\x9be\tf\ng\r\nh\ri"),
                         "a\\x1b[2Jb\\x07c\\x7fd\\x9be\tf\ng\nh\\x0di")

    def test_bidi_formatting_is_shown_escaped(self):
        self.assertEqual(output.terminal_safe("a" + BIDI + "b"),
                         "a" + "".join("\\u" + format(ord(c), "04x") for c in BIDI) + "b")

    def test_plain_output_has_no_bidi_formatting(self):
        post = {"id": "123", "url": "https://x.com/ex/status/123", "text": "pay \u202egnp.exe \u05e9\u05dc\u05d5\u05dd",
                "author": {"id": "42", "username": "ex", "display_name": "Ex\u2067ample\u2069"},
                "created_at": "2026-10-02T12:00:00Z", "origin": "live", "source": "x", "unavailable": None,
                "media": [{"key": "3_1", "type": "photo", "url": "https://pbs.twimg.com/m.jpg",
                           "alt_text": "alt \u200f text \u0645\u0631\u062d\u0628\u0627"}]}
        text = output.render_plain({"items": [post]}, NOW)
        self.assertEqual(bidi(text), [])
        self.assertIn("pay \\u202egnp.exe \u05e9\u05dc\u05d5\u05dd", text)
        self.assertIn("alt: \"alt \\u200f text \u0645\u0631\u062d\u0628\u0627\"", text)

    def test_json_output_has_no_bidi_formatting_and_same_value(self):
        result = {"items": [{"text": "a" + BIDI + "b \u05e9\u05dc\u05d5\u05dd", "author": {"display_name": "\u202eé\u2069"},
                             "k\u202ey" + BIDI: "\u0645\u0631\u062d\u0628\u0627"}]}
        text = output.render_json(result)
        self.assertEqual(bidi(text), [])
        self.assertEqual(json.loads(text), result)
        self.assertIn("\u05e9\u05dc\u05d5\u05dd", text)
        self.assertIn("\u0645\u0631\u062d\u0628\u0627", text)

    def test_plain_output_has_no_controls(self):
        post = {"id": "123", "url": "https://x.com/ex/status/123\x1b[0m", "text": "body\x1b]52;c;aGk=\x07\nline two",
                "author": {"id": "42", "username": "ex\x1bample", "display_name": "Ex\x9bample"},
                "created_at": "2026-10-02T12:00:00Z", "origin": "live", "source": "x", "unavailable": None,
                "media": [{"key": "3_1", "type": "photo", "url": "https://pbs.twimg.com/m.jpg\x1b[1A",
                           "alt_text": "alt \x1b[2J text"}],
                "referenced": [{"type": "quoted", "id": "9", "url": "https://x.com/o\x1b/status/9"}]}
        text = output.render_plain({"items": [post], "summary": ["note \x1b[31m"], "notes": ["x\x07"]}, NOW)
        self.assertEqual(controls(text), [])
        self.assertIn("alt: \"alt \\x1b[2J text\"", text)
        self.assertIn("body\\x1b]52;c;aGk=\\x07\nline two", text)

    def test_json_output_has_no_controls_and_same_value(self):
        result = {"items": [{"text": "a\x1bb\x7fc\x9bd\ne", "author": {"display_name": "é\x85"}}]}
        text = output.render_json(result)
        self.assertEqual(controls(text.rstrip("\n")), [])
        self.assertEqual(json.loads(text), result)


class CliTerminalTest(CliBase):
    def test_plain_error_has_no_controls(self):
        self.transport.push(response(400, {"title": "Invalid Request", "detail": "bad \x1b[2J query"}))
        code, out, err = self.run_cli("search", "agents")
        self.assertEqual(code, 1)
        self.assertEqual(controls(err), [])
        self.assertIn("bad \\x1b[2J query", err)

    def test_json_error_has_no_controls(self):
        self.transport.push(response(400, {"title": "Invalid Request", "detail": "bad \x9b2J query"}))
        code, out, _ = self.run_cli("search", "agents", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(controls(out.rstrip("\n")), [])
        self.assertIn("bad \x9b2J query", json.loads(out)["error"]["message"])

    def test_plain_error_has_no_bidi_formatting(self):
        self.transport.push(response(400, {"title": "Invalid Request", "detail": "bad \u202e query \u05e9\u05dc\u05d5\u05dd"}))
        code, out, err = self.run_cli("search", "agents")
        self.assertEqual(code, 1)
        self.assertEqual(bidi(err), [])
        self.assertIn("bad \\u202e query \u05e9\u05dc\u05d5\u05dd", err)

    def test_json_error_has_no_bidi_formatting(self):
        self.transport.push(response(400, {"title": "Invalid Request", "detail": "bad \u2066 query"}))
        code, out, _ = self.run_cli("search", "agents", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(bidi(out), [])
        self.assertIn("bad \u2066 query", json.loads(out)["error"]["message"])

    def test_prebuilt_plain_has_no_bidi_formatting(self):
        self.transport.push(response(200, {"data": {"id": "7", "username": "me\u202e\u0645\u0631\u062d\u0628\u0627"}}))
        code, out, _ = self.run_cli("auth", "status")
        self.assertEqual(code, 0)
        self.assertEqual(bidi(out), [])
        self.assertIn("@me\\u202e\u0645\u0631\u062d\u0628\u0627 (id 7)", out)

    def test_prebuilt_plain_has_no_controls(self):
        self.transport.push(response(200, {"data": {"id": "7", "username": "me\x1b[2J"}}))
        code, out, _ = self.run_cli("auth", "status")
        self.assertEqual(code, 0)
        self.assertEqual(controls(out), [])
        self.assertIn("@me\\x1b[2J (id 7)", out)


if __name__ == "__main__":
    unittest.main()
