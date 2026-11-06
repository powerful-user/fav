import multiprocessing
import threading
import unittest
from datetime import datetime

from tests.helpers import Clock, TempHome, write_config
from wings_x import budget as bd
from wings_x import config as cfg
from wings_x import paths
from wings_x import store as st
from wings_x.errors import XError


def _process_worker(env, inv, barrier, results):
    """Runs in a separate process: one reservation of $0.30 against the shared database."""
    p = paths.resolve_paths(env)
    config = cfg.load_config(p.config_file)
    s = st.Store(p.db_file)
    try:
        b = bd.Budget(s, config, invocation_id=inv, now_fn=Clock(), pid_alive=lambda pid: True)
        barrier.wait(20)
        try:
            b.reserve("search", "/s", 0.30)
            results.put(True)
        except XError:
            results.put(False)
    finally:
        s.close()


class BudgetTest(TempHome):
    def setUp(self):
        super().setUp()
        write_config(self.env)
        self.paths = paths.resolve_paths(self.env)
        paths.ensure_dirs(self.paths)
        self.config = cfg.load_config(self.paths.config_file)
        self.clock = Clock()
        self.store = st.Store(self.paths.db_file)
        self.addCleanup(self.store.close)

    def budget(self, store=None, extra=0.0, inv="inv-1", pid_alive=None):
        return bd.Budget(store or self.store, self.config, invocation_id=inv, extra_usd=extra, now_fn=self.clock,
                         pid_alive=pid_alive or (lambda pid: True))

    def rows(self):
        return [dict(r) for r in self.store.conn.execute("SELECT * FROM ledger ORDER BY id")]

    def spend(self, b, usd):
        """Reserve and settle a known cost made of distinct user reads."""
        n = int(round(usd / 0.010))
        row = b.reserve("test", "/x", usd)
        charges = [("user_read", "u" + str(i) + "-" + str(row)) for i in range(n)]
        b.settle(row, charges=charges, http_status=200, duration_ms=1, result_count=n)

    def test_worst_case(self):
        b = self.budget()
        self.assertAlmostEqual(b.worst_case("post_read", 25), 0.125)
        self.assertAlmostEqual(b.worst_case("owned_read", 100), 0.10)
        self.assertAlmostEqual(b.worst_case("user_read", 3), 0.03)
        with self.assertRaises(ValueError):
            b.worst_case("bogus", 1)

    def test_worst_case_count_is_a_whole_number(self):
        b = self.budget()
        for count in (2.5, 25.0, True, -1, 10 ** 400):
            with self.subTest(count=count):
                with self.assertRaises(XError) as ctx:
                    b.worst_case("post_read", count)
                self.assertEqual(ctx.exception.code, "X_BAD_INPUT")

    def test_reserve_writes_row(self):
        b = self.budget()
        row = b.reserve("search", "/2/tweets/search/recent", 0.125)
        (r,) = self.rows()
        self.assertEqual(r["id"], row)
        self.assertEqual(r["state"], "reserved")
        self.assertAlmostEqual(r["reserved_usd"], 0.125)
        self.assertEqual(r["invocation_id"], "inv-1")
        self.assertEqual(r["command"], "search")
        self.assertEqual(r["endpoint"], "/2/tweets/search/recent")
        self.assertEqual(r["at"], "2026-10-02T12:00:00Z")
        self.assertIsNotNone(r["pid"])
        self.assertAlmostEqual(b.committed_today(), 0.125)

    def test_ceiling_is_inclusive(self):
        b = self.budget()
        b.reserve("search", "/s", 1.00)
        with self.assertRaises(XError):
            b.reserve("search", "/s", 0.001)

    def test_refuse_over_ceiling(self):
        b = self.budget()
        self.spend(b, 0.95)
        with self.assertRaises(XError) as ctx:
            b.reserve("search", "/2/tweets/search/recent", 0.10)
        self.assertEqual(ctx.exception.code, "X_BUDGET_EXCEEDED")
        self.assertIn("$0.05", ctx.exception.message)
        self.assertIn("$0.10", ctx.exception.message)
        self.assertIn("--budget", ctx.exception.message)
        self.assertEqual(len(self.rows()), 1)

    def test_settle_dedups_by_utc_day(self):
        b = self.budget()
        row = b.reserve("post", "/2/tweets", 0.02)
        est = b.settle(row, charges=[("post_read", "1"), ("post_read", "1"), ("post_read", "2")], http_status=200,
                       duration_ms=5, result_count=2, x_request_id="req-1")
        self.assertAlmostEqual(est, 0.010)
        r = self.rows()[0]
        self.assertEqual(r["state"], "settled")
        self.assertAlmostEqual(r["est_usd"], 0.010)
        self.assertEqual(r["resources"], 2)
        self.assertEqual(r["http_status"], 200)
        self.assertEqual(r["duration_ms"], 5)
        self.assertEqual(r["result_count"], 2)
        self.assertEqual(r["x_request_id"], "req-1")
        row2 = b.reserve("post", "/2/tweets", 0.02)
        self.assertAlmostEqual(b.settle(row2, charges=[("post_read", "1")], http_status=200, duration_ms=5,
                                        result_count=1), 0.0)
        self.clock.advance(hours=13)
        row3 = b.reserve("post", "/2/tweets", 0.02)
        self.assertAlmostEqual(b.settle(row3, charges=[("post_read", "1")], http_status=200, duration_ms=5,
                                        result_count=1), 0.005)
        self.assertAlmostEqual(b.committed_today(), 0.005)

    def test_kinds_charged_separately(self):
        b = self.budget()
        row = b.reserve("post", "/2/tweets", 0.02)
        est = b.settle(row, charges=[("post_read", "1"), ("user_read", "1")], http_status=200, duration_ms=1,
                       result_count=1)
        self.assertAlmostEqual(est, 0.015)

    def test_settle_error_costs_nothing(self):
        b = self.budget()
        row = b.reserve("search", "/s", 0.10)
        est = b.settle(row, charges=[], http_status=503, duration_ms=3, result_count=0, error_code="X_UPSTREAM")
        self.assertEqual(est, 0.0)
        r = self.rows()[0]
        self.assertEqual(r["error_code"], "X_UPSTREAM")
        self.assertEqual(r["state"], "settled")
        self.assertAlmostEqual(b.committed_today(), 0.0)

    def test_extra_budget_per_invocation(self):
        self.spend(self.budget(inv="other"), 0.95)
        b = self.budget(extra=0.20)
        b.reserve("search", "/s", 0.10)
        b.reserve("search", "/s", 0.10)
        with self.assertRaises(XError):
            b.reserve("search", "/s", 0.10)

    def test_extra_not_shared_across_invocations(self):
        self.spend(self.budget(inv="other"), 0.95)
        a = self.budget(extra=0.20, inv="a")
        a.reserve("search", "/s", 0.20)
        b = self.budget(inv="b")
        with self.assertRaises(XError):
            b.reserve("search", "/s", 0.01)

    def test_dead_process_reservation_abandoned_and_counted(self):
        dead = self.budget(inv="dead")
        row = dead.reserve("search", "/s", 0.30)
        self.store.conn.execute("UPDATE ledger SET pid = 4242 WHERE id = ?", (row,))
        live = self.budget(inv="live", pid_alive=lambda pid: pid != 4242)
        live.reserve("search", "/s", 0.10)
        self.assertEqual([r["state"] for r in self.rows()], ["abandoned", "reserved"])
        self.assertAlmostEqual(live.committed_today(), 0.40)

    def test_old_reservation_abandoned(self):
        b = self.budget()
        b.reserve("search", "/s", 0.10)
        self.clock.advance(minutes=11)
        b.reserve("search", "/s", 0.10)
        self.assertEqual([r["state"] for r in self.rows()], ["abandoned", "reserved"])
        self.assertAlmostEqual(b.committed_today(), 0.20)

    def test_exempt_rows(self):
        b = self.budget()
        b.record_exempt("auth status", "/2/users/me", http_status=200, duration_ms=4)
        (r,) = self.rows()
        self.assertEqual(r["state"], "settled")
        self.assertEqual(r["est_usd"], 0.0)
        self.assertEqual(r["reserved_usd"], 0.0)
        self.assertEqual(r["endpoint"], "/2/users/me")
        self.assertAlmostEqual(b.committed_today(), 0.0)

    def test_spent_is_per_invocation(self):
        a = self.budget(inv="a")
        self.spend(a, 0.10)
        b = self.budget(inv="b")
        self.spend(b, 0.20)
        self.assertAlmostEqual(a.spent(), 0.10)
        self.assertAlmostEqual(b.spent(), 0.20)

    def test_concurrent_reservations_cannot_pass_ceiling(self):
        results = []
        guard = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(i):
            s = st.Store(self.paths.db_file)
            try:
                b = self.budget(store=s, inv="t" + str(i))
                barrier.wait()
                try:
                    b.reserve("search", "/s", 0.15)
                    ok = True
                except XError:
                    ok = False
                with guard:
                    results.append(ok)
            finally:
                s.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 6)
        self.assertEqual(results.count(False), 2)

    def test_processes_racing_cannot_pass_ceiling(self):
        mp = multiprocessing.get_context("spawn")
        barrier = mp.Barrier(4)
        results = mp.Queue()
        procs = [mp.Process(target=_process_worker, args=(self.env, "p" + str(i), barrier, results)) for i in range(4)]
        for proc in procs:
            proc.start()
        outcomes = [results.get(timeout=30) for _ in procs]
        for proc in procs:
            proc.join(30)
            self.assertEqual(proc.exitcode, 0)
        self.assertEqual(outcomes.count(True), 3)
        self.assertEqual(outcomes.count(False), 1)

    def test_clock_read_under_the_write_lock(self):
        # A writer waiting on BEGIN IMMEDIATE must not stamp the time it began waiting.
        seen = []

        def clock():
            seen.append(self.store.conn.in_transaction)
            return self.clock()

        b = bd.Budget(self.store, self.config, invocation_id="inv-1", now_fn=clock, pid_alive=lambda pid: True)
        b.reserve("test", "/x", 0.01)
        b.record_exempt("test", "/x", http_status=200, duration_ms=1)
        b.abandon_stale()
        self.assertEqual(seen, [True, True, True])

    def test_settlement_uses_reservation_day(self):
        self.clock.now = datetime.fromisoformat("2026-10-02T23:59:50+00:00")
        b = self.budget()
        row = b.reserve("post", "/2/tweets", 0.02)
        self.clock.advance(seconds=20)
        est = b.settle(row, charges=[("post_read", "1")], http_status=200, duration_ms=1, result_count=1)
        self.assertAlmostEqual(est, 0.005)
        self.assertEqual([r[0] for r in self.store.conn.execute("SELECT utc_day FROM charged")], ["2026-10-02"])
        self.assertAlmostEqual(b.committed_today(), 0.0)
        row2 = b.reserve("post", "/2/tweets", 0.02)
        est2 = b.settle(row2, charges=[("post_read", "1")], http_status=200, duration_ms=1, result_count=1)
        self.assertAlmostEqual(est2, 0.005)
        self.assertAlmostEqual(b.committed_today(), 0.005)


if __name__ == "__main__":
    unittest.main()
