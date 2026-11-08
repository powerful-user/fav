"""fav --version and python -m fav, which replace the old launcher script."""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import fav
from fav import cli
from tests.helpers import TempHome

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class VersionTest(TempHome):
    def test_value(self):
        self.assertEqual(fav.__version__, "0.1.0a1")

    def test_version_flag(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(cli.main(["--version"], env=self.env, stdout=out, stderr=err), 0)
        self.assertEqual(out.getvalue(), "fav %s\n" % fav.__version__)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(os.listdir(self.tmp), [])


class ModuleTest(unittest.TestCase):
    def run_module(self, *args):
        home = tempfile.mkdtemp(prefix="fav-home-")
        self.addCleanup(shutil.rmtree, home, True)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "PYTHONPATH": ROOT}
        proc = subprocess.run([sys.executable, "-B", "-m", "fav", *args], capture_output=True, text=True,
                              env=env, cwd="/")
        return proc, home

    def test_help(self):
        proc, home = self.run_module("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage: fav", proc.stdout)
        self.assertEqual(os.listdir(home), [])

    def test_version(self):
        proc, home = self.run_module("--version")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "fav %s\n" % fav.__version__)
        self.assertEqual(os.listdir(home), [])


if __name__ == "__main__":
    unittest.main()
