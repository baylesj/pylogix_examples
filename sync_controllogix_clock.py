#!/usr/bin/env python3
"""
sync_controllogix_clock.py

Synchronize the Wall Clock Time of a Rockwell Automation ControlLogix /
CompactLogix processor to the local time of the server running this script.

How it works
------------
1. Connect to the controller over EtherNet/IP using pylogix.
2. Read the controller's clock, bracketing the read with server timestamps so
   that the network round-trip latency can be subtracted out. This gives an
   accurate measurement of the offset between the controller clock and the
   server clock.
3. If the absolute offset exceeds the configured threshold, write the server's
   current time to the controller.
4. Re-read the controller clock and verify that it now agrees with the server
   within tolerance. If it does not, the run is reported as a failure.

Time basis (important)
----------------------
pylogix synchronizes the controller's *absolute* clock in UTC:
  * GetPLCTime() returns the controller's system time as a UTC-naive datetime
    (microseconds since the 1970 UTC epoch).
  * SetPLCTime() writes the host's current UTC time (time.time()) and stamps the
    controller's DST flag from the host's local DST setting.

This script therefore measures and corrects the offset in UTC -- it compares the
controller clock against the SERVER'S UTC time. Setting the clock makes the
controller represent the same absolute instant as this server. The controller's
displayed *local* wall-clock time is then derived from the controller's own
Time Zone configuration (Studio 5000 -> Controller Properties -> Date/Time). So:

  * If the controller's configured time zone matches this server's, the
    controller's displayed local time will match the server's local time.
  * Because the comparison is UTC-vs-UTC, a correctly configured controller
    should only ever show small (drift-sized) offsets here. An offset near a
    whole number of hours usually means a real problem -- a dead clock battery,
    a controller that was never set, or a basis mismatch -- so the script logs
    an explicit warning when it sees one.

A whole-hour offset (e.g. 7 h in Pacific) usually means pylogix's read attribute
(0x0B) and write attribute (0x06) hold different time bases on your controller.
By default this script now detects that automatically (``--strategy auto``): it
reads both attributes, and if their difference is a real time-zone offset it
compensates the write so the controller's UTC clock is set correctly. You should
not need any flag. ``probe_wallclock.py`` and the explicit ``--strategy`` options
remain for diagnosis. See CLOCK_TIMEZONE.md.

Usage
-----
    python3 sync_controllogix_clock.py 192.168.1.10
    python3 sync_controllogix_clock.py 192.168.1.10 --slot 0 --threshold-ms 1000
    python3 sync_controllogix_clock.py 192.168.1.10 --dry-run -v
    python3 sync_controllogix_clock.py 192.168.1.10 --strategy utc-attr11
    python3 sync_controllogix_clock.py 192.168.1.10 --log-file /var/log/plc_clock.log

Cron (sync every day at 02:15, append to a log)::

    15 2 * * * /usr/bin/python3 /opt/scripts/sync_controllogix_clock.py \
        192.168.1.10 --log-file /var/log/plc_clock_sync.log >> /dev/null 2>&1

Requirements
------------
    pip install pylogix

Exit codes
----------
    0  Success: clock was already within tolerance, or was corrected & verified.
    1  Unexpected/internal error.
    2  Invalid command-line arguments (argparse default).
    3  Communication failure (controller not found / unreachable / read failed).
    4  Write or post-write verification failed.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from struct import pack, unpack_from

LOGGER_NAME = "plc_clock_sync"
log = logging.getLogger(LOGGER_NAME)

# The naive Unix epoch used to convert raw microsecond clock values into
# datetime objects. pylogix reports the controller clock as microseconds since
# the 1970 UTC epoch, so all datetimes in this module are naive-UTC.
EPOCH = datetime(1970, 1, 1)


def utcnow() -> datetime:
    """Current server time as a naive-UTC datetime (to match pylogix's basis)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def localnow() -> datetime:
    """Current server time as a naive-LOCAL datetime (for local-basis strategies)."""
    return datetime.now()


# CIP Wall Clock Time object (class 0x8B). The stock pylogix behavior READS
# attribute 0x0B (11) but WRITES attribute 0x06 (6); when those two attributes
# hold different time bases the "set" leaves a timezone-sized residual. The
# non-stock strategies below drive read and write consistently. See
# probe_wallclock.py, which determines empirically which attribute is which.
WALLCLOCK_CLASS = 0x8B
WALLCLOCK_INSTANCE = 0x01
ATTR_LOCAL = 0x06       # SetPLCTime write target
ATTR_DST = 0x0A         # DST flag, written alongside the time
ATTR_CURRENT = 0x0B     # GetPLCTime read source
SVC_GET_ATTR_LIST = 0x03
SVC_SET_ATTR_LIST = 0x04
# Get_Attribute_List payload offset within the raw response; this is the offset
# pylogix's own _get_plc_time() uses for attribute 11.
PAYLOAD_OFFSET = 56

# Exit codes -- see module docstring.
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_COMMS = 3
EXIT_SET_FAILED = 4


class CommsError(Exception):
    """Raised when the controller cannot be reached or a request fails."""


class ClockSyncError(Exception):
    """Raised when setting or verifying the controller clock fails."""


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def configure_logging(level: int, log_file: str | None) -> None:
    """Configure console (stderr) logging and, optionally, a rotating file."""
    log.setLevel(logging.DEBUG)  # handlers do the real filtering
    log.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(level)
    console.setFormatter(fmt)
    log.addHandler(console)

    if log_file:
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(fmt)
            log.addHandler(file_handler)
        except OSError as exc:
            # Don't abort the whole run just because the log file is unwritable;
            # warn on the console and carry on.
            log.warning("Could not open log file %r: %s", log_file, exc)


# --------------------------------------------------------------------------- #
# pylogix import (deferred so --help works without the dependency installed)
# --------------------------------------------------------------------------- #
def import_pylogix():
    try:
        from pylogix import PLC  # type: ignore
    except ImportError as exc:
        raise CommsError(
            "The 'pylogix' library is not installed. Install it with "
            "'pip install pylogix'."
        ) from exc
    return PLC


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def _status_text(response) -> str:
    """Best-effort extraction of a human-readable status from a pylogix Response."""
    status = getattr(response, "Status", None)
    return str(status) if status else "Unknown status"


def _response_ok(response) -> bool:
    """A pylogix Response is considered successful when Status == 'Success'."""
    return str(getattr(response, "Status", "")).lower() == "success"


# --------------------------------------------------------------------------- #
# Raw CIP access to the Wall Clock Time object (used by the non-stock strategies)
# --------------------------------------------------------------------------- #
def _plausible_us(value: int) -> bool:
    """True if ``value`` microseconds-since-1970 lands on a believable date."""
    try:
        year = (EPOCH + timedelta(microseconds=value)).year
    except (OverflowError, OSError, ValueError):
        return False
    return 2015 <= year <= 2040


def _decode_epoch_us(raw: bytes) -> int:
    """Extract epoch-microseconds from a Get_Attribute_List response.

    Tries the known payload offset first, then falls back to scanning for the
    first 8-byte window that decodes to a plausible date (robust across
    controller families whose response layout differs).
    """
    if len(raw) >= PAYLOAD_OFFSET + 8:
        value = unpack_from("<Q", raw, PAYLOAD_OFFSET)[0]
        if _plausible_us(value):
            return value
    for i in range(0, len(raw) - 7):
        value = unpack_from("<Q", raw, i)[0]
        if _plausible_us(value):
            return value
    raise CommsError("Could not decode a clock value from the controller response.")


def _raw_read_attr_us(comm, attr: int) -> int:
    """Read one Wall Clock attribute as epoch-microseconds via the Message API."""
    resp = comm.Message(SVC_GET_ATTR_LIST, WALLCLOCK_CLASS, WALLCLOCK_INSTANCE, [attr])
    if not _response_ok(resp):
        raise CommsError(
            f"Reading Wall Clock attribute 0x{attr:02x} failed "
            f"(status: {_status_text(resp)})"
        )
    raw = getattr(resp, "Value", b"")
    if not isinstance(raw, (bytes, bytearray)):
        raise CommsError(f"Wall Clock attribute 0x{attr:02x} returned no data.")
    return _decode_epoch_us(bytes(raw))


def _dst_flag() -> int:
    """Host DST flag as pylogix stamps it (clamped to 0/1)."""
    return 1 if time.localtime().tm_isdst > 0 else 0


def _raw_write(comm, attrs: list[int], values: list[bytes]):
    """Set_Attribute_List write of one or more Wall Clock attributes."""
    return comm.Message(
        SVC_SET_ATTR_LIST, WALLCLOCK_CLASS, WALLCLOCK_INSTANCE, attrs, values
    )


# --------------------------------------------------------------------------- #
# Read/write strategies
# --------------------------------------------------------------------------- #
# The stock strategy reproduces pylogix's built-in behavior (read attr 11 as
# UTC, write attr 6 as UTC via SetPLCTime). The others make read and write
# consistent so the timezone-sized residual disappears. Choose with --strategy;
# run probe_wallclock.py first to learn which attribute holds UTC vs local.
def _write_stock(comm):
    return comm.SetPLCTime()


def _write_utc_attr11(comm):
    """Write true UTC to attribute 11 -- the SAME attribute GetPLCTime reads."""
    micros = int(time.time() * 1_000_000)
    return _raw_write(
        comm, [ATTR_CURRENT, ATTR_DST], [pack("<Q", micros), pack("<B", _dst_flag())]
    )


def _write_local_attr6(comm):
    """Write local wall-clock time to attribute 6 (always writable)."""
    micros = int((localnow() - EPOCH).total_seconds() * 1_000_000)
    return _raw_write(
        comm, [ATTR_LOCAL, ATTR_DST], [pack("<Q", micros), pack("<B", _dst_flag())]
    )


def _write_calibrated(comm):
    """Measure the controller's own attr6-attr11 offset, then write attr 6 with
    (true UTC + that offset) so the derived attr 11 lands on true UTC."""
    offset_us = _raw_read_attr_us(comm, ATTR_LOCAL) - _raw_read_attr_us(comm, ATTR_CURRENT)
    log.debug("calibrate: measured attr6-attr11 offset = %+.3f h",
              offset_us / 3_600_000_000.0)
    micros = int(time.time() * 1_000_000) + offset_us
    return _raw_write(
        comm, [ATTR_LOCAL, ATTR_DST], [pack("<Q", micros), pack("<B", _dst_flag())]
    )


# Real-world UTC offsets are always a whole number of 15-minute steps and never
# exceed ~14 h. We use that to tell a genuine controller time-zone offset (worth
# compensating) apart from a garbage/implausible attribute read (do not trust).
_MAX_ZONE_US = 14 * 3_600_000_000
_ZONE_TOLERANCE_MIN = 6.0


def _is_zone_offset(offset_us: int) -> bool:
    """True if ``offset_us`` looks like a real UTC offset (15-min multiple, <=14h)."""
    if abs(offset_us) > _MAX_ZONE_US:
        return False
    minutes = offset_us / 60_000_000.0
    nearest_quarter = round(minutes / 15.0) * 15
    return abs(minutes - nearest_quarter) <= _ZONE_TOLERANCE_MIN


def _fmt_us(us) -> str:
    """Render epoch-microseconds as a readable naive datetime (or a placeholder)."""
    if us is None:
        return "<unreadable>"
    return (EPOCH + timedelta(microseconds=us)).isoformat(sep=" ", timespec="seconds")


def _auto_decision(comm) -> tuple:
    """Read-only. Determine what the 'auto' strategy would do.

    Returns (offset_us, a6_us, a11_us, verdict, detail) where verdict is one of:
      'unread'   -- attr 6 could not be read (would fall back to standard write)
      'not-zone' -- attr6-attr11 delta is not a plausible time zone (standard write)
      'agree'    -- attr 6 and attr 11 match; no zone offset (standard write)
      'zone'     -- a real zone offset was found (would compensate the write)
    """
    try:
        a6 = _raw_read_attr_us(comm, ATTR_LOCAL)
        a11 = _raw_read_attr_us(comm, ATTR_CURRENT)
    except Exception as exc:  # attr 6 not readable on this build
        return (None, None, None, "unread", str(exc))
    offset_us = a6 - a11
    if not _is_zone_offset(offset_us):
        return (offset_us, a6, a11, "not-zone", "")
    if round(offset_us / 900_000_000.0) == 0:  # within ~a quarter hour of zero
        return (offset_us, a6, a11, "agree", "")
    return (offset_us, a6, a11, "zone", "")


def _write_auto(comm):
    """Smart default: detect the controller's own attr6-attr11 zone offset and,
    only if it is a plausible time zone, compensate the write so the UTC clock
    (attr 11) is set correctly. Otherwise fall back to the standard write."""
    offset_us, _a6, _a11, verdict, detail = _auto_decision(comm)

    if verdict == "unread":
        log.info(
            "auto: could not read attr 6 to check for a zone offset (%s); "
            "using the standard SetPLCTime write.", detail
        )
        return _write_stock(comm)

    offset_h = offset_us / 3_600_000_000.0
    if verdict == "not-zone":
        log.warning(
            "auto: the controller's attr6-attr11 delta (%+.2f h) is not a plausible "
            "time-zone offset; using the standard write WITHOUT compensating. "
            "Run probe_wallclock.py to investigate.", offset_h
        )
        return _write_stock(comm)

    if verdict == "agree":
        log.debug("auto: attr 6 and attr 11 agree (no zone offset); standard write.")
        return _write_stock(comm)

    log.info(
        "auto: detected controller time-zone offset (attr6-attr11 = %+.2f h); "
        "compensating so the UTC clock (attr 11) is set correctly.", offset_h
    )
    micros = int(time.time() * 1_000_000) + offset_us
    return _raw_write(
        comm, [ATTR_LOCAL, ATTR_DST], [pack("<Q", micros), pack("<B", _dst_flag())]
    )


def _preview_auto(comm) -> str:
    """Read-only human description of what _write_auto would do (for --dry-run)."""
    offset_us, a6, a11, verdict, detail = _auto_decision(comm)
    if verdict == "unread":
        return (
            f"attr 6 is not readable on this controller ({detail}); would fall back to "
            "the standard SetPLCTime write, which does NOT correct a zone offset -- "
            "run probe_wallclock.py to investigate."
        )
    base = (
        f"attr 11 (UTC source) = {_fmt_us(a11)}, attr 6 (local) = {_fmt_us(a6)}, "
        f"delta = {offset_us / 3_600_000_000.0:+.2f} h"
    )
    if verdict == "not-zone":
        return base + " -- not a plausible time zone; would write WITHOUT compensating."
    if verdict == "agree":
        return base + " -- attributes agree; would use the standard write."
    target = _fmt_us(int(time.time() * 1_000_000))
    return base + f" -- would COMPENSATE so the UTC clock (attr 11) becomes ~{target}."


class ClockStrategy:
    """How to read, write, and time-base a controller clock sync."""

    def __init__(self, name, basis, reads_attr, writer, description, preview_fn=None):
        self.name = name
        self.basis = basis                 # "UTC" or "LOCAL" (label + server basis)
        self.reads_attr = reads_attr       # ATTR_CURRENT via GetPLCTime, or ATTR_LOCAL raw
        self._writer = writer
        self.description = description
        self._preview_fn = preview_fn

    def server_now(self) -> datetime:
        return utcnow() if self.basis == "UTC" else localnow()

    def write(self, comm):
        return self._writer(comm)

    def preview(self, comm) -> str:
        """Read-only description of what write() would do (for --dry-run)."""
        if self._preview_fn is not None:
            return self._preview_fn(comm)
        return f"would write via the '{self.name}' strategy"


STRATEGIES = {
    "auto": ClockStrategy(
        "auto", "UTC", ATTR_CURRENT, _write_auto,
        "Smart default: auto-detect the controller's zone offset and compensate "
        "only when it is a real time zone; otherwise behave like 'stock'.",
        preview_fn=_preview_auto,
    ),
    "stock": ClockStrategy(
        "stock", "UTC", ATTR_CURRENT, _write_stock,
        "pylogix default: read attr 11 (UTC), write attr 6 (UTC). Reproduces the bug.",
    ),
    "utc-attr11": ClockStrategy(
        "utc-attr11", "UTC", ATTR_CURRENT, _write_utc_attr11,
        "Symmetric on the UTC attribute: read AND write attr 11.",
    ),
    "local-attr6": ClockStrategy(
        "local-attr6", "LOCAL", ATTR_LOCAL, _write_local_attr6,
        "Symmetric on the local attribute: read AND write attr 6, compared to server local time.",
    ),
    "calibrate": ClockStrategy(
        "calibrate", "UTC", ATTR_CURRENT, _write_calibrated,
        "Measure the controller's attr6-attr11 offset and compensate the write.",
    ),
}
STOCK = STRATEGIES["stock"]


def read_plc_clock(comm, strategy: "ClockStrategy" = STOCK) -> tuple[datetime, float]:
    """
    Read the controller clock once, correcting for network round-trip latency.

    Returns a tuple of (plc_time, latency_ms) where ``plc_time`` is the
    controller clock as a naive datetime (in the strategy's basis) and
    ``latency_ms`` is the measured round-trip time of the read in milliseconds.

    Raises CommsError on any communication or status failure.
    """
    t0 = utcnow()
    if strategy.reads_attr == ATTR_LOCAL:
        # Local-basis strategy: read attribute 6 directly via raw CIP so the
        # read matches the write (GetPLCTime would read attribute 11 instead).
        try:
            plc_time = EPOCH + timedelta(microseconds=_raw_read_attr_us(comm, ATTR_LOCAL))
        except (socket.error, OSError) as exc:
            raise CommsError(f"Network error while reading controller clock: {exc}") from exc
        t1 = utcnow()
        raw = True
    else:
        try:
            # Prefer the raw microsecond value for precision. The keyword that
            # selects it has changed across pylogix releases (`raw` in 1.x,
            # `raw_dt` in some older builds), so try both, then fall back to the
            # plain datetime form.
            raw = True
            try:
                response = comm.GetPLCTime(raw=True)
            except TypeError:
                try:
                    response = comm.GetPLCTime(raw_dt=True)
                except TypeError:
                    response = comm.GetPLCTime()
                    raw = False
        except (socket.error, OSError) as exc:
            raise CommsError(f"Network error while reading controller clock: {exc}") from exc
        except Exception as exc:  # pylogix can raise assorted internal errors
            raise CommsError(f"Unexpected error while reading controller clock: {exc}") from exc
        t1 = utcnow()

        if not _response_ok(response):
            raise CommsError(
                f"Controller did not return a valid time (status: {_status_text(response)})"
            )

        value = getattr(response, "Value", None)
        if value is None:
            raise CommsError("Controller returned an empty time value.")

        if raw:
            try:
                plc_time = EPOCH + timedelta(microseconds=int(value))
            except (ValueError, TypeError, OverflowError) as exc:
                raise CommsError(f"Could not interpret raw controller time {value!r}: {exc}") from exc
        else:
            if not isinstance(value, datetime):
                raise CommsError(f"Unexpected controller time value: {value!r}")
            plc_time = value

    latency = t1 - t0
    latency_ms = latency.total_seconds() * 1000.0

    # The reading reflects the controller clock at roughly the midpoint of the
    # round trip; charge half the latency back so the comparison is fair.
    plc_time_corrected = plc_time - latency / 2

    log.debug(
        "Read controller clock: %s (raw=%s, round-trip %.1f ms)",
        plc_time.isoformat(sep=" ", timespec="milliseconds"),
        raw,
        latency_ms,
    )
    return plc_time_corrected, latency_ms


def measure_offset(comm, strategy: "ClockStrategy" = STOCK) -> tuple[float, float, datetime]:
    """
    Measure the offset between the controller clock and the server clock.

    Returns (offset_ms, latency_ms, plc_time):
      offset_ms > 0  -> controller is AHEAD of the server
      offset_ms < 0  -> controller is BEHIND the server
    """
    plc_time, latency_ms = read_plc_clock(comm, strategy)
    server_now = strategy.server_now()
    offset_ms = (plc_time - server_now).total_seconds() * 1000.0

    direction = "ahead of" if offset_ms >= 0 else "behind"
    log.info(
        "Controller clock is %.1f ms %s server "
        "(controller=%s %s, server=%s %s, link %.1f ms).",
        abs(offset_ms),
        direction,
        plc_time.isoformat(sep=" ", timespec="milliseconds"),
        strategy.basis,
        server_now.isoformat(sep=" ", timespec="milliseconds"),
        strategy.basis,
        latency_ms,
    )

    # A near-whole-hour offset almost always means a timezone/DST/basis
    # mismatch rather than ordinary drift; flag it so an operator can act.
    _warn_if_timezone_sized(offset_ms, strategy)
    return offset_ms, latency_ms, plc_time


def _warn_if_timezone_sized(offset_ms: float, strategy: "ClockStrategy" = STOCK) -> None:
    hours = abs(offset_ms) / 3_600_000.0
    nearest_hour = round(hours)
    if nearest_hour >= 1 and abs(hours - nearest_hour) < 0.05:
        log.warning(
            "Offset (%.0f min) is close to a whole number of hours. Because this "
            "comparison is %s-vs-%s, that is not normal clock drift -- it usually "
            "means a dead clock battery, a controller whose clock was never set, or "
            "a controller time-zone/DST/attribute-basis mismatch. If you are on the "
            "'stock' strategy, try probe_wallclock.py and an alternate --strategy.",
            abs(offset_ms) / 60_000.0,
            strategy.basis,
            strategy.basis,
        )


def set_plc_clock(comm, strategy: "ClockStrategy" = STOCK) -> None:
    """
    Write the server's current time to the controller using ``strategy``.

    Raises ClockSyncError if the write does not report success.
    """
    try:
        response = strategy.write(comm)
    except (socket.error, OSError) as exc:
        raise ClockSyncError(f"Network error while setting controller clock: {exc}") from exc
    except CommsError as exc:
        # e.g. a calibrate read failed before the write.
        raise ClockSyncError(f"Could not set controller clock: {exc}") from exc
    except Exception as exc:
        raise ClockSyncError(f"Unexpected error while setting controller clock: {exc}") from exc

    if not _response_ok(response):
        raise ClockSyncError(
            f"Controller rejected the clock write (status: {_status_text(response)})"
        )
    log.debug("Clock write (strategy=%s) reported success.", strategy.name)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def sync_once(comm, threshold_ms: float, verify_tol_ms: float, dry_run: bool,
              strategy: "ClockStrategy" = STOCK) -> int:
    """
    Perform a single measure -> (correct) -> verify cycle on an open connection.

    Returns one of the EXIT_* codes.
    """
    offset_ms, latency_ms, _ = measure_offset(comm, strategy)

    if abs(offset_ms) <= threshold_ms:
        log.info(
            "Offset %.1f ms is within the %.0f ms threshold; no change needed.",
            abs(offset_ms),
            threshold_ms,
        )
        return EXIT_OK

    if dry_run:
        log.warning(
            "[DRY RUN] Offset %.1f ms exceeds threshold %.0f ms; would set the "
            "controller clock (strategy '%s') but --dry-run is active. No change made.",
            abs(offset_ms),
            threshold_ms,
            strategy.name,
        )
        # Preview is read-only; it reports what the write would do (for 'auto',
        # this reads attr 6 so we can see the zone offset without writing).
        try:
            log.info("[DRY RUN] %s: %s", strategy.name, strategy.preview(comm))
        except Exception as exc:  # never let a preview read abort a dry run
            log.debug("Strategy preview unavailable: %s", exc)
        return EXIT_OK

    log.info(
        "Offset %.1f ms exceeds threshold %.0f ms; setting controller clock to "
        "server time.",
        abs(offset_ms),
        threshold_ms,
    )
    set_plc_clock(comm, strategy)

    # Verify. Allow a verification tolerance that accounts for the measured link
    # latency so a slow network doesn't produce false failures.
    allowed = max(verify_tol_ms, latency_ms + 250.0)
    new_offset_ms, _, _ = measure_offset(comm, strategy)
    if abs(new_offset_ms) <= allowed:
        log.info(
            "Clock corrected and verified: residual offset %.1f ms (<= %.0f ms).",
            abs(new_offset_ms),
            allowed,
        )
        return EXIT_OK

    raise ClockSyncError(
        f"Clock write did not take effect: residual offset {abs(new_offset_ms):.1f} ms "
        f"exceeds verification tolerance {allowed:.0f} ms."
    )


def run(args) -> int:
    """Open the connection (with retries) and run one sync cycle."""
    PLC = import_pylogix()
    strategy = STRATEGIES[getattr(args, "strategy", "auto")]
    if strategy.name != "auto":
        log.info("Using clock strategy '%s': %s", strategy.name, strategy.description)

    attempt = 0
    last_error: Exception | None = None
    while attempt <= args.max_retries:
        attempt += 1
        if attempt > 1:
            log.info(
                "Retry %d/%d after %.1f s ...",
                attempt - 1,
                args.max_retries,
                args.retry_delay,
            )
            _interruptible_sleep(args.retry_delay)

        try:
            with PLC() as comm:
                comm.IPAddress = args.ip
                comm.ProcessorSlot = args.slot
                # pylogix exposes SocketTimeout on recent releases; set it
                # defensively without failing on older ones.
                try:
                    comm.SocketTimeout = args.socket_timeout
                except Exception:  # pragma: no cover - attribute may not exist
                    log.debug("pylogix build does not expose SocketTimeout; using default.")

                log.info(
                    "Connecting to controller at %s slot %d (timeout %.1fs) ...",
                    args.ip,
                    args.slot,
                    args.socket_timeout,
                )
                return sync_once(
                    comm,
                    threshold_ms=args.threshold_ms,
                    verify_tol_ms=args.verify_tolerance_ms,
                    dry_run=args.dry_run,
                    strategy=strategy,
                )

        except CommsError as exc:
            last_error = exc
            log.warning("Communication problem: %s", exc)
            # Transient -- worth retrying.
            continue
        except ClockSyncError as exc:
            # The write/verify failed. Retrying a failed write is reasonable.
            last_error = exc
            log.warning("Clock set/verify problem: %s", exc)
            continue

    # All attempts exhausted.
    if isinstance(last_error, ClockSyncError):
        log.error("Failed to set controller clock after %d attempt(s): %s",
                  attempt, last_error)
        return EXIT_SET_FAILED
    log.error(
        "Could not synchronize controller clock after %d attempt(s): %s",
        attempt,
        last_error,
    )
    return EXIT_COMMS


def _interruptible_sleep(seconds: float) -> None:
    """Sleep that responds promptly to Ctrl-C."""
    import time

    time.sleep(max(0.0, seconds))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize a Rockwell ControlLogix/CompactLogix processor clock to "
            "the local time of this server via pylogix."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "ip",
        help="IP address or hostname of the controller / Ethernet module.",
    )
    parser.add_argument(
        "--slot",
        type=int,
        default=0,
        help="Chassis slot of the controller (CompactLogix is typically 0).",
    )
    parser.add_argument(
        "--threshold-ms",
        type=float,
        default=1000.0,
        help="Only correct the clock when the absolute offset exceeds this many "
        "milliseconds.",
    )
    parser.add_argument(
        "--verify-tolerance-ms",
        type=float,
        default=1000.0,
        help="Maximum residual offset (ms) accepted after a write when verifying.",
    )
    parser.add_argument(
        "--socket-timeout",
        type=float,
        default=5.0,
        help="Per-request socket timeout in seconds.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Number of additional attempts after the first on transient failure.",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=3.0,
        help="Seconds to wait between attempts.",
    )
    parser.add_argument(
        "--strategy",
        choices=list(STRATEGIES),
        default="auto",
        help="Read/write strategy. 'auto' (default) detects the controller's own "
        "zone offset and compensates only when it is a real time zone, otherwise "
        "behaving like 'stock' -- you normally do not need to set this. Overrides: "
        "'stock' matches pylogix (read attr 11, write attr 6); 'utc-attr11' reads "
        "and writes attr 11 (UTC); 'local-attr6' reads and writes attr 6 (local); "
        "'calibrate' always compensates. Run probe_wallclock.py to diagnose.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Measure and report the offset but never write to the controller.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional path to a rotating log file (in addition to stderr).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase console verbosity (-v for INFO, -vv for DEBUG).",
    )
    return parser


def validate_args(args, parser: argparse.ArgumentParser) -> None:
    if not args.ip or not args.ip.strip():
        parser.error("Controller IP/hostname must not be empty.")
    if args.slot < 0:
        parser.error("--slot must be >= 0.")
    if args.threshold_ms < 0:
        parser.error("--threshold-ms must be >= 0.")
    if args.verify_tolerance_ms <= 0:
        parser.error("--verify-tolerance-ms must be > 0.")
    if args.socket_timeout <= 0:
        parser.error("--socket-timeout must be > 0.")
    if args.max_retries < 0:
        parser.error("--max-retries must be >= 0.")
    if args.retry_delay < 0:
        parser.error("--retry-delay must be >= 0.")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(args, parser)

    console_level = logging.WARNING
    if args.verbose == 1:
        console_level = logging.INFO
    elif args.verbose >= 2:
        console_level = logging.DEBUG
    else:
        # Default to INFO so cron logs are useful even without -v.
        console_level = logging.INFO

    configure_logging(console_level, args.log_file)

    try:
        result = run(args)
        if result == EXIT_OK:
            log.info("Done.")
        return result
    except KeyboardInterrupt:
        log.error("Interrupted by user.")
        return EXIT_INTERNAL
    except CommsError as exc:
        # Raised before the retry loop (e.g. pylogix not installed).
        log.error("%s", exc)
        return EXIT_COMMS
    except Exception:  # noqa: BLE001 - last-resort guard for a cron job
        log.exception("Unexpected internal error.")
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
