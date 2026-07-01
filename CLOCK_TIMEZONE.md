# ControlLogix clock: UTC-vs-local offset

## Symptom
After `sync_controllogix_clock.py` sets the clock, the post-write check reports a
whole-hour offset (e.g. **7 h in Pacific / UTC-7**).

## Cause
pylogix's `GetPLCTime`/`SetPLCTime` touch **different attributes** of the CIP
Wall Clock Time object (class `0x8B`), verified in pylogix 1.1.5:

| Operation | CIP | Attribute |
|-----------|-----|-----------|
| `GetPLCTime()` | Get_Attribute_List | `0x0B` (11) — read source |
| `SetPLCTime()` | Set_Attribute_List | `0x06` (6) — write target |

If those two attributes hold different time bases (one UTC, one local), a "set"
leaves a clean, timezone-sized residual. Which attribute is UTC vs local is **not
identical across firmware**, so the script confirms it empirically at runtime.

## The fix: it's automatic (`--strategy auto`, the default)
`sync_controllogix_clock.py` now handles this itself — **no flag needed**:

1. Reads both attr 6 and attr 11 and computes their difference.
2. If that difference is a real time-zone offset (a multiple of 15 min, ≤14 h),
   it compensates the write so the controller's UTC clock (attr 11) is set
   correctly.
3. Otherwise (attr 6 unreadable, or the delta isn't zone-shaped) it falls back to
   the standard `SetPLCTime` write — i.e. exactly the old behavior.
4. It always verifies attr 11 == server UTC afterward, so a wrong guess fails
   loudly instead of silently corrupting the clock.

```
python3 sync_controllogix_clock.py <ip>            # auto-detects and corrects
python3 sync_controllogix_clock.py <ip> --dry-run  # measure only, no write
```

The delta the script relies on (attr6 − attr11) is stable even when the absolute
clock is badly wrong, so a dead battery or never-set clock doesn't fool it.

## Diagnosis / manual override (optional)
Read-only probe (safe on a live controller), for when you want to see the raw
attributes yourself:
```
python3 probe_wallclock.py <controller-ip> --slot <n>   # writes wallclock_probe_report.txt
```
- attr whose delta-vs-UTC ≈ 0 → holds UTC
- attr whose delta-vs-local ≈ 0 → holds local

Explicit `--strategy` overrides (also available standalone in
`sync_controllogix_clock_fixes.py`, which is dry-run unless `--commit`):

| Strategy | Read + write | Use when |
|----------|--------------|----------|
| `auto` | detect zone offset, compensate if real (**default**) | normal use — no flag needed |
| `stock` | read 11, write 6 | reproduce the bug / baseline |
| `utc-attr11` | read + write attr 11 (UTC) | force writing the UTC attribute directly |
| `local-attr6` | read + write attr 6 (local) | you want the controller's *local* display to track server local time |
| `calibrate` | write attr 6 with measured offset, always | force compensation without auto's guardrails |

## Notes
- `probe_wallclock.py` never writes. `sync_controllogix_clock_fixes.py` is
  dry-run unless `--commit`. `sync_controllogix_clock.py` writes only when the
  offset exceeds `--threshold-ms` and `--dry-run` is not set.
- A whole-hour offset can also mean a dead clock battery or a never-set clock —
  the probe distinguishes these (the offset won't be a clean zone multiple).
- DST: 7 h = PDT (DST active), 8 h = PST. The write stamps the host DST flag.
