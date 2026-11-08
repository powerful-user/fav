import unittest

from tests.helpers import CONFIG_TOML, Clock, FakeTransport, TempHome, response, write_config
from tests.media_helpers import MEDIA_FIELDS, TODAY, charge, lookup
from fav import budget as bd
from fav import client as cl
from fav import config as cfg
from fav import paths
from fav import store as st
from fav.errors import XError

LOOKUP_PARAMS = {"ids": "2001", "tweet.fields": "attachments", "expansions": "attachments.media_keys",
                 "media.fields": MEDIA_FIELDS}


class MediaPriceTest(TempHome):
    def load(self, text):
        write_config(self.env, text)
        return cfg.load_config(paths.resolve_paths(self.env).config_file)

    def test_default_and_set(self):
        self.assertEqual(self.load(CONFIG_TOML).prices.media_read, 0.005)
        self.assertEqual(self.load(CONFIG_TOML + "[prices]\nmedia_read = 0\n").prices.media_read, 0.0)
        self.assertEqual(self.load(CONFIG_TOML + "[prices]\nmedia_read = 0.002\n").prices.media_read, 0.002)

    def test_rejected(self):
        for value in ("-0.001", "nan", "inf", '"free"', "true"):
            with self.subTest(value=value):
                with self.assertRaises(XError) as ctx:
                    self.load(CONFIG_TOML + "[prices]\nmedia_read = " + value + "\n")
                self.assertEqual(ctx.exception.code, "X_CONFIG")
                self.assertIn("prices.media_read", ctx.exception.message)


class MediaBudgetBase(TempHome):
    def make(self, text=CONFIG_TOML, extra=0.0):
        write_config(self.env, text)
        p = paths.resolve_paths(self.env)
        paths.ensure_dirs(p)
        self.config = cfg.load_config(p.config_file)
        self.store = st.Store(p.db_file)
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.budget = bd.Budget(self.store, self.config, invocation_id="inv", extra_usd=extra, now_fn=self.clock,
                                pid_alive=lambda pid: True)
        self.transport = FakeTransport()
        self.client = cl.XClient(self.transport, token=lambda: "tok-1", budget=self.budget, sleep=lambda s: None)

    def rows(self):
        return [dict(r) for r in self.store.conn.execute("SELECT * FROM ledger ORDER BY id")]

    def charged(self, day=TODAY):
        return set((r["kind"], r["resource_id"]) for r in self.store.conn.execute(
            "SELECT kind, resource_id FROM charged WHERE utc_day = ?", (day,)))


class BudgetMediaTest(MediaBudgetBase):
    def test_media_kind(self):
        self.make()
        self.assertIn("media_read", bd.KINDS)
        self.assertAlmostEqual(self.budget.worst_case("media_read", 2), 0.01)

    def test_allowance_is_read_only(self):
        self.make(extra=0.5)
        self.assertAlmostEqual(self.budget.allowance(), 1.5)
        self.assertEqual(self.rows(), [])
        self.budget.reserve("search", "/x", 0.30)
        self.assertAlmostEqual(self.budget.allowance(), 1.2)
        self.assertEqual(len(self.rows()), 1)

    def test_allowance_counts_used_budget(self):
        self.make(CONFIG_TOML.replace("1.00", "0.10"), extra=0.5)
        self.budget.reserve("search", "/x", 0.30)
        # 0.10 of headroom went first, then 0.20 of the 0.50 --budget.
        self.assertAlmostEqual(self.budget.allowance(), 0.30)

    def test_resource_cost_counts_only_uncharged_today(self):
        self.make()
        charge(self.store, "post_read", ["2001"])
        charge(self.store, "media_read", ["3_2"], day="2026-10-01")
        cost = self.budget.resource_cost([("post_read", "2001"), ("media_read", "3_1"), ("media_read", "3_2"),
                                          ("media_read", "3_1")])
        self.assertAlmostEqual(cost, 0.010)
        self.assertEqual(self.budget.resource_cost([("post_read", "2001")]), 0.0)
        with self.assertRaises(ValueError):
            self.budget.resource_cost([("bogus", "1")])

    def test_reserve_resources(self):
        self.make()
        charge(self.store, "post_read", ["2001"])
        row = self.budget.reserve_resources("search", "/2/tweets", [("post_read", "2001"), ("media_read", "3_1")])
        (ledger,) = self.rows()
        self.assertEqual(ledger["id"], row)
        self.assertEqual((ledger["state"], ledger["command"], ledger["endpoint"]), ("reserved", "search", "/2/tweets"))
        self.assertAlmostEqual(ledger["reserved_usd"], 0.005)

    def test_charged_resources_reserve_nothing_at_the_ceiling(self):
        self.make()
        charge(self.store, "post_read", ["2001"])
        charge(self.store, "media_read", ["3_1"])
        self.budget.reserve("search", "/x", 1.0)
        self.budget.reserve_resources("search", "/2/tweets", [("post_read", "2001"), ("media_read", "3_1")])
        self.assertEqual(self.rows()[-1]["reserved_usd"], 0.0)
        with self.assertRaises(XError) as ctx:
            self.budget.reserve_resources("search", "/2/tweets", [("media_read", "3_9")])
        self.assertEqual(ctx.exception.code, "X_BUDGET_EXCEEDED")
        self.assertEqual(len(self.rows()), 2)

    def test_reserve_resources_uses_budget_once(self):
        self.make(CONFIG_TOML.replace("1.00", "0.00"), extra=0.005)
        self.budget.reserve_resources("search", "/2/tweets", [("media_read", "3_1")])
        with self.assertRaises(XError) as ctx:
            self.budget.reserve_resources("search", "/2/tweets", [("media_read", "3_2")])
        self.assertEqual(ctx.exception.code, "X_BUDGET_EXCEEDED")


class _ChargingTransport(FakeTransport):
    """Charges resources from another connection while the request is in flight, as a concurrent process would."""

    def __init__(self, db_file, resources, *items):
        super().__init__(*items)
        self.db_file = db_file
        self.resources = resources

    def send(self, method, url, headers, body, timeout):
        other = st.Store(self.db_file)
        try:
            charge(other, self.resources[0], self.resources[1:])
        finally:
            other.close()
        return super().send(method, url, headers, body, timeout)


class ClientResourcesTest(MediaBudgetBase):
    def get(self, resources, params=None):
        return self.client.get("search", "/2/tweets", dict(params or LOOKUP_PARAMS), resources=resources)

    def test_resource_form_reserves_listed_resources(self):
        self.make()
        charge(self.store, "post_read", ["2001"])
        self.transport.push(lookup(("2001", ["3_1"])))
        payload = self.get([("post_read", "2001"), ("media_read", "3_1")])
        self.assertEqual(payload["includes"]["media"][0]["media_key"], "3_1")
        self.assertEqual(self.transport.paths(), ["/2/tweets"])
        self.assertEqual(self.transport.query(0), LOOKUP_PARAMS)
        (row,) = self.rows()
        self.assertEqual(row["state"], "settled")
        self.assertAlmostEqual(row["reserved_usd"], 0.005)
        self.assertAlmostEqual(row["est_usd"], 0.005)
        self.assertEqual(row["resources"], 2)
        self.assertEqual(self.charged(), {("post_read", "2001"), ("media_read", "3_1")})

    def test_data_is_charged_as_post_read(self):
        self.make()
        self.transport.push(lookup(("2001", ["3_1"])))
        self.get([("post_read", "2001"), ("media_read", "3_1")])
        (row,) = self.rows()
        self.assertAlmostEqual(row["reserved_usd"], 0.010)
        self.assertAlmostEqual(row["est_usd"], 0.010)
        self.assertEqual(self.charged(), {("post_read", "2001"), ("media_read", "3_1")})

    def test_resolved_attachment_returned_again_stays_within_reservation(self):
        self.make()
        self.transport.push(lookup(("2004", ["3_4a", "3_4b"])))
        self.client.get("search", "/2/tweets", dict(LOOKUP_PARAMS, ids="2004"),
                        resources=[("post_read", "2004"), ("media_read", "3_4a"), ("media_read", "3_4b")])
        (row,) = self.rows()
        self.assertAlmostEqual(row["reserved_usd"], 0.015)
        self.assertLessEqual(row["est_usd"], row["reserved_usd"] + 1e-9)
        self.assertAlmostEqual(row["est_usd"], 0.015)

    def test_resources_charged_first_elsewhere_settle_lower(self):
        self.make()
        db_file = paths.resolve_paths(self.env).db_file
        self.transport = _ChargingTransport(db_file, ("media_read", "3_1"), lookup(("2001", ["3_1"])))
        self.client = cl.XClient(self.transport, token=lambda: "tok-1", budget=self.budget, sleep=lambda s: None)
        self.get([("post_read", "2001"), ("media_read", "3_1")])
        (row,) = self.rows()
        self.assertAlmostEqual(row["reserved_usd"], 0.010)
        self.assertAlmostEqual(row["est_usd"], 0.005)

    def test_count_form_charges_included_media(self):
        self.make()
        payload = {"data": [{"id": "2001", "attachments": {"media_keys": ["3_1"]}}],
                   "includes": {"media": [{"media_key": "3_1", "type": "photo"}]}}
        self.transport.push(response(200, payload))
        self.client.get("search", "/2/tweets", {"ids": "2001"}, kind="post_read", max_resources=1)
        self.assertEqual(self.charged(), {("post_read", "2001"), ("media_read", "3_1")})
        self.assertAlmostEqual(self.rows()[0]["est_usd"], 0.010)

    def test_retry_reserves_again(self):
        self.make()
        charge(self.store, "post_read", ["2001"])
        self.transport.push(response(503, {"title": "Service Unavailable"}), lookup(("2001", ["3_1"])))
        self.get([("post_read", "2001"), ("media_read", "3_1")])
        rows = self.rows()
        self.assertEqual([r["http_status"] for r in rows], [503, 200])
        self.assertEqual([round(r["reserved_usd"], 6) for r in rows], [0.005, 0.005])

    def test_retry_over_budget_raises_budget_exceeded(self):
        self.make(CONFIG_TOML.replace("1.00", "0.00"), extra=0.005)
        charge(self.store, "post_read", ["2001"])
        self.transport.push(response(503, {"title": "Service Unavailable"}))
        with self.assertRaises(XError) as ctx:
            self.get([("post_read", "2001"), ("media_read", "3_1")])
        self.assertEqual(ctx.exception.code, "X_BUDGET_EXCEEDED")
        self.assertEqual(len(self.transport.requests), 1)
        (row,) = self.rows()
        self.assertEqual((row["state"], row["http_status"]), ("settled", 503))

    def test_forms_are_exclusive(self):
        self.make()
        with self.assertRaises(ValueError):
            self.client.get("search", "/2/tweets", LOOKUP_PARAMS, kind="post_read", max_resources=1,
                            resources=[("post_read", "2001")])
        with self.assertRaises(ValueError):
            self.client.get("search", "/2/tweets", LOOKUP_PARAMS)
        with self.assertRaises(ValueError):
            self.client.get("search", "/2/tweets", LOOKUP_PARAMS, kind="post_read")
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
