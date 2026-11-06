import math
import os
from datetime import datetime, timedelta, timezone

from wings_x.errors import XError
from wings_x.parse import iso_z

KINDS = ("post_read", "owned_read", "user_read", "media_read")
STALE_AFTER = timedelta(minutes=10)
_EPSILON = 1e-9


def _pid_alive(pid):
    """True when pid is a live process. Signal 0 checks without sending a signal."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _as_utc(moment):
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _finite_amount(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise XError("X_BAD_INPUT", label + " must be a finite non-negative amount")
    try:
        number = float(value)
    except OverflowError:
        raise XError("X_BAD_INPUT", label + " must be a finite non-negative amount") from None
    if not math.isfinite(number) or number < 0:
        raise XError("X_BAD_INPUT", label + " must be a finite non-negative amount")
    return number


def _dollars(amount):
    return "$" + format(amount, ".2f")


def _dedupe(charges):
    seen = set()
    unique = []
    for charge in charges:
        if (
            not isinstance(charge, tuple)
            or len(charge) != 2
            or not isinstance(charge[0], str)
            or not isinstance(charge[1], str)
        ):
            raise XError("X_BAD_INPUT", "charge is invalid")
        if charge in seen:
            continue
        seen.add(charge)
        unique.append(charge)
    return unique


class Budget:
    def __init__(self, store, config, *, invocation_id, extra_usd=0.0, now_fn, pid=None, pid_alive=None):
        self.store = store
        self.config = config
        self.invocation_id = invocation_id
        self.extra_usd = _finite_amount(extra_usd, "budget")
        self._extra_used = 0.0
        self.now_fn = now_fn
        self.pid = os.getpid() if pid is None else pid
        self.pid_alive = pid_alive if pid_alive is not None else _pid_alive

    def _price(self, kind):
        if kind not in KINDS:
            raise ValueError(kind)
        return getattr(self.config.prices, kind)

    def worst_case(self, kind, count):
        price = self._price(kind)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise XError("X_BAD_INPUT", "resource count must be a non-negative whole number")
        try:
            cost = price * count
        except OverflowError:
            raise XError("X_BAD_INPUT", "resource count is out of range") from None
        if not math.isfinite(cost):
            raise XError("X_BAD_INPUT", "resource count is out of range")
        return cost

    def _stamp(self):
        try:
            now = self.now_fn()
            at = iso_z(now)
            current = _as_utc(now)
        except (TypeError, ValueError, OverflowError, AttributeError):
            raise XError("X_UPSTREAM", "clock returned an invalid time") from None
        return current, at

    def _committed(self, day):
        row = self.store.conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE
                    WHEN state = 'settled' THEN est_usd
                    WHEN state IN ('reserved', 'abandoned') THEN reserved_usd
                    ELSE 0
                END
            ), 0)
            FROM ledger
            WHERE substr(at, 1, 10) = ?
            """,
            (day,),
        ).fetchone()
        try:
            total = float(row[0])
        except (TypeError, ValueError, OverflowError):
            raise XError("X_UPSTREAM", "ledger total is invalid") from None
        if not math.isfinite(total) or total < 0:
            raise XError("X_UPSTREAM", "ledger total is invalid")
        return total

    def _is_stale(self, row, now):
        at_text = row["at"]
        try:
            if not isinstance(at_text, str):
                raise ValueError(at_text)
            at = _as_utc(datetime.fromisoformat(at_text.replace("Z", "+00:00")))
        except (TypeError, ValueError, OverflowError):
            return True
        if now - at > STALE_AFTER:
            return True
        pid = row["pid"]
        if pid == self.pid:
            return False
        try:
            alive = self.pid_alive(pid)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return not alive

    def _abandon_stale(self, now):
        rows = self.store.conn.execute(
            "SELECT id, pid, at FROM ledger WHERE state = 'reserved'"
        ).fetchall()
        for row in rows:
            if not self._is_stale(row, now):
                continue
            self.store.conn.execute(
                "UPDATE ledger SET state = 'abandoned' WHERE id = ? AND state = 'reserved'",
                (row["id"],),
            )

    def _uncharged_cost(self, unique, day):
        cost = 0.0
        for kind, resource_id in unique:
            price = self._price(kind)
            found = self.store.conn.execute(
                "SELECT 1 FROM charged WHERE utc_day = ? AND kind = ? AND resource_id = ?",
                (day, kind, resource_id),
            ).fetchone()
            if found is None:
                cost += price
        if not math.isfinite(cost) or cost < 0:
            raise XError("X_BAD_INPUT", "resource cost is out of range")
        return cost

    def resource_cost(self, resources):
        """The price of the listed resources not yet charged today. Read-only: no transaction, no write."""
        unique = _dedupe(resources)
        _now, at = self._stamp()
        return self._uncharged_cost(unique, at[:10])

    def allowance(self):
        """Today's headroom under the ceiling plus unused --budget, as reserve computes it. Read-only."""
        _now, at = self._stamp()
        committed = self._committed(at[:10])
        headroom = max(0.0, self.config.daily_ceiling_usd - committed)
        return headroom + max(0.0, self.extra_usd - self._extra_used)

    def reserve(self, command, endpoint, worst_usd):
        worst = _finite_amount(worst_usd, "worst case")
        return self._reserve(command, endpoint, lambda day: worst)

    def reserve_resources(self, command, endpoint, resources):
        """Reserve the uncharged cost of the listed resources on the reservation's UTC day."""
        unique = _dedupe(resources)
        for kind, _resource_id in unique:
            self._price(kind)
        return self._reserve(command, endpoint, lambda day: self._uncharged_cost(unique, day))

    def _reserve(self, command, endpoint, worst_for_day):
        extra_delta = 0.0
        with self.store.transaction(immediate=True):
            # The clock, abandon and total are read only after the write lock, so a wait cannot date the row early.
            now, at = self._stamp()
            day = at[:10]
            self._abandon_stale(now)
            worst = worst_for_day(day)
            committed = self._committed(day)
            headroom = max(0.0, self.config.daily_ceiling_usd - committed)
            unused_extra = max(0.0, self.extra_usd - self._extra_used)
            allowance = headroom + unused_extra
            if worst > allowance + _EPSILON:
                raise XError(
                    "X_BUDGET_EXCEEDED",
                    "allowance "
                    + _dollars(allowance)
                    + " is below this request's worst case "
                    + _dollars(worst)
                    + "; pass --budget to raise it",
                )
            cursor = self.store.conn.execute(
                """
                INSERT INTO ledger (
                    invocation_id, pid, at, command, endpoint, state, reserved_usd, est_usd
                ) VALUES (?, ?, ?, ?, ?, 'reserved', ?, 0)
                """,
                (self.invocation_id, self.pid, at, command, endpoint, worst),
            )
            row_id = cursor.lastrowid
            if worst > headroom:
                extra_delta = worst - headroom
        self._extra_used += extra_delta
        return row_id

    def settle(self, row_id, *, charges, http_status, duration_ms, result_count, x_request_id=None, error_code=None):
        unique = _dedupe(charges)
        with self.store.transaction(immediate=True):
            row = self.store.conn.execute("SELECT at FROM ledger WHERE id = ?", (row_id,)).fetchone()
            if row is None or not isinstance(row["at"], str) or len(row["at"]) < 10:
                raise XError("X_UPSTREAM", "ledger row is missing")
            # Accounting day is the reservation's UTC day, not the time of settlement.
            day = row["at"][:10]
            est = 0.0
            for kind, resource_id in unique:
                price = self._price(kind)
                found = self.store.conn.execute(
                    "SELECT 1 FROM charged WHERE utc_day = ? AND kind = ? AND resource_id = ?",
                    (day, kind, resource_id),
                ).fetchone()
                if found is not None:
                    continue
                self.store.conn.execute(
                    "INSERT INTO charged (utc_day, kind, resource_id) VALUES (?, ?, ?)",
                    (day, kind, resource_id),
                )
                est += price
            if not math.isfinite(est) or est < 0:
                raise XError("X_UPSTREAM", "settled cost is invalid")
            self.store.conn.execute(
                """
                UPDATE ledger
                SET state = 'settled',
                    est_usd = ?,
                    resources = ?,
                    duration_ms = ?,
                    result_count = ?,
                    x_request_id = ?,
                    http_status = ?,
                    error_code = ?
                WHERE id = ?
                """,
                (est, len(unique), duration_ms, result_count, x_request_id, http_status, error_code, row_id),
            )
        return est

    def record_exempt(self, command, endpoint, *, http_status, duration_ms, error_code=None):
        with self.store.transaction(immediate=True):
            _at_now, at = self._stamp()
            cursor = self.store.conn.execute(
                """
                INSERT INTO ledger (
                    invocation_id, pid, at, command, endpoint, state,
                    reserved_usd, est_usd, duration_ms, http_status, error_code
                ) VALUES (?, ?, ?, ?, ?, 'settled', 0, 0, ?, ?, ?)
                """,
                (self.invocation_id, self.pid, at, command, endpoint, duration_ms, http_status, error_code),
            )
            return cursor.lastrowid

    def abandon_stale(self):
        """Mark reservations of dead processes, or older than ten minutes, abandoned."""
        with self.store.transaction(immediate=True):
            now, _at = self._stamp()
            self._abandon_stale(now)

    def committed_today(self):
        _now, at = self._stamp()
        return self._committed(at[:10])

    def spent(self):
        row = self.store.conn.execute(
            """
            SELECT COALESCE(SUM(est_usd), 0) FROM ledger
            WHERE invocation_id = ? AND state = 'settled'
            """,
            (self.invocation_id,),
        ).fetchone()
        try:
            total = float(row[0])
        except (TypeError, ValueError, OverflowError):
            raise XError("X_UPSTREAM", "ledger total is invalid") from None
        if not math.isfinite(total) or total < 0:
            raise XError("X_UPSTREAM", "ledger total is invalid")
        return total
