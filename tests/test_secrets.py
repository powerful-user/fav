import os
import subprocess
import unittest

from tests.helpers import SCOPE, page, raw_post, response, users_response, write_token
from tests.test_cli import CliBase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECRETS = ["SECRET-ACCESS-AAAA", "SECRET-REFRESH-BBBB", "SECRET-APP-CCCC", "SECRET-ACCESS-DDDD",
           "SECRET-REFRESH-EEEE"]


class SecretsTest(CliBase):
    def test_no_secret_leaks(self):
        write_token(self.env, access=SECRETS[0], refresh=SECRETS[1], expires_at="2026-10-02T11:00:00Z")
        new_token = {"token_type": "bearer", "expires_in": 7200, "access_token": SECRETS[3], "scope": SCOPE,
                     "refresh_token": SECRETS[4]}
        outputs = []

        def run(*argv, **kwargs):
            code, out, err = self.run_cli(*argv, **kwargs)
            outputs.extend([out, err])
            return code

        self.transport.push(response(200, new_token), page([raw_post("1")]), users_response(("42", "example")))
        self.assertEqual(run("bookmarks", "sync"), 0)
        self.transport.push(response(404, {"title": "Not Found Error", "detail": "Could not find tweet"}))
        self.assertEqual(run("post", "5"), 1)
        self.transport.push(response(500), response(500))
        self.assertEqual(run("search", "agents"), 1)
        self.assertEqual(run("auth", "app-token", getpass_fn=lambda prompt: SECRETS[2]), 0)
        self.transport.push(response(401, {"title": "Unauthorized"}))
        self.assertEqual(run("usage", "--json"), 0)
        self.transport.push(response(200, {"data": {"id": "7", "username": "me"}}))
        self.assertEqual(run("auth", "status", "--json"), 0)
        self.transport.push(response(400, {"error": "invalid_grant"}))
        write_token(self.env, access=SECRETS[3], refresh=SECRETS[4], expires_at="2026-10-02T11:00:00Z")
        self.assertEqual(run("search", "agents"), 1)
        # The refresh itself was sent and rejected; a search sent with the expired token would end here too.
        self.assertEqual(self.transport.requests[-1]["url"], "https://api.x.com/2/oauth2/token")
        self.assertIn("X_AUTH_EXPIRED", outputs[-1])

        state = os.path.join(self.env["XDG_STATE_HOME"], "wings", "x")
        blobs = []
        for dirpath, _dirs, files in os.walk(state):
            for name in files:
                with open(os.path.join(dirpath, name), "rb") as fh:
                    blobs.append(fh.read())
        self.assertGreater(len(blobs), 0)
        blobs.extend(o.encode("utf-8") for o in outputs)
        for secret in SECRETS:
            for blob in blobs:
                self.assertNotIn(secret.encode("ascii"), blob)
        for request in self.transport.requests:
            if request["url"].startswith("https://api.x.com/2/usage/"):
                continue
            self.assertNotEqual(request["headers"].get("Authorization"), "Bearer " + SECRETS[2])


class LiveSmokeTest(unittest.TestCase):
    def test_skips_without_flag(self):
        env = dict((k, v) for k, v in os.environ.items() if k not in ("WINGS_X_LIVE", "BASH_ENV", "ENV"))
        proc = subprocess.run(["bash", os.path.join(ROOT, "tests", "live_smoke.sh")], capture_output=True, text=True,
                              env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("live smoke skipped", proc.stdout)


if __name__ == "__main__":
    unittest.main()
