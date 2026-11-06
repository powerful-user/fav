"""tests.yml and publish.yml, as the spec's section 2 lists them."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import fav
from tests.helpers import ROOT, source_tree_only

SUITE = "python -B -m unittest discover -s tests -t ."


def read(name):
    with open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8") as fh:
        return fh.read()


@source_tree_only
class TestsWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("tests.yml")

    def test_triggers(self):
        self.assertIn("pull_request:", self.text)
        self.assertIn("branches: [main]", self.text)

    def test_unit_matrix(self):
        for system in ("ubuntu-latest", "macos-latest"):
            self.assertIn(system, self.text)
        for version in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn('"%s"' % version, self.text)
        self.assertIn("run: " + SUITE, self.text)

    def test_installed_packages(self):
        self.assertIn('"*.whl"', self.text)
        self.assertIn('"*.tar.gz"', self.text)
        self.assertIn("$RUNNER_TEMP/suite", self.text)
        # Run from the repository root, the copied suite would import the checkout's fav.
        self.assertEqual(self.text.count("working-directory: ${{ runner.temp }}/suite\n"), 2)
        self.assertIn("assert fav.__file__.startswith(sys.prefix), fav.__file__", self.text)
        self.assertLess(self.text.index("assert fav.__file__.startswith(sys.prefix)"),
                        self.text.index("$RUNNER_TEMP/venv/bin/" + SUITE))
        self.assertIn("$RUNNER_TEMP/venv/bin/" + SUITE, self.text)
        self.assertIn("$RUNNER_TEMP/venv/bin/fav --version", self.text)

    def test_no_pytest(self):
        self.assertNotIn("pytest", self.text)


@source_tree_only
class PublishWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read("publish.yml")

    def test_trusted_publishing(self):
        for line in ("types: [published]", "needs: test", "needs: build", "environment: pypi",
                     "id-token: write", "uses: pypa/gh-action-pypi-publish@release/v1"):
            self.assertIn(line, self.text)

    def test_suite_runs_after_tag_check(self):
        self.assertIn("run: " + SUITE, self.text)
        self.assertNotIn("pytest", self.text)
        self.assertLess(self.text.index("name: Tag matches version"), self.text.index("run: " + SUITE))
        self.assertIn("TAG: ${{ github.event.release.tag_name }}", self.text)

    def tag_check(self, tag):
        match = re.search(r'^\s*run: (test "\$TAG" = .*)$', self.text, re.M)
        self.assertIsNotNone(match, "no tag check")
        # The workflow runs `python` from setup-python; point that name at this interpreter.
        shim = tempfile.mkdtemp(prefix="fav-shim-")
        self.addCleanup(shutil.rmtree, shim, True)
        os.symlink(sys.executable, os.path.join(shim, "python"))
        env = {"PATH": shim + os.pathsep + os.environ.get("PATH", ""), "TAG": tag}
        return subprocess.run(["bash", "-c", match.group(1)], cwd=ROOT, env=env).returncode

    def test_tag_check_passes_on_match(self):
        self.assertEqual(self.tag_check("v" + fav.__version__), 0)

    def test_tag_check_fails_on_mismatch(self):
        self.assertNotEqual(self.tag_check("v" + fav.__version__ + "9"), 0)
        self.assertNotEqual(self.tag_check(fav.__version__), 0)


if __name__ == "__main__":
    unittest.main()
