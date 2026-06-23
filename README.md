# pylogix_examples

Example scripts for working with Rockwell Automation ControlLogix /
CompactLogix processors over EtherNet/IP using [pylogix](https://github.com/dmroeder/pylogix).

## Scripts

### `sync_controllogix_clock.py`

Synchronize a controller's Wall Clock Time to the local time of the server
running the script. It measures the offset between the controller and server
clocks (correcting for network round-trip latency), and writes the server's
time to the controller when the offset exceeds a configurable threshold, then
re-reads and verifies the correction.

```bash
python3 sync_controllogix_clock.py 192.168.1.10
python3 sync_controllogix_clock.py 192.168.1.10 --slot 0 --threshold-ms 1000
python3 sync_controllogix_clock.py 192.168.1.10 --dry-run -v
```

Run `python3 sync_controllogix_clock.py --help` for the full list of options.

## Requirements

```bash
pip install pylogix
```
