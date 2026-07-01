# On-site request: ControlLogix clock timezone fix

> Template — fill in `<CONTROLLER-IP>` and `<SLOT>` (CompactLogix is usually 0)
> before sending. Everything below is safe except the clearly-marked Step 3.

**Background (short):** The controller's clock is reading a whole number of hours
off (a timezone issue, not drift). We have an updated sync tool that auto-corrects
it. We need you to run two READ-ONLY steps and send the results back **before** we
do any write. Nothing but Step 3 changes the controller.

**Controller:** `<CONTROLLER-IP>`, slot `<SLOT>`

---

## Before you start
- Use the machine that already runs the clock sync (has Python + `pylogix`).
- Get the latest `sync_controllogix_clock.py` and put it in the folder you run
  from (e.g. `C:\Temp`). Grab `probe_wallclock.py` too if it's handy — it's only
  needed for the optional deep check below.
- Commands below use the Windows `py` launcher; if the file is elsewhere, `cd` to
  that folder first. (On Linux/Mac use `python3` instead of `py`.)

---

## Step 1 — Preview (READ-ONLY: `--dry-run` never writes)
This reads the clock and prints exactly what the fix *would* do — including the
measured timezone offset — but makes no change.

```
py sync_controllogix_clock.py <CONTROLLER-IP> --slot <SLOT> --dry-run
```

**Copy the whole console output and send it back.** That's all we need to
confirm the fix before writing.

---

## Step 2 — Apply the fix — DO NOT RUN until we confirm
Wait for our go-ahead after we review Step 1. When we say go, this is the command
that actually sets the clock (note: no `--dry-run`):

```
py sync_controllogix_clock.py <CONTROLLER-IP> --slot <SLOT>
```

Then send us that output as well so we can confirm it verified OK.

---

## Optional deep check (only if we ask)
A read-only report of every clock attribute, if we need more detail:

```
py probe_wallclock.py <CONTROLLER-IP> --slot <SLOT>
```

It writes **`wallclock_probe_report.txt`** — send that file back. (Contains only
clock values and the controller's model/revision — no passwords or program data.)

---

## Notes
- Step 1 and the optional check are completely safe and can be run anytime. Only
  Step 2 writes to the controller.
- The tool sets the PLC to match **this computer's** clock. Please confirm this
  machine's own time/date is correct (NTP synced) before Step 2 — otherwise the
  PLC will be set to whatever this computer thinks the time is.
- If any command errors out, just send us the full text of the error.

Thanks!
