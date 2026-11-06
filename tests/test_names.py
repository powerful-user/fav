"""fav names itself everywhere: no leftover names, hints, paths or User-Agents."""
import io
import os
import unittest

import fav
from fav import auth, cli, client
from fav import config as cfg
from fav import paths
from tests.helpers import OLD_NAME, Clock, FakeTransport, TempHome, response, write_config, write_token

PACKAGE = os.path.dirname(os.path.abspath(fav.__file__))
TESTS = os.path.dirname(os.path.abspath(__file__))


def files_under(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


class OldNameTest(unittest.TestCase):
    def test_no_old_name_in_package_or_tests(self):
        needle = OLD_NAME.encode("ascii")
        hits = []
        for root in (PACKAGE, TESTS):
            for path in files_under(root):
                with open(path, "rb") as fh:
                    for number, line in enumerate(fh, 1):
                        if needle in line.lower():
                            hits.append("%s:%d" % (os.path.relpath(path, os.path.dirname(root)), number))
        self.assertEqual(hits, [])


class HintTest(TempHome):
    def test_login_hint(self):
        self.assertEqual(auth.LOGIN_HINT, "run fav auth login")

    def test_help_names_fav(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(cli.main(["--help"], env=self.env, stdout=out, stderr=err), 0)
        self.assertTrue(out.getvalue().startswith("usage: fav "), out.getvalue())
        self.assertIn("Read-only access to X for agents.", out.getvalue())


class UserAgentTest(TempHome):
    def test_client_user_agent(self):
        self.assertEqual(client.USER_AGENT, "fav/" + fav.__version__)

    def test_token_requests_user_agent(self):
        config = cfg.load_config(write_config(self.env))
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        write_token(self.env)
        transport = FakeTransport(response(200, {"revoked": True}))
        auth.Auth(p, config, transport, now_fn=Clock()).logout()
        self.assertEqual(transport.requests[0]["headers"]["User-Agent"], "fav/" + fav.__version__)


if __name__ == "__main__":
    unittest.main()
