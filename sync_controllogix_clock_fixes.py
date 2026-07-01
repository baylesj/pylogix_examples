#!/usr/bin/env python3
"""
sync_controllogix_clock_fixes.py

Candidate fixes for the Wall Clock Time UTC-vs-local offset seen with the stock
sync_controllogix_clock.py (which reads attribute 0x0B / 11 but writes attribute
0x06 / 6 of CIP class 0x8B -- an asymmetry that yields a timezone-sized residual).

This script implements several interchangeable strategies for the READ + WRITE
pair so we can confirm on the bench which one drives the controller correctly,
WITHOUT editing pylogix. Pick one with --strategy.

    stock        Reproduce the current behaviour: write attr 6 (UTC us) + DST,
                 read attr 11, compare vs server UTC. (Baseline -- shows the bug.)

    utc-attr11   Write UTC us to attribute 11 (the SAME attribute we read) + DST,
                 read attr 11, compare vs server UTC. Fully symmetric on the UTC
                 attribute. Correct fix IF the probe shows attr 11 == UTC and the
                 controller accepts a write to attr 11.

    local-attr6  Write LOCAL wall-clock us to attribute 6, read attr 6, compare
                 vs server LOCAL. Symmetric on the local attribute (attr 6 is
                 always writable -- the stock code already writes it). Correct fix
                 IF the probe shows attr 6 == local time.

    calibrate    Assumption-light: measure the controller's own (attr6 - attr11)
                 offset live, then write attr 6 with (true UTC + that offset) so
                 the derived attr 11 lands on true UTC. Read attr 11, compare vs
                 server UTC. Use when we are unsure which attribute is which.

SAFETY
------
Dry-run by default: it READS and prints exactly what it WOULD write, but changes
nothing. Add --commit to actually write the clock. Every run re-reads attr 6 and
attr 11 before (and, if committed, after) so it doubles as a diagnostic.

Usage
-----
    # See what each strategy would do (no writes):
    python3 sync_controllogix_clock_fixes.py 192.168.1.10 --strategy utc-attr11
    python3 sync_controllogix_clock_fixes.py 192.168.1.10 --strategy local-attr6

    # Actually set the clock with the chosen strategy:
    python3 sync_controllogix_clock_fixes.py 192.168.1.10 --strategy utc-attr11 --commit

Requirements
------------
    pip install pylogix
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from struct import pack, unpack_from

WALLCLOCK_CLASS = 0x8B
WALLCLOCK_INSTANCE = 0x01
ATTR_LOCAL = 0x06       # SetPLCTime write target
ATTR_DST = 0x0A         # DST flag (stock writes this alongside attr 6)
ATTR_CURRENT = 0x0B     # GetPLCTime read source

SVC_GET_ATTR_LIST = 0x03
SVC_SET_ATTR_LIST = 0x04

EPOCH = datetime(1970, 1, 1)
# Get_Attribute_List payload sits at this offset in the raw response; this is the
# offset pylogix's own _get_plc_time() uses for attribute 11.
PAYLOAD_OFFSET = 56


def import_plc():
    try:
        from pylogix import PLC  # type: ignore
    except ImportError:
        sys.exit("pylogix not installed. Run 'pip install pylogix'.")
    return PLC


# --------------------------------------------------------------------------- #
# Raw CIP read / write of the Wall Clock Time object (via the public Message API)
# --------------------------------------------------------------------------- #
def _decode_epoch_us(raw: bytes) -> int | None:
    """Return microseconds-since-1970 from a Get_Attribute_List response.

    Try the known payload offset first; if that value is not a plausible date,
    scan for the first 8-byte window that decodes to a real one.
    """
    if len(raw) >= PAYLOAD_OFFSET + 8:
        val = unpack_from("<Q", raw, PAYLOAD_OFFSET)[0]
        if _plausible_us(val):
            return val
    for i in range(0, len(raw) - 7):
        val = unpack_from("<Q", raw, i)[0]
        if _plausible_us(val):
            return val
    return None


def _plausible_us(val: int) -> bool:
    try:
        year = (EPOCH + timedelta(microseconds=val)).year
    except (OverflowError, OSError, ValueError):
        return False
    return 2015 <= year <= 2040


def read_attr_us(comm, attr: int) -> tuple[int | None, object, bytes]:
    """Read one Wall Clock attribute and decode it as epoch microseconds."""
    resp = comm.Message(SVC_GET_ATTR_LIST, WALLCLOCK_CLASS, WALLCLOCK_INSTANCE, [attr])
    raw = getattr(resp, "Value", b"")
    raw = bytes(raw) if isinstance(raw, (bytes, bytearray)) else b""
    return (_decode_epoch_us(raw) if raw else None), getattr(resp, "Status", None), raw


def write_attrs(comm, attrs: list[int], values: list[bytes]):
    """Set_Attribute_List write of one or more Wall Clock attributes."""
    return comm.Message(
        SVC_SET_ATTR_LIST, WALLCLOCK_CLASS, WALLCLOCK_INSTANCE, attrs, values
    )


def _fmt(us: int | None) -> str:
    if us is None:
        return "<undecoded>"
    return (EPOCH + timedelta(microseconds=us)).isoformat(sep=" ", timespec="milliseconds")


def _dst_flag() -> int:
    return 1 if time.localtime().tm_isdst > 0 else 0


def server_utc_us() -> int:
    return int(time.time() * 1_000_000)


def server_local_us() -> int:
    # Local wall-clock instant expressed as microseconds since the epoch, so it
    # decodes (read back "as UTC") to the local wall time.
    return int((datetime.now() - EPOCH).total_seconds() * 1_000_000)


# --------------------------------------------------------------------------- #
# Strategy plans: each returns (write_attrs, write_values, verify_attr, verify_basis_us)
# --------------------------------------------------------------------------- #
def plan(strategy: str, comm) -> tuple[list[int], list[bytes], int, int, str]:
    dst = _dst_flag()
    if strategy == "stock":
        utc = server_utc_us()
        return [ATTR_LOCAL, ATTR_DST], [pack("<Q", utc), pack("<B", dst)], \
            ATTR_CURRENT, server_utc_us(), "server UTC"

    if strategy == "utc-attr11":
        utc = server_utc_us()
        return [ATTR_CURRENT, ATTR_DST], [pack("<Q", utc), pack("<B", dst)], \
            ATTR_CURRENT, server_utc_us(), "server UTC"

    if strategy == "local-attr6":
        local = server_local_us()
        return [ATTR_LOCAL, ATTR_DST], [pack("<Q", local), pack("<B", dst)], \
            ATTR_LOCAL, server_local_us(), "server LOCAL"

    if strategy == "calibrate":
        a6, _, _ = read_attr_us(comm, ATTR_LOCAL)
        a11, _, _ = read_attr_us(comm, ATTR_CURRENT)
        if a6 is None or a11 is None:
            sys.exit("calibrate: could not read both attr 6 and attr 11 to measure offset.")
        offset_us = a6 - a11
        print(f"  calibrate: measured (attr6 - attr11) = {offset_us / 3_600_000_000:+.3f} h")
        utc = server_utc_us()
        return [ATTR_LOCAL, ATTR_DST], [pack("<Q", utc + offset_us), pack("<B", dst)], \
            ATTR_CURRENT, server_utc_us(), "server UTC"

    sys.exit(f"unknown strategy {strategy!r}")


def snapshot(comm, label: str) -> None:
    a6, s6, _ = read_attr_us(comm, ATTR_LOCAL)
    a11, s11, _ = read_attr_us(comm, ATTR_CURRENT)
    su, sl = server_utc_us(), server_local_us()
    print(f"  [{label}]")
    print(f"    attr  6 (write target): {_fmt(a6)}  (status={s6})")
    print(f"    attr 11 (read source) : {_fmt(a11)}  (status={s11})")
    print(f"    server UTC            : {_fmt(su)}")
    print(f"    server LOCAL          : {_fmt(sl)}")
    if a6 is not None and a11 is not None:
        print(f"    attr6 - attr11        : {(a6 - a11) / 3_600_000_000:+.3f} h")
    if a11 is not None:
        print(f"    attr11 - server UTC   : {(a11 - su) / 3_600_000_000:+.3f} h")


def residual_report(comm, verify_attr: int, basis_us: int, basis_name: str) -> None:
    val, status, _ = read_attr_us(comm, verify_attr)
    if val is None:
        print(f"  VERIFY: could not decode attr {verify_attr} (status={status}).")
        return
    resid_ms = (val - basis_us) / 1000.0
    hours = abs(resid_ms) / 3_600_000.0
    flag = ""
    if round(hours) >= 1 and abs(hours - round(hours)) < 0.05:
        flag = "  <-- WHOLE-HOUR residual: strategy did not resolve the basis mismatch"
    print(
        f"  VERIFY: attr {verify_attr} vs {basis_name} = {resid_ms:+.1f} ms "
        f"({resid_ms / 3_600_000.0:+.3f} h){flag}"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Try candidate fixes for the ControlLogix clock UTC/local offset.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("ip", help="Controller IP/hostname.")
    p.add_argument("--slot", type=int, default=0)
    p.add_argument(
        "--strategy",
        required=True,
        choices=["stock", "utc-attr11", "local-attr6", "calibrate"],
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help="Actually write the clock. Without this the script is read-only.",
    )
    p.add_argument("--socket-timeout", type=float, default=5.0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    PLC = import_plc()

    with PLC() as comm:
        comm.IPAddress = args.ip
        comm.ProcessorSlot = args.slot
        try:
            comm.SocketTimeout = args.socket_timeout
        except Exception:  # pragma: no cover
            pass

        print(f"Strategy: {args.strategy}   commit={args.commit}")
        print(f"Controller: {args.ip} slot {args.slot}\n")

        print("BEFORE:")
        snapshot(comm, "before")
        print()

        attrs, values, verify_attr, basis_us, basis_name = plan(args.strategy, comm)
        planned_us = unpack_from("<Q", values[0], 0)[0]
        print(
            f"PLAN: write attr(s) {['0x%02x' % a for a in attrs]} "
            f"with primary value {_fmt(planned_us)} "
            f"(dst={_dst_flag()}); verify attr {verify_attr} vs {basis_name}."
        )

        if not args.commit:
            print("\n[DRY RUN] No write performed. Re-run with --commit to apply.")
            return 0

        resp = write_attrs(comm, attrs, values)
        print(f"\nWROTE. status={getattr(resp, 'Status', None)}")
        time.sleep(0.2)

        print("\nAFTER:")
        snapshot(comm, "after")
        residual_report(comm, verify_attr, basis_us, basis_name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
