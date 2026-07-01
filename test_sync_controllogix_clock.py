#!/usr/bin/env python3
"""
Unit tests for sync_controllogix_clock.py.

These tests use a fake, in-process pylogix ``PLC`` so they require neither a
real controller nor the pylogix library to be installed. The fake mirrors the
behavior of pylogix 1.1.5:

  * GetPLCTime(raw=True)  -> Response whose .Value is integer microseconds
                             since the 1970 UTC epoch.
  * GetPLCTime()          -> Response whose .Value is a UTC-naive datetime.
  * SetPLCTime(dst=None)  -> sets the controller clock to "now" (UTC).
  * .Status is the string "Success" on success, or a descriptive error string.

Run:
    python3 -m unittest -v test_sync_controllogix_clock
    # or, if pytest is available:
    python3 -m pytest -v test_sync_controllogix_clock.py
"""

from __future__ import annotations

import logging
import socket
import unittest
from datetime import datetime, timedelta, timezone
from struct import pack, unpack_from
from types import SimpleNamespace
from unittest import mock

import sync_controllogix_clock as mod


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeResponse:
    """Mimics pylogix.lgx_response.Response (.TagName, .Value, .Status)."""

    def __init__(self, value, status="Success"):
        self.TagName = None
        self.Value = value
        self.Status = status

    def __repr__(self):
        return f"FakeResponse(Value={self.Value!r}, Status={self.Status!r})"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_fake_plc(
    offset_seconds=0.0,
    support_raw=True,
    set_status="Success",
    set_applies=True,
    set_raises=None,
    get_fail_times=0,
    get_fail_mode="status",  # "status" -> bad Status; "exception" -> raise socket err
):
    """
    Build a fake pylogix ``PLC`` class.

    ``offset_seconds`` is how far the controller clock leads (positive) or lags
    (negative) the server's UTC clock. State is shared across instances (a new
    instance is created per connection attempt) so retry behavior can be tested.
    """
    state = {
        "get_calls": 0,
        "set_calls": 0,
        "instances": 0,
        "get_fail_times": get_fail_times,
        "get_fail_mode": get_fail_mode,
        "last": None,
    }

    class FakePLC:
        state = None  # set below

        def __init__(self):
            state["instances"] += 1
            state["last"] = self
            self.IPAddress = None
            self.ProcessorSlot = None
            self.SocketTimeout = None
            self._offset = timedelta(seconds=offset_seconds)

        # context-manager protocol used by ``with PLC() as comm:``
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def GetPLCTime(self, raw=False):
            state["get_calls"] += 1
            if not support_raw and raw:
                raise TypeError(
                    "GetPLCTime() got an unexpected keyword argument 'raw'"
                )
            if state["get_calls"] <= state["get_fail_times"]:
                if state["get_fail_mode"] == "exception":
                    raise socket.error("simulated network error")
                return FakeResponse(None, status="Connection failure")

            plc_dt = _utcnow() + self._offset
            if raw:
                micros = int(round((plc_dt - mod.EPOCH).total_seconds() * 1_000_000))
                return FakeResponse(micros, status="Success")
            return FakeResponse(plc_dt, status="Success")

        def SetPLCTime(self, dst=None):
            state["set_calls"] += 1
            if set_raises is not None:
                raise set_raises
            if set_applies:
                self._offset = timedelta(0)
            return FakeResponse(int((_utcnow() - mod.EPOCH).total_seconds() * 1e6), status=set_status)

    FakePLC.state = state
    return FakePLC


def make_tz_fake(tz_hours=-7.0, start_skew_s=10.0):
    """
    Build a fake PLC that models the real bug: an internal UTC clock (attr 11)
    and a configured time-zone offset, where the local attribute (attr 6) equals
    UTC + tz_offset. Writing attr 6 sets local (so UTC = written - offset);
    writing attr 11 sets UTC directly. Stock SetPLCTime writes server-UTC into
    attr 6, which corrupts the UTC clock by exactly the zone offset.
    """
    tz_off_us = int(tz_hours * 3_600_000_000)

    class TzFakePLC:
        def __init__(self):
            self.IPAddress = None
            self.ProcessorSlot = None
            self.SocketTimeout = None
            # Start the controller's UTC clock skewed so a sync is triggered.
            self.t_utc_us = int((_utcnow() - mod.EPOCH).total_seconds() * 1e6) + int(start_skew_s * 1e6)
            self.writes = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def _attr_us(self, attr):
            return self.t_utc_us if attr == mod.ATTR_CURRENT else self.t_utc_us + tz_off_us

        def GetPLCTime(self, raw=False):
            if raw:
                return FakeResponse(self.t_utc_us, "Success")
            return FakeResponse(mod.EPOCH + timedelta(microseconds=self.t_utc_us))

        def SetPLCTime(self, dst=None):
            self.writes += 1
            # Stock write: server UTC lands in the LOCAL attribute (the bug).
            self.t_utc_us = int((_utcnow() - mod.EPOCH).total_seconds() * 1e6) - tz_off_us
            return FakeResponse(None, "Success")

        def Message(self, service, cls, inst, attr=None, data=b""):
            if service == mod.SVC_GET_ATTR_LIST:
                val = self._attr_us(attr[0])
                return FakeResponse(b"\x00" * mod.PAYLOAD_OFFSET + pack("<Q", val), "Success")
            if service == mod.SVC_SET_ATTR_LIST:
                self.writes += 1
                for a, v in zip(attr, data):
                    if a == mod.ATTR_CURRENT:
                        self.t_utc_us = unpack_from("<Q", v, 0)[0]
                    elif a == mod.ATTR_LOCAL:
                        self.t_utc_us = unpack_from("<Q", v, 0)[0] - tz_off_us
                    # attr 0x0A (DST) is accepted and ignored by the fake.
                return FakeResponse(None, "Success")
            raise AssertionError(f"unexpected CIP service 0x{service:02x}")

    return TzFakePLC


def make_args(**overrides):
    base = dict(
        ip="192.168.1.10",
        slot=0,
        threshold_ms=1000.0,
        verify_tolerance_ms=1000.0,
        socket_timeout=5.0,
        max_retries=2,
        retry_delay=0.0,
        dry_run=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# Base test case (quiets logging, patches import_pylogix per-test)
# --------------------------------------------------------------------------- #
class Base(unittest.TestCase):
    def setUp(self):
        # Keep test output clean but leave the logger usable for assertLogs.
        mod.log.handlers[:] = [logging.NullHandler()]
        mod.log.setLevel(logging.CRITICAL)
        mod.log.propagate = False

    def run_with(self, fake_plc, args):
        with mock.patch.object(mod, "import_pylogix", return_value=fake_plc):
            return mod.run(args)


# --------------------------------------------------------------------------- #
# Offset measurement
# --------------------------------------------------------------------------- #
class TestMeasureOffset(Base):
    def test_offset_positive_when_controller_ahead(self):
        comm = make_fake_plc(offset_seconds=2.0)()
        offset_ms, latency_ms, _ = mod.measure_offset(comm)
        self.assertAlmostEqual(offset_ms, 2000.0, delta=200.0)
        self.assertGreaterEqual(latency_ms, 0.0)

    def test_offset_negative_when_controller_behind(self):
        comm = make_fake_plc(offset_seconds=-2.0)()
        offset_ms, _, _ = mod.measure_offset(comm)
        self.assertAlmostEqual(offset_ms, -2000.0, delta=200.0)

    def test_datetime_fallback_when_raw_unsupported(self):
        # support_raw=False forces the GetPLCTime(raw=True) TypeError path.
        comm = make_fake_plc(offset_seconds=1.0, support_raw=False)()
        offset_ms, _, _ = mod.measure_offset(comm)
        self.assertAlmostEqual(offset_ms, 1000.0, delta=200.0)

    def test_read_bad_status_raises_commserror(self):
        comm = make_fake_plc(get_fail_times=99, get_fail_mode="status")()
        with self.assertRaises(mod.CommsError):
            mod.measure_offset(comm)

    def test_read_socket_error_raises_commserror(self):
        comm = make_fake_plc(get_fail_times=99, get_fail_mode="exception")()
        with self.assertRaises(mod.CommsError):
            mod.measure_offset(comm)

    def test_whole_hour_offset_emits_warning(self):
        comm = make_fake_plc(offset_seconds=3600.0)()
        mod.log.setLevel(logging.WARNING)
        with self.assertLogs(mod.log, level="WARNING") as cm:
            mod.measure_offset(comm)
        self.assertTrue(any("whole number of hours" in m for m in cm.output))


# --------------------------------------------------------------------------- #
# Single sync cycle (sync_once)
# --------------------------------------------------------------------------- #
class TestSyncOnce(Base):
    def test_within_threshold_does_not_set(self):
        fake = make_fake_plc(offset_seconds=0.2)  # 200 ms < 1000 ms threshold
        comm = fake()
        rc = mod.sync_once(comm, threshold_ms=1000.0, verify_tol_ms=1000.0, dry_run=False)
        self.assertEqual(rc, mod.EXIT_OK)
        self.assertEqual(fake.state["set_calls"], 0)

    def test_beyond_threshold_sets_and_verifies(self):
        fake = make_fake_plc(offset_seconds=5.0, set_applies=True)
        comm = fake()
        rc = mod.sync_once(comm, threshold_ms=1000.0, verify_tol_ms=1000.0, dry_run=False)
        self.assertEqual(rc, mod.EXIT_OK)
        self.assertEqual(fake.state["set_calls"], 1)

    def test_dry_run_never_sets(self):
        fake = make_fake_plc(offset_seconds=5.0)
        comm = fake()
        rc = mod.sync_once(comm, threshold_ms=1000.0, verify_tol_ms=1000.0, dry_run=True)
        self.assertEqual(rc, mod.EXIT_OK)
        self.assertEqual(fake.state["set_calls"], 0)

    def test_set_rejected_raises_clocksyncerror(self):
        fake = make_fake_plc(offset_seconds=5.0, set_status="Path destination unknown")
        comm = fake()
        with self.assertRaises(mod.ClockSyncError):
            mod.sync_once(comm, threshold_ms=1000.0, verify_tol_ms=1000.0, dry_run=False)

    def test_set_exception_raises_clocksyncerror(self):
        fake = make_fake_plc(offset_seconds=5.0, set_raises=socket.error("boom"))
        comm = fake()
        with self.assertRaises(mod.ClockSyncError):
            mod.sync_once(comm, threshold_ms=1000.0, verify_tol_ms=1000.0, dry_run=False)

    def test_set_does_not_take_effect_fails_verification(self):
        fake = make_fake_plc(offset_seconds=5.0, set_applies=False)
        comm = fake()
        with self.assertRaises(mod.ClockSyncError):
            mod.sync_once(comm, threshold_ms=1000.0, verify_tol_ms=1000.0, dry_run=False)


# --------------------------------------------------------------------------- #
# End-to-end (run): connection, retries, exit codes
# --------------------------------------------------------------------------- #
class TestRun(Base):
    def test_success_within_tolerance(self):
        fake = make_fake_plc(offset_seconds=0.0)
        rc = self.run_with(fake, make_args())
        self.assertEqual(rc, mod.EXIT_OK)
        self.assertEqual(fake.state["set_calls"], 0)
        # Connection params are applied to the PLC object.
        self.assertEqual(fake.state["last"].IPAddress, "192.168.1.10")
        self.assertEqual(fake.state["last"].ProcessorSlot, 0)

    def test_success_after_correction(self):
        fake = make_fake_plc(offset_seconds=10.0, set_applies=True)
        rc = self.run_with(fake, make_args())
        self.assertEqual(rc, mod.EXIT_OK)
        self.assertEqual(fake.state["set_calls"], 1)

    def test_comms_failure_returns_exit_comms(self):
        fake = make_fake_plc(get_fail_times=99, get_fail_mode="exception")
        rc = self.run_with(fake, make_args(max_retries=1))
        self.assertEqual(rc, mod.EXIT_COMMS)
        # first attempt + 1 retry == 2 connection attempts
        self.assertEqual(fake.state["instances"], 2)

    def test_transient_failure_then_success(self):
        # First GetPLCTime call fails, the retry connects cleanly.
        fake = make_fake_plc(offset_seconds=0.0, get_fail_times=1, get_fail_mode="exception")
        rc = self.run_with(fake, make_args(max_retries=2))
        self.assertEqual(rc, mod.EXIT_OK)
        self.assertGreaterEqual(fake.state["instances"], 2)

    def test_set_failure_returns_exit_set_failed(self):
        fake = make_fake_plc(offset_seconds=10.0, set_status="Path destination unknown")
        rc = self.run_with(fake, make_args(max_retries=1))
        self.assertEqual(rc, mod.EXIT_SET_FAILED)

    def test_verify_failure_returns_exit_set_failed(self):
        fake = make_fake_plc(offset_seconds=10.0, set_applies=False)
        rc = self.run_with(fake, make_args(max_retries=1))
        self.assertEqual(rc, mod.EXIT_SET_FAILED)

    def test_dry_run_end_to_end(self):
        fake = make_fake_plc(offset_seconds=10.0)
        rc = self.run_with(fake, make_args(dry_run=True))
        self.assertEqual(rc, mod.EXIT_OK)
        self.assertEqual(fake.state["set_calls"], 0)


# --------------------------------------------------------------------------- #
# main(): argument handling and missing-dependency path
# --------------------------------------------------------------------------- #
class TestMain(Base):
    def test_pylogix_not_installed_returns_exit_comms(self):
        def boom():
            raise mod.CommsError("pylogix not installed")

        with mock.patch.object(mod, "import_pylogix", side_effect=boom):
            rc = mod.main(["192.168.1.10"])
        self.assertEqual(rc, mod.EXIT_COMMS)

    def test_invalid_slot_is_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            mod.main(["192.168.1.10", "--slot", "-1"])
        self.assertEqual(ctx.exception.code, 2)  # argparse usage error

    def test_empty_ip_is_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            mod.main(["   "])
        self.assertEqual(ctx.exception.code, 2)

    def test_main_success_path(self):
        fake = make_fake_plc(offset_seconds=0.0)
        with mock.patch.object(mod, "import_pylogix", return_value=fake):
            rc = mod.main(["192.168.1.10", "--threshold-ms", "1000"])
        self.assertEqual(rc, mod.EXIT_OK)


# --------------------------------------------------------------------------- #
# Strategies: reproduce the bug on 'stock' and confirm the fixes resolve it
# --------------------------------------------------------------------------- #
class TestStrategies(Base):
    def _sync(self, comm, strategy_name):
        return mod.sync_once(
            comm,
            threshold_ms=1000.0,
            verify_tol_ms=1000.0,
            dry_run=False,
            strategy=mod.STRATEGIES[strategy_name],
        )

    def test_stock_reproduces_timezone_offset(self):
        # A controller with a -7h zone: stock write corrupts UTC, verify fails.
        comm = make_tz_fake(tz_hours=-7.0)()
        with self.assertRaises(mod.ClockSyncError):
            self._sync(comm, "stock")

    def test_utc_attr11_syncs_cleanly(self):
        comm = make_tz_fake(tz_hours=-7.0)()
        self.assertEqual(self._sync(comm, "utc-attr11"), mod.EXIT_OK)
        self.assertGreaterEqual(comm.writes, 1)

    def test_local_attr6_syncs_cleanly(self):
        comm = make_tz_fake(tz_hours=-7.0)()
        self.assertEqual(self._sync(comm, "local-attr6"), mod.EXIT_OK)
        self.assertGreaterEqual(comm.writes, 1)

    def test_calibrate_syncs_cleanly(self):
        comm = make_tz_fake(tz_hours=-7.0)()
        self.assertEqual(self._sync(comm, "calibrate"), mod.EXIT_OK)
        self.assertGreaterEqual(comm.writes, 1)

    def test_fixes_work_across_zones(self):
        for tz in (-8.0, -5.0, 0.0, 5.5, 9.0):
            for name in ("utc-attr11", "local-attr6", "calibrate"):
                comm = make_tz_fake(tz_hours=tz)()
                self.assertEqual(
                    self._sync(comm, name), mod.EXIT_OK,
                    msg=f"strategy {name} failed at tz={tz}",
                )

    def test_default_strategy_is_auto(self):
        parser = mod.build_parser()
        self.assertEqual(parser.parse_args(["1.2.3.4"]).strategy, "auto")

    def test_auto_fixes_timezone_controller(self):
        # The smart default corrects a -7h-zone controller with no flag at all.
        comm = make_tz_fake(tz_hours=-7.0)()
        self.assertEqual(self._sync(comm, "auto"), mod.EXIT_OK)
        self.assertGreaterEqual(comm.writes, 1)

    def test_auto_handles_all_zones(self):
        for tz in (-8.0, -5.0, 0.0, 5.5, 5.75, 9.0):
            comm = make_tz_fake(tz_hours=tz)()
            self.assertEqual(
                self._sync(comm, "auto"), mod.EXIT_OK, msg=f"auto failed at tz={tz}"
            )

    def test_auto_falls_back_when_attr6_unreadable(self):
        # The plain fake has no Message() (raw attr-6 read). auto must degrade to
        # the standard SetPLCTime write rather than error.
        fake = make_fake_plc(offset_seconds=10.0, set_applies=True)
        rc = self.run_with(fake, make_args(strategy="auto", max_retries=0))
        self.assertEqual(rc, mod.EXIT_OK)
        self.assertEqual(fake.state["set_calls"], 1)

    def test_auto_dry_run_previews_offset_without_writing(self):
        comm = make_tz_fake(tz_hours=-7.0)()
        mod.log.setLevel(logging.INFO)
        with self.assertLogs(mod.log, level="INFO") as cm:
            rc = mod.sync_once(
                comm, threshold_ms=1000.0, verify_tol_ms=1000.0, dry_run=True,
                strategy=mod.STRATEGIES["auto"],
            )
        self.assertEqual(rc, mod.EXIT_OK)
        self.assertEqual(comm.writes, 0)  # dry run must not write
        output = "\n".join(cm.output)
        self.assertIn("COMPENSATE", output)
        self.assertIn("-7.00 h", output)

    def test_auto_decision_reads_both_attributes(self):
        comm = make_tz_fake(tz_hours=-7.0)()
        offset_us, a6, a11, verdict, _ = mod._auto_decision(comm)
        self.assertEqual(verdict, "zone")
        self.assertAlmostEqual(offset_us / 3_600_000_000.0, -7.0, places=2)
        self.assertIsNotNone(a6)
        self.assertIsNotNone(a11)

    def test_auto_preview_falls_back_when_attr6_unreadable(self):
        fake = make_fake_plc(offset_seconds=10.0)  # no Message() method
        comm = fake()
        msg = mod.STRATEGIES["auto"].preview(comm)
        self.assertIn("not readable", msg)

    def test_is_zone_offset_classifier(self):
        hour = 3_600_000_000
        self.assertTrue(mod._is_zone_offset(0))
        self.assertTrue(mod._is_zone_offset(-7 * hour))
        self.assertTrue(mod._is_zone_offset(int(5.5 * hour)))
        self.assertTrue(mod._is_zone_offset(int(5.75 * hour)))  # Nepal +5:45
        self.assertFalse(mod._is_zone_offset(int(2.13 * hour)))  # ~8 min off a quarter
        self.assertFalse(mod._is_zone_offset(20 * hour))         # too large

    def test_run_accepts_strategy_arg(self):
        comm_factory = make_tz_fake(tz_hours=-7.0)
        rc = self.run_with(comm_factory, make_args(strategy="utc-attr11", max_retries=0))
        self.assertEqual(rc, mod.EXIT_OK)

    def test_cli_parses_strategy_choice(self):
        parser = mod.build_parser()
        args = parser.parse_args(["1.2.3.4", "--strategy", "calibrate"])
        self.assertEqual(args.strategy, "calibrate")

    def test_cli_rejects_unknown_strategy(self):
        parser = mod.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["1.2.3.4", "--strategy", "nonsense"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
