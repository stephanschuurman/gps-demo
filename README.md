# gps-demo

Live GPS viewer for macOS. Connects to a Bluetooth or USB GPS receiver and displays position, speed, altitude, and satellites on an interactive map (OpenStreetMap).

## Devices

| Script | Connection | Device |
|---|---|---|
| `connect_bt_gps.py` | Bluetooth (RFCOMM) | BT A+ GPS (737-A+) |
| `connect_usb_gps.py` | USB serial / PyUSB | MTK GPS Receiver (0x0E8D:0x3329) |
| `gps_map.py` | USB serial / PyUSB | MTK GPS Receiver — with map viewer |

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate.fish   # or: source .venv/bin/activate
pip install -r requirements.txt
```

> Bluetooth also requires **blueutil**: `brew install blueutil`

## Usage

### Map viewer (recommended)

```bash
.venv/bin/python3 gps_map.py
```

Automatically opens a browser at `http://localhost:8080`. Shows:
- Live position + track on OpenStreetMap
- Altitude, course, speed, fix type, accuracy, HDOP
- Satellites in use / visible
- Skyplot (top-down view of the sky with satellites at azimuth + elevation)

**Options:**

| Option | Description | Default |
|---|---|---|
| `--port /dev/cu.xxx` | Serial port | auto-detect |
| `--baud 9600` | Baud rate | auto-detect |
| `--vid 0x0E8D` | USB Vendor ID | 0x0E8D (MTK) |
| `--pid 0x3329` | USB Product ID | 0x3329 |
| `--usb-direct` | Force PyUSB bulk-transfer | — |
| `--web-port 9000` | Web server port | 8080 |
| `--no-browser` | Don't open browser automatically | — |

### Stream NMEA (Bluetooth)

```bash
.venv/bin/python3 connect_bt_gps.py
```

Connects to "BT A+ GPS" via Bluetooth and prints NMEA sentences to stdout.

**Options:** `--name`, `--scan-seconds`, `--connect-timeout`, `--baud`, `--port`, `--no-stream`

### Stream NMEA (USB)

```bash
.venv/bin/python3 connect_usb_gps.py
```

Connects to the MTK USB receiver (serial or direct USB CDC ACM) and prints NMEA sentences.

**Options:** `--port`, `--baud`, `--vid`, `--pid`, `--usb-direct`, `--no-stream`

## NMEA sentences

Processed: `$GPGGA` / `$GNGGA`, `$GPRMC` / `$GNRMC`, `$GPGSV` / `$GNGSV`, `$GPGST`

## Device manual

[737-A+ Wireless GPS Receiver User's Manual](https://manualzz.com/doc/6600353/737-a--wireless-gps-receiver-user-s-manual)

