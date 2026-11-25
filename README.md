# psyonic-ble

Command line tool for setting up, testing and reading out a PSYONIC Ability Hand over Bluetooth LE.
It implements the BLE command set documented in PSYONIC's
[ability-hand-api](https://github.com/psyonicinc/ability-hand-api) on top of [bleak](https://github.com/hbldh/bleak).

## What it can do

| Area | Commands |
|---|---|
| Find and identify | `scan`, `info` (name, firmware, control mode, hardware, motor check), `name` |
| Settings | `mode` (control mode), `flag` (binary settings), `word` (32-bit settings), `comm` (COMM line mode and baud rate) |
| Sensor data | `stream` (position, velocity, current, touch, IMU, ...), `battery`, `temp` |
| Test motion and feedback | `grip`, `open`, `led`, `vibrate` |
| Anything else | `raw` (any BLE command), `shell` (interactive) |

## Install

```bash
uv tool install -e .          # puts `psyonic-ble` on your PATH
# or, without installing:
uv run psyonic-ble --help
```

## Usage

The hand accepts only one BLE connection, so disconnect the PSYONIC app first.

```bash
psyonic-ble scan                                # hands in range
psyonic-ble info
psyonic-ble mode                                # read control mode; `mode bluetooth` sets it
psyonic-ble flag list                           # documented binary settings
psyonic-ble flag get                            # current values, * = non-default
psyonic-ble flag enable vibration
psyonic-ble word get collision-voltage
psyonic-ble comm                                # COMM line mode (I2C/UART/RS485) and baud
psyonic-ble comm uart --baud 230400
psyonic-ble stream list
psyonic-ble stream position -d 5 --csv > pos.csv
psyonic-ble stream fsr                          # all 30 touch sensors
psyonic-ble battery
psyonic-ble temp
psyonic-ble grip list                           # preset grips
psyonic-ble grip power -p 0.5                   # power grasp, 0.5 s motion period
psyonic-ble grip pinch --open                   # pinch's open configuration
psyonic-ble open
psyonic-ble led 00FE45
psyonic-ble raw g03:0.5 RZ16
psyonic-ble -v info                             # -v prints all BLE traffic
psyonic-ble shell
```

Each command connects anew, which takes a few seconds. `shell` keeps one connection open: at the
prompt you can type subcommands (`grip power`, `stream position -c 10`) or raw BLE commands
(`g03:0.5`, `RZ16`). Ctrl-C stops a running command; Ctrl-D or `exit` quits.

Pick a hand with `-a <address>` or `-n <name part>`, or set `PSYONIC_BLE_ADDRESS` / `PSYONIC_BLE_NAME`.
`psyonic-ble <command> -h` shows all options of a command.

## Notes

- `grip` and `open` only move the hand in Bluetooth control mode (`mode bluetooth`). Set the
  previous mode again afterwards.
- Settings writes (`flag`, `word`, `mode`, `comm`, `name`) are saved to flash. Reserved flags,
  enabling flag 39 (disables the air temperature protection), and undocumented or calibration
  words need `--force`.
- `stream fsr` accepts both the documented 60-byte int16 format and the 45-byte packed 12-bit
  format decoded by PSYONIC's MATLAB plotter.
- Read status (`S`) and firmware updates (`O`) are not wrapped; use `raw` for those.

## Tests

```bash
uv run pytest
```
