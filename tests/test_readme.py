"""README.md, as the spec's section 4 lists its parts."""
import os
import re
import tomllib
import unittest

from tests.helpers import OLD_NAME, ROOT, source_tree_only

HEADINGS = ["Install", "Set up your X app", "Commands", "Spending", "With an agent", "Files", "Development",
            "License"]


@source_tree_only
class ReadmeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            cls.text = fh.read()

    def part(self, heading):
        start = self.text.index("\n## %s\n" % heading)
        rest = self.text[start + 1:]
        end = rest.find("\n## ")
        return rest if end < 0 else rest[:end]

    def test_parts_in_order(self):
        self.assertTrue(self.text.startswith("# fav\n"))
        self.assertEqual(re.findall(r"^## (.+)$", self.text, re.M), HEADINGS)

    def test_intro(self):
        intro = self.text[:self.text.index("\n## ")]
        self.assertIn("not affiliated with X", intro)

    def test_install(self):
        part = self.part("Install")
        for line in ("pipx install fav", "uv tool install fav", "brew install service-dept/tap/fav", "3.11",
                     "FTS5", "X_ENVIRONMENT"):
            self.assertIn(line, part)

    def test_x_app_setup(self):
        part = self.part("Set up your X app")
        for line in ("http://127.0.0.1:8765/callback", "~/.config/fav/config.toml", "client_id", "fav auth login",
                     "tweet.read", "users.read", "bookmark.read", "offline.access", "fav auth app-token",
                     "spending limit"):
            self.assertIn(line, part)
        # A new user writes config.toml before running fav, so nothing has made the directory yet.
        self.assertIn("mkdir -p -m 700 ~/.config/fav", part)
        self.assertIn("$XDG_CONFIG_HOME/fav", part)
        # Login prints the authorization URL; it does not open a browser itself.
        self.assertNotIn("opens a browser", self.text)
        self.assertNotIn("login in a browser", self.text)

    def test_commands_and_spending(self):
        for line in ("--json", "--budget", "last sync"):
            self.assertIn(line, self.part("Commands"))
        for line in ("daily_ceiling_usd", "--budget", "config.toml"):
            self.assertIn(line, self.part("Spending"))

    def test_with_an_agent(self):
        part = self.part("With an agent")
        self.assertIn("git clone --depth 1 --branch v", part)
        self.assertIn("https://github.com/service-dept/fav", part)
        self.assertIn("skills/fav", part)

    def test_files_and_development(self):
        part = self.part("Files")
        for line in ("~/.config/fav", "~/.local/state/fav", "0600"):
            self.assertIn(line, part)
        # fav never changes the mode of the config.toml the user writes.
        self.assertNotIn("The files inside are mode", part)
        part = self.part("Development")
        self.assertIn("python -B -m unittest discover -s tests -t .", part)
        self.assertIn("FAV_LIVE=1 tests/live_smoke.sh", part)

    def test_license(self):
        part = self.part("License")
        self.assertIn("MIT", part)
        self.assertIn("fav by Service Dept.", part)

    def test_no_old_name(self):
        self.assertNotIn(OLD_NAME, self.text.lower())

    def test_relative_links_exist_and_become_absolute_on_pypi(self):
        links = re.findall(r"\]\(([^)\s]+)\)", self.text)
        relative = [link for link in links if not re.match(r"[a-z]+:|#", link)]
        self.assertGreater(len(relative), 0)
        for link in relative:
            self.assertTrue(os.path.exists(os.path.join(ROOT, link.split("#")[0])), link)
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
            hook = tomllib.load(fh)["tool"]["hatch"]["metadata"]["hooks"]["fancy-pypi-readme"]
        text = self.text
        for sub in hook["substitutions"]:
            text = re.sub(sub["pattern"], sub["replacement"], text)
        left = [link for link in re.findall(r"\]\(([^)\s]+)\)", text) if not re.match(r"[a-z]+:|#", link)]
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main()
