import os
import sys
import unittest

from tests.helpers import TempHome, write_config
from fav import config as cfg
from fav.errors import XError


class _NoFtsConn:
    def execute(self, sql):
        raise _NoFtsSqlite.OperationalError("no such module: fts5")

    def close(self):
        pass


class _NoFtsSqlite:
    class OperationalError(Exception):
        pass

    @staticmethod
    def connect(name):
        return _NoFtsConn()


class ConfigTest(TempHome):
    def test_defaults(self):
        c = cfg.load_config(write_config(self.env))
        self.assertEqual(c.client_id, "test-client")
        self.assertEqual(c.redirect_uri, "http://127.0.0.1:8765/callback")
        self.assertEqual(c.daily_ceiling_usd, 1.0)
        self.assertEqual(c.search_limit_default, 25)
        self.assertEqual(c.thread_limit_default, 50)
        self.assertEqual(c.folder_pages_max, 10)
        self.assertEqual(cfg.Config.SEARCH_LIMIT_MAX, 100)
        self.assertEqual(c.prices.post_read, 0.005)
        self.assertEqual(c.prices.owned_read, 0.001)
        self.assertEqual(c.prices.user_read, 0.010)

    def test_overrides(self):
        path = write_config(
            self.env,
            'client_id = "c"\ndaily_ceiling_usd = 2.5\nsearch_limit_default = 10\nfolder_pages_max = 2\n'
            "[prices]\npost_read = 0.004\n",
        )
        c = cfg.load_config(path)
        self.assertEqual(c.daily_ceiling_usd, 2.5)
        self.assertEqual(c.search_limit_default, 10)
        self.assertEqual(c.folder_pages_max, 2)
        self.assertEqual(c.prices.post_read, 0.004)
        self.assertEqual(c.prices.user_read, 0.010)
        self.assertEqual(c.redirect_uri, "http://127.0.0.1:8765/callback")

    def test_integer_ceiling_accepted(self):
        c = cfg.load_config(write_config(self.env, 'client_id = "c"\ndaily_ceiling_usd = 2\n'))
        self.assertEqual(c.daily_ceiling_usd, 2.0)

    def assertConfigError(self, text, field):
        path = write_config(self.env, text)
        with self.assertRaises(XError) as ctx:
            cfg.load_config(path)
        self.assertEqual(ctx.exception.code, "X_CONFIG")
        self.assertIn(field, ctx.exception.message)

    def test_missing_file(self):
        with self.assertRaises(XError) as ctx:
            cfg.load_config(self.tmp + "/nope.toml")
        self.assertEqual(ctx.exception.code, "X_CONFIG")
        self.assertIn("nope.toml", ctx.exception.message)

    def test_unreadable_file(self):
        path = self.tmp + "/dir.toml"
        os.mkdir(path)
        with self.assertRaises(XError) as ctx:
            cfg.load_config(path)
        self.assertEqual(ctx.exception.code, "X_CONFIG")
        self.assertIn("dir.toml", ctx.exception.message)

    def test_missing_client_id(self):
        self.assertConfigError("daily_ceiling_usd = 1.0\n", "client_id")

    def test_empty_client_id(self):
        self.assertConfigError('client_id = ""\n', "client_id")

    def test_bad_type(self):
        self.assertConfigError('client_id = "c"\ndaily_ceiling_usd = "lots"\n', "daily_ceiling_usd")

    def test_bool_is_not_a_number(self):
        self.assertConfigError('client_id = "c"\ndaily_ceiling_usd = true\n', "daily_ceiling_usd")

    def test_negative_price(self):
        self.assertConfigError('client_id = "c"\n[prices]\nuser_read = -1\n', "prices.user_read")

    def test_non_finite_numbers_rejected(self):
        cases = [
            ('client_id = "c"\ndaily_ceiling_usd = nan\n', "daily_ceiling_usd"),
            ('client_id = "c"\ndaily_ceiling_usd = inf\n', "daily_ceiling_usd"),
            ('client_id = "c"\ndaily_ceiling_usd = -inf\n', "daily_ceiling_usd"),
            ('client_id = "c"\n[prices]\npost_read = nan\n', "prices.post_read"),
            ('client_id = "c"\n[prices]\npost_read = inf\n', "prices.post_read"),
            ('client_id = "c"\n[prices]\npost_read = -inf\n', "prices.post_read"),
            ('client_id = "c"\n[prices]\nowned_read = nan\n', "prices.owned_read"),
            ('client_id = "c"\n[prices]\nowned_read = inf\n', "prices.owned_read"),
            ('client_id = "c"\n[prices]\nowned_read = -inf\n', "prices.owned_read"),
            ('client_id = "c"\n[prices]\nuser_read = nan\n', "prices.user_read"),
            ('client_id = "c"\n[prices]\nuser_read = inf\n', "prices.user_read"),
            ('client_id = "c"\n[prices]\nuser_read = -inf\n', "prices.user_read"),
        ]
        for text, field in cases:
            self.assertConfigError(text, field)

    def test_oversized_numbers_rejected(self):
        huge = "1" + "0" * 309
        for sign in ("", "-"):
            self.assertConfigError('client_id = "c"\ndaily_ceiling_usd = ' + sign + huge + "\n", "daily_ceiling_usd")
            for key in ("post_read", "owned_read", "user_read"):
                self.assertConfigError('client_id = "c"\n[prices]\n' + key + " = " + sign + huge + "\n", "prices." + key)

    def test_unparseable_integer_is_config_error(self):
        # Pin Python's default limit so PYTHONINTMAXSTRDIGITS cannot make the value parseable.
        self.addCleanup(sys.set_int_max_str_digits, sys.get_int_max_str_digits())
        sys.set_int_max_str_digits(4300)
        for key in ("daily_ceiling_usd", "search_limit_default"):
            for sign in ("", "-"):
                self.assertConfigError('client_id = "c"\n' + key + " = " + sign + "9" * 4301 + "\n", "config.toml")

    def test_limit_out_of_range(self):
        self.assertConfigError('client_id = "c"\nsearch_limit_default = 500\n', "search_limit_default")
        self.assertConfigError('client_id = "c"\nsearch_limit_default = 9\n', "search_limit_default")
        self.assertConfigError('client_id = "c"\nthread_limit_default = 0\n', "thread_limit_default")

    def test_invalid_toml(self):
        self.assertConfigError("client_id = \n", "config.toml")

    def test_unknown_key(self):
        self.assertConfigError('client_id = "c"\nclient_secret = "s"\n', "client_secret")

    def test_unknown_price(self):
        self.assertConfigError('client_id = "c"\n[prices]\nfree_read = 0\n', "prices.free_read")


class EnvironmentTest(unittest.TestCase):
    def test_current_ok(self):
        cfg.check_environment()

    def test_old_python(self):
        with self.assertRaises(XError) as ctx:
            cfg.check_environment(version_info=(3, 10, 12))
        self.assertEqual(ctx.exception.code, "X_ENVIRONMENT")
        self.assertIn("3.11", ctx.exception.message)

    def test_no_fts5(self):
        with self.assertRaises(XError) as ctx:
            cfg.check_environment(sqlite_module=_NoFtsSqlite())
        self.assertEqual(ctx.exception.code, "X_ENVIRONMENT")
        self.assertIn("FTS5", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
