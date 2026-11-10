"""pyproject.toml and LICENSE, as the spec's section 2 lists them."""
import os
import re
import tomllib
import unittest

from tests.helpers import ROOT, source_tree_only

REPO = "https://github.com/service-dept/fav"


@source_tree_only
class PyprojectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
            cls.data = tomllib.load(fh)
        cls.project = cls.data["project"]
        cls.hatch = cls.data["tool"]["hatch"]

    def test_build_system(self):
        self.assertEqual(self.data["build-system"],
                         {"requires": ["hatchling", "hatch-fancy-pypi-readme"], "build-backend": "hatchling.build"})

    def test_metadata(self):
        p = self.project
        self.assertEqual(p["name"], "fav")
        self.assertEqual(p["description"], "A read-only, spend-capped command line for X, built for agents.")
        self.assertEqual(p["dynamic"], ["version", "readme"])
        self.assertNotIn("version", p)
        self.assertNotIn("readme", p)
        self.assertEqual(p["requires-python"], ">=3.11")
        self.assertEqual(p["license"], "MIT")
        self.assertEqual(p["license-files"], ["LICENSE"])
        self.assertEqual(p.get("dependencies", []), [])
        self.assertEqual(p["keywords"], ["x", "twitter", "cli", "agents", "bookmarks"])

    def test_classifiers(self):
        classifiers = self.project["classifiers"]
        for wanted in ("Development Status :: 3 - Alpha", "Environment :: Console",
                       "Operating System :: POSIX :: Linux", "Operating System :: MacOS",
                       "Programming Language :: Python :: 3.11", "Programming Language :: Python :: 3.12",
                       "Programming Language :: Python :: 3.13", "Programming Language :: Python :: 3.14"):
            self.assertIn(wanted, classifiers)
        # A license classifier next to a license expression is rejected by PEP 639 tools.
        self.assertEqual([c for c in classifiers if c.startswith("License ::")], [])

    def test_entry_point(self):
        self.assertEqual(self.project["scripts"], {"fav": "fav.__main__:main"})
        from fav import __main__ as entry
        self.assertTrue(callable(entry.main))

    def test_urls(self):
        self.assertEqual(self.project["urls"],
                         {"Homepage": REPO, "Repository": REPO, "Issues": REPO + "/issues"})

    def test_version_source(self):
        self.assertEqual(self.hatch["version"], {"path": "fav/__init__.py"})

    def test_build_targets(self):
        targets = self.hatch["build"]["targets"]
        self.assertEqual(targets["wheel"]["include"], ["fav"])
        self.assertEqual(targets["sdist"]["include"], ["fav", "tests", "README.md", "LICENSE"])

    def test_readme_links_become_absolute(self):
        hook = self.hatch["metadata"]["hooks"]["fancy-pypi-readme"]
        self.assertEqual(hook["content-type"], "text/markdown")
        self.assertEqual(hook["fragments"], [{"path": "README.md"}])
        text = "the [license](LICENSE) and [the skill](skills/fav/SKILL.md)"
        for sub in hook["substitutions"]:
            text = re.sub(sub["pattern"], sub["replacement"], text)
        self.assertEqual(text, "the [license](%s/blob/main/LICENSE) and "
                               "[the skill](%s/blob/main/skills/fav/SKILL.md)" % (REPO, REPO))


@source_tree_only
class LicenseTest(unittest.TestCase):
    def test_mit(self):
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("MIT License\n"))
        self.assertIn("Copyright (c) 2026 Service Dept\n", text)


if __name__ == "__main__":
    unittest.main()
