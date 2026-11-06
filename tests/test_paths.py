import dataclasses
import errno
import os
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock

from tests.helpers import TempHome
from fav import paths
from fav.errors import CODES, EXIT_ERROR, EXIT_OK, EXIT_USAGE, XError, local_io_error


class PathsTest(TempHome):
    def test_xdg_paths(self):
        p = paths.resolve_paths(self.env)
        self.assertEqual(p.config_dir, os.path.join(self.env["XDG_CONFIG_HOME"], "fav"))
        self.assertEqual(p.state_dir, os.path.join(self.env["XDG_STATE_HOME"], "fav"))
        self.assertEqual([f.name for f in dataclasses.fields(p)], ["config_dir", "state_dir"])
        self.assertEqual(p.config_file, os.path.join(p.config_dir, "config.toml"))
        self.assertEqual(p.token_file, os.path.join(p.config_dir, "token.json"))
        self.assertEqual(p.app_bearer_file, os.path.join(p.config_dir, "app-bearer"))
        self.assertEqual(p.token_lock, os.path.join(p.config_dir, "token.lock"))
        self.assertEqual(p.db_file, os.path.join(p.state_dir, "x.sqlite3"))
        self.assertEqual(p.sync_lock, os.path.join(p.state_dir, "sync.lock"))

    def test_home_fallback(self):
        p = paths.resolve_paths({"HOME": self.tmp})
        self.assertEqual(p.config_dir, os.path.join(self.tmp, ".config", "fav"))
        self.assertEqual(p.state_dir, os.path.join(self.tmp, ".local", "state", "fav"))

    def test_empty_xdg_value_falls_back(self):
        p = paths.resolve_paths({"HOME": self.tmp, "XDG_CONFIG_HOME": ""})
        self.assertEqual(p.config_dir, os.path.join(self.tmp, ".config", "fav"))

    def test_missing_home_is_an_error(self):
        with self.assertRaises(XError) as ctx:
            paths.resolve_paths({})
        self.assertEqual(ctx.exception.code, "X_CONFIG")

    def test_ensure_dirs_0700(self):
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        for directory in (p.config_dir, p.state_dir):
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)

    def test_ensure_dirs_tightens_existing(self):
        p = paths.resolve_paths(self.env)
        os.makedirs(p.config_dir)
        os.chmod(p.config_dir, 0o755)
        paths.ensure_dirs(p)
        self.assertEqual(stat.S_IMODE(os.stat(p.config_dir).st_mode), 0o700)

    def test_ensure_dirs_failure_is_local_io(self):
        with open(self.env["XDG_STATE_HOME"], "w"):
            pass
        p = paths.resolve_paths(self.env)
        with self.assertRaises(XError) as ctx:
            paths.ensure_dirs(p)
        self.assertEqual(ctx.exception.code, "X_LOCAL_IO")
        self.assertIn(p.state_dir, ctx.exception.message)

    def test_atomic_write_0600(self):
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        target = os.path.join(p.config_dir, "secret")
        paths.atomic_write(target, b"one")
        paths.atomic_write(target, b"two")
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"two")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(p.config_dir)), ["secret"])

    def test_atomic_write_completes_short_writes(self):
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        target = os.path.join(p.config_dir, "secret")
        payload = b"secret-token"
        real_write = os.write

        def one_byte(fd, data):
            return real_write(fd, bytes(data)[:1])

        with mock.patch("fav.paths.os.write", one_byte):
            paths.atomic_write(target, payload)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), payload)
        self.assertEqual(sorted(os.listdir(p.config_dir)), ["secret"])

    def test_atomic_write_fails_when_write_makes_no_progress(self):
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        target = os.path.join(p.config_dir, "secret")
        paths.atomic_write(target, b"keep")

        def no_progress(fd, data):
            return 0

        with mock.patch("fav.paths.os.write", no_progress):
            with self.assertRaises(OSError):
                paths.atomic_write(target, b"secret-token")
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"keep")
        self.assertEqual(sorted(os.listdir(p.config_dir)), ["secret"])

    def test_chmod_failure_removes_temp_and_closes_fd(self):
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        target = os.path.join(p.config_dir, "secret")
        paths.atomic_write(target, b"keep")
        opened = []
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            fd, tmp_path = real_mkstemp(*args, **kwargs)
            opened.append(fd)
            return fd, tmp_path

        def boom(path, mode):
            raise OSError("chmod failed")

        with mock.patch("fav.paths.tempfile.mkstemp", spy_mkstemp):
            with mock.patch("fav.paths.os.chmod", boom):
                with self.assertRaises(OSError):
                    paths.atomic_write(target, b"new")
        self.assertEqual(sorted(os.listdir(p.config_dir)), ["secret"])
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"keep")
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError) as ctx:
            os.fstat(opened[0])
        self.assertEqual(ctx.exception.errno, errno.EBADF)


class ErrorsTest(unittest.TestCase):
    def test_json_shape(self):
        e = XError("X_RATE_LIMITED", "slow down", retry_at="2026-10-02T12:15:00Z")
        self.assertEqual(
            e.to_json(),
            {"error": {"code": "X_RATE_LIMITED", "message": "slow down", "retry_at": "2026-10-02T12:15:00Z"}},
        )
        self.assertEqual(str(e), "X_RATE_LIMITED: slow down")

    def test_json_without_retry(self):
        e = XError("X_BAD_INPUT", "nope")
        self.assertEqual(e.to_json(), {"error": {"code": "X_BAD_INPUT", "message": "nope"}})

    def test_codes(self):
        self.assertEqual(len(CODES), 15)
        for code in ["X_CONFIG", "X_ENVIRONMENT", "X_AUTH_REQUIRED", "X_AUTH_EXPIRED", "X_SCOPE_MISMATCH",
                     "X_API_ACCESS_REQUIRED", "X_CREDITS_EXHAUSTED", "X_BUDGET_EXCEEDED", "X_RATE_LIMITED",
                     "X_NOT_FOUND", "X_PROTECTED", "X_BAD_INPUT", "X_QUERY_INVALID", "X_UPSTREAM", "X_LOCAL_IO"]:
            self.assertIn(code, CODES)

    def test_local_io_error_message(self):
        e = local_io_error("write", "/x/token.json", PermissionError(13, "Permission denied"))
        self.assertEqual(e.code, "X_LOCAL_IO")
        self.assertEqual(e.message, "cannot write /x/token.json: Permission denied")
        named = sqlite3.OperationalError("sentinel-schema")
        named.sqlite_errorname = "SQLITE_CORRUPT"
        e = local_io_error("open", "/x/x.sqlite3", named)
        self.assertEqual(e.message, "cannot open /x/x.sqlite3: SQLITE_CORRUPT")
        self.assertNotIn("sentinel-schema", e.message)
        e = local_io_error("open", "/x/x.sqlite3", sqlite3.OperationalError("sentinel-schema"))
        self.assertEqual(e.message, "cannot open /x/x.sqlite3: OperationalError")
        self.assertNotIn("sentinel-schema", e.message)
        e = local_io_error("delete", "/x/token.json", OSError())
        self.assertEqual(e.message, "cannot delete /x/token.json: OSError")

    def test_unknown_code_rejected(self):
        with self.assertRaises(ValueError):
            XError("X_MADE_UP", "x")

    def test_exit_codes(self):
        self.assertEqual((EXIT_OK, EXIT_ERROR, EXIT_USAGE), (0, 1, 2))


if __name__ == "__main__":
    unittest.main()
