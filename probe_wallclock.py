#!/usr/bin/env python3
"""
probe_wallclock.py

READ-ONLY diagnostic for the ControlLogix / CompactLogix Wall Clock Time object.

Purpose
-------
sync_controllogix_clock.py reads and writes DIFFERENT attributes of the CIP
Wall Clock Time object (class 0x8B):

    * GetPLCTime()  reads  attribute 0x0B (11)  -- via Get_Attribute_List
    * SetPLCTime()  writes attribute 0x06 (6)   -- via Set_Attribute_List

If those two attributes represent different time bases on your controller
(e.g. one UTC, one local), a "set the clock" run leaves a clean, timezone-sized
residual at the verification step. We are seeing ~7 h in Pacific (UTC-7).

This script does NOT change the controller. It only READS a range of Wall Clock
attributes, dumps the raw bytes, and decodes every plausible timestamp it finds,
reporting each attribute's offset from the server's UTC clock and from the
server's LOCAL clock. That tells us, unambiguously and per-attribute, which
attribute holds UTC and which holds local time -- which is exactly what we need
to pick the correct fix.

*** SAFE TO RUN ON A LIVE, RUNNING CONTROLLER. IT PERFORMS READS ONLY. ***

Usage
-----
    python3 probe_wallclock.py 192.168.1.10
    python3 probe_wallclock.py 192.168.1.10 --slot 0
    python3 probe_wallclock.py 192.168.1.10 --out my_plc_report.txt

Requirements
------------
    pip install pylogix

What to send back
-----------------
The full report is printed to the screen AND written to a text file
(default: wallclock_probe_report.txt). Send us that file. It contains no
credentials or secrets -- just clock values, timezone offsets, and the
controller's product name/revision.
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
from datetime import datetime, timedelta, timezone
from struct import unpack_from

# CIP Wall Clock Time object.
WALLCLOCK_CLASS = 0x8B
WALLCLOCK_INSTANCE = 0x01

# CIP service codes.
SVC_GET_ATTR_SINGLE = 0x0E
SVC_GET_ATTR_LIST = 0x03

# Attributes we will try to read. 6 (write target) and 11 (read source) are the
# two the sync script actually uses; the rest are probed for context because the
# UTC-vs-local pair is not identical across controller families/firmware.
CANDIDATE_ATTRIBUTES = list(range(1, 13))

EPOCH = datetime(1970, 1, 1)  # naive-UTC epoch, matching pylogix's basis.

# Only treat a decoded value as a real clock if it lands in this window.
MIN_YEAR = 2015
MAX_YEAR = 2040


def import_plc():
    try:
        from pylogix import PLC  # type: ignore
        import pylogix
    except ImportError:
        sys.exit(
            "The 'pylogix' library is not installed. Install it with "
            "'pip install pylogix' and re-run."
        )
    return PLC, getattr(pylogix, "__version__", "unknown")


# --------------------------------------------------------------------------- #
# Output helpers -- everything is captured so it can be written to a file too.
# --------------------------------------------------------------------------- #
_LINES: list[str] = []


def emit(line: str = "") -> None:
    print(line)
    _LINES.append(line)


def hexdump(raw: bytes) -> str:
    if not raw:
        return "    <empty>"
    out = []
    for i in range(0, len(raw), 16):
        chunk = raw[i : i + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"    {i:4d}: {hexpart:<48s}  {asciipart}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Decoders -- find clock values inside a raw CIP response without assuming the
# exact byte offset (it can vary by service and controller).
# --------------------------------------------------------------------------- #
def _plausible(dt: datetime) -> bool:
    return MIN_YEAR <= dt.year <= MAX_YEAR


def scan_epoch_timestamps(raw: bytes) -> list[tuple[int, str, int, datetime]]:
    """
    Slide over the buffer looking for a value that decodes to a real date:
      * <Q  (64-bit) microseconds since the 1970 epoch  (pylogix's format)
      * <I  (32-bit) seconds      since the 1970 epoch  (belt & suspenders)
    Returns list of (offset, width, raw_value, datetime_naive_utc).
    """
    found = []
    for i in range(0, len(raw) - 7):
        try:
            val = unpack_from("<Q", raw, i)[0]
            dt = EPOCH + timedelta(microseconds=val)
            if _plausible(dt):
                found.append((i, "u64-microseconds", val, dt))
        except (OverflowError, OSError, ValueError):
            pass
    for i in range(0, len(raw) - 3):
        try:
            val = unpack_from("<I", raw, i)[0]
            dt = EPOCH + timedelta(seconds=val)
            if _plausible(dt):
                found.append((i, "u32-seconds", val, dt))
        except (OverflowError, OSError, ValueError):
            pass
    return found


def scan_int_array(raw: bytes) -> list[tuple[int, datetime]]:
    """
    Some controllers expose a "local date/time" attribute as an array of INTs
    (year, month, day, hour, minute, second, [microsecond]). Look for a run of
    16-bit words that parses as a valid calendar date. Returns (offset, dt).
    """
    found = []
    for i in range(0, len(raw) - 11):
        try:
            year, month, day, hour, minute, second = unpack_from("<6H", raw, i)
        except Exception:
            continue
        if not (MIN_YEAR <= year <= MAX_YEAR):
            continue
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        if not (hour <= 23 and minute <= 59 and second <= 61):
            continue
        try:
            found.append((i, datetime(year, month, day, hour, minute, second)))
        except ValueError:
            pass
    return found


def delta_hours(dt: datetime, reference: datetime) -> float:
    return (dt - reference).total_seconds() / 3600.0


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #
def read_attribute(comm, service: int, attribute, label: str):
    """Issue one raw CIP read via the public Message() API. Never writes."""
    try:
        resp = comm.Message(service, WALLCLOCK_CLASS, WALLCLOCK_INSTANCE, attribute)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the whole probe
        emit(f"  [{label}] EXCEPTION: {exc!r}")
        return
    status = getattr(resp, "Status", None)
    raw = getattr(resp, "Value", None)
    if not isinstance(raw, (bytes, bytearray)):
        emit(f"  [{label}] status={status!r}  (no raw bytes returned: {raw!r})")
        return
    raw = bytes(raw)
    emit(f"  [{label}] status={status!r}  len={len(raw)} bytes")
    emit(hexdump(raw))

    server_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    server_local = datetime.now()

    ts = scan_epoch_timestamps(raw)
    if ts:
        for offset, width, val, dt in ts:
            emit(
                f"      -> epoch value @offset {offset} ({width}): raw={val}"
            )
            emit(
                f"         decoded (as UTC)   = {dt.isoformat(sep=' ', timespec='milliseconds')}"
            )
            emit(
                f"         delta vs server UTC   = {delta_hours(dt, server_utc):+.3f} h"
            )
            emit(
                f"         delta vs server LOCAL = {delta_hours(dt, server_local):+.3f} h"
            )
    arr = scan_int_array(raw)
    for offset, dt in arr:
        emit(
            f"      -> INT-array date @offset {offset}: "
            f"{dt.isoformat(sep=' ', timespec='seconds')}"
        )
        emit(f"         delta vs server LOCAL = {delta_hours(dt, server_local):+.3f} h")
        emit(f"         delta vs server UTC   = {delta_hours(dt, server_utc):+.3f} h")
    if not ts and not arr:
        emit("      -> no timestamp-like value decoded from this attribute.")
    emit()


def probe(comm, pylogix_version: str) -> None:
    emit("=" * 74)
    emit("  ControlLogix Wall Clock Time probe  (READ-ONLY -- no writes performed)")
    emit("=" * 74)

    # --- Host / server context -------------------------------------------- #
    server_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    server_local = datetime.now()
    lt = time.localtime()
    emit("HOST / SERVER")
    emit(f"  script run at (UTC)   : {server_utc.isoformat(sep=' ', timespec='milliseconds')}")
    emit(f"  script run at (LOCAL) : {server_local.isoformat(sep=' ', timespec='milliseconds')}")
    emit(f"  local - UTC offset    : {delta_hours(server_local, server_utc):+.3f} h")
    emit(f"  tzname                : {time.tzname}  tm_isdst={lt.tm_isdst}")
    emit(f"  time.time()           : {time.time():.6f}")
    emit(f"  SetPLCTime WOULD write: {int(time.time() * 1_000_000)} us since epoch (NOT written)")
    emit(f"  SetPLCTime WOULD stamp: dst={lt.tm_isdst}")
    emit(f"  python                : {platform.python_version()} on {platform.platform()}")
    emit(f"  pylogix               : {pylogix_version}")
    emit()

    # --- Device identity (helps correlate firmware/family) ---------------- #
    emit("DEVICE PROPERTIES")
    try:
        dev = comm.GetDeviceProperties()
        value = getattr(dev, "Value", None)
        emit(f"  status={getattr(dev, 'Status', None)!r}")
        if value is not None:
            for attr in (
                "ProductName",
                "Vendor",
                "DeviceType",
                "ProductCode",
                "Revision",
                "SerialNumber",
                "Status",
            ):
                if hasattr(value, attr):
                    emit(f"  {attr:12s}: {getattr(value, attr)}")
    except Exception as exc:  # noqa: BLE001
        emit(f"  (GetDeviceProperties failed: {exc!r})")
    emit()

    # --- The canonical pylogix read (attribute 11) ------------------------ #
    emit("PYLOGIX GetPLCTime()  (this is attribute 0x0B / 11, the sync READ source)")
    try:
        raw_resp = comm.GetPLCTime(raw=True)
        human_resp = comm.GetPLCTime()
        rawval = getattr(raw_resp, "Value", None)
        emit(f"  raw   : status={getattr(raw_resp, 'Status', None)!r}  value={rawval!r} us")
        if isinstance(rawval, int):
            dt = EPOCH + timedelta(microseconds=rawval)
            emit(f"          decoded (as UTC) = {dt.isoformat(sep=' ', timespec='milliseconds')}")
            emit(f"          delta vs server UTC   = {delta_hours(dt, server_utc):+.3f} h")
            emit(f"          delta vs server LOCAL = {delta_hours(dt, server_local):+.3f} h")
        emit(f"  human : status={getattr(human_resp, 'Status', None)!r}  value={getattr(human_resp, 'Value', None)!r}")
    except Exception as exc:  # noqa: BLE001
        emit(f"  (GetPLCTime failed: {exc!r})")
    emit()

    # --- Raw attribute sweep ---------------------------------------------- #
    emit("RAW ATTRIBUTE SWEEP  (Get_Attribute_List, service 0x03 -- matches pylogix)")
    emit("  Interpretation guide:")
    emit("    * an attribute whose 'delta vs server UTC'   ~= 0  holds UTC time")
    emit("    * an attribute whose 'delta vs server LOCAL' ~= 0  holds LOCAL time")
    emit("    * attr 6 is the SetPLCTime WRITE target; attr 11 is the READ source")
    emit()
    for attr in CANDIDATE_ATTRIBUTES:
        read_attribute(
            comm, SVC_GET_ATTR_LIST, [attr], f"list  attr {attr} (0x{attr:02x})"
        )

    emit("RAW ATTRIBUTE SWEEP  (Get_Attribute_Single, service 0x0E -- cross-check)")
    emit()
    for attr in CANDIDATE_ATTRIBUTES:
        read_attribute(
            comm, SVC_GET_ATTR_SINGLE, attr, f"single attr {attr} (0x{attr:02x})"
        )

    emit("=" * 74)
    emit("  END OF PROBE. Send the output file back for analysis.")
    emit("=" * 74)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="READ-ONLY Wall Clock Time diagnostic for ControlLogix/CompactLogix.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("ip", help="IP address or hostname of the controller.")
    p.add_argument("--slot", type=int, default=0, help="Chassis slot of the controller.")
    p.add_argument(
        "--out",
        default="wallclock_probe_report.txt",
        help="File to write the full report to (in addition to the screen).",
    )
    p.add_argument(
        "--socket-timeout",
        type=float,
        default=5.0,
        help="Per-request socket timeout in seconds.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    PLC, version = import_plc()

    try:
        with PLC() as comm:
            comm.IPAddress = args.ip
            comm.ProcessorSlot = args.slot
            try:
                comm.SocketTimeout = args.socket_timeout
            except Exception:  # pragma: no cover - older builds
                pass
            emit(f"Connecting to {args.ip} slot {args.slot} ...")
            emit()
            probe(comm, version)
    except Exception as exc:  # noqa: BLE001 - report cleanly for the operator
        emit(f"\nFATAL: could not complete probe: {exc!r}")
        _flush(args.out)
        return 1

    _flush(args.out)
    return 0


def _flush(path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_LINES) + "\n")
        print(f"\n[report written to {path}]")
    except OSError as exc:
        print(f"\n[could not write report file {path!r}: {exc}]")


if __name__ == "__main__":
    sys.exit(main())
