#!/usr/bin/env python3
"""Scan and connect to a Bluetooth GPS device on macOS.

Default target name: "BT A+ GPS"
Requires: blueutil (brew install blueutil)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

try:
    import serial  # pyserial
except ImportError:  # checked at runtime via require_pyserial()
    pass


@dataclass
class Device:
    address: str
    name: str
    connected: bool = False
    paired: bool = False


def run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def require_pyserial() -> None:
    try:
        import serial  # noqa: F401
    except ImportError:
        print("pyserial is not installed.")
        print("Install it with: pip install pyserial")
        sys.exit(1)


def require_blueutil() -> None:
    if shutil.which("blueutil"):
        return

    print("blueutil is not installed.")
    print("Install it with: brew install blueutil")
    sys.exit(1)


def parse_devices_json(raw: str) -> list[Device]:
    if not raw.strip():
        return []

    payload: Any = json.loads(raw)
    if isinstance(payload, dict):
        payload = [payload]

    devices: list[Device] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        address = str(item.get("address", "")).strip()
        name = str(item.get("name", "")).strip()
        if not address:
            continue
        devices.append(
            Device(
                address=address,
                name=name,
                connected=bool(item.get("connected", False)),
                paired=bool(item.get("paired", False)),
            )
        )
    return devices


def blueutil_list_paired() -> list[Device]:
    proc = run_cmd(["blueutil", "--paired", "--format", "json"], check=False)
    if proc.returncode != 0:
        return []
    try:
        return parse_devices_json(proc.stdout)
    except json.JSONDecodeError:
        return []


def blueutil_inquiry(seconds: int) -> list[Device]:
    proc = run_cmd(
        ["blueutil", "--inquiry", str(seconds), "--format", "json"],
        check=False,
    )
    if proc.returncode != 0:
        return []
    try:
        return parse_devices_json(proc.stdout)
    except json.JSONDecodeError:
        return []


def find_target(devices: list[Device], target_name: str) -> Device | None:
    target_fold = target_name.casefold()
    for dev in devices:
        if dev.name and dev.name.casefold() == target_fold:
            return dev
    for dev in devices:
        if dev.name and target_fold in dev.name.casefold():
            return dev
    return None


def is_connected(address: str) -> bool:
    proc = run_cmd(["blueutil", "--is-connected", address], check=False)
    if proc.returncode != 0:
        return False
    return proc.stdout.strip() == "1"


def connect_device(address: str, timeout: int) -> bool:
    run_cmd(["blueutil", "--connect", address], check=False)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_connected(address):
            return True
        time.sleep(1)
    return False


def best_serial_port(candidates: list[str]) -> str | None:
    """Prefer /dev/cu.* over /dev/tty.* for outgoing connections on macOS."""
    for p in candidates:
        if p.startswith("/dev/cu."):
            return p
    return candidates[0] if candidates else None


NMEA_PREFIXES = ("$GP", "$GN", "$GL", "$GA", "$GB", "$BD", "$QZ", "$II")
BAUD_CANDIDATES = (4800, 9600, 38400, 57600, 19200, 115200)


def _open_serial(port: str, baud: int) -> "serial.Serial":  # type: ignore[name-defined]
    return serial.Serial(  # type: ignore[name-defined]
        port,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,  # type: ignore[name-defined]
        stopbits=serial.STOPBITS_ONE,  # type: ignore[name-defined]
        parity=serial.PARITY_NONE,  # type: ignore[name-defined]
        timeout=2.0,
    )


def detect_baud(port: str, probe_secs: int = 3) -> int | None:
    """Try common baud rates and return the first one that produces NMEA sentences."""
    for baud in BAUD_CANDIDATES:
        print(f"  Trying {baud} baud...", end=" ", flush=True)
        try:
            with _open_serial(port, baud) as ser:
                deadline = time.monotonic() + probe_secs
                while time.monotonic() < deadline:
                    raw = ser.readline()
                    if not raw:
                        continue
                    line = raw.decode("ascii", errors="replace").strip()
                    if any(line.startswith(p) for p in NMEA_PREFIXES):
                        print(f"got NMEA at {baud} baud.")
                        return baud
        except Exception:
            pass
        print("no NMEA.")
    return None


def stream_nmea(port: str, baud: int) -> None:
    print(f"Streaming NMEA from {port} at {baud} baud — Ctrl+C to stop.")
    try:
        with _open_serial(port, baud) as ser:
            while True:
                raw = ser.readline()
                if raw:
                    line = raw.decode("ascii", errors="replace").strip()
                    if line:
                        print(line)
    except Exception as exc:  # serial.SerialException or similar
        print(f"Serial error: {exc}")


def list_serial_candidates() -> list[str]:
    proc = run_cmd(["sh", "-lc", "ls /dev/cu.* /dev/tty.* 2>/dev/null | grep -E 'BTAGPS|Bluetooth' || true"], check=False)
    items = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return sorted(set(items))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan and connect to Bluetooth GPS (macOS + blueutil)."
    )
    parser.add_argument(
        "--name",
        default="BT A+ GPS",
        help="Bluetooth device name to connect (default: %(default)s)",
    )
    parser.add_argument(
        "--scan-seconds",
        type=int,
        default=8,
        help="Active inquiry duration in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--connect-timeout",
        type=int,
        default=12,
        help="Seconds to wait for connection state (default: %(default)s)",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=None,
        help="Serial baud rate (default: auto-detect)",
    )
    parser.add_argument(
        "--port",
        default=None,
        help="Serial port to use (default: auto-detect)",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Do not stream NMEA data after connecting",
    )
    args = parser.parse_args()

    require_blueutil()
    if not args.no_stream:
        require_pyserial()

    print(f"Target device: {args.name}")
    print("Step 1/3: checking paired devices...")
    paired = blueutil_list_paired()
    target = find_target(paired, args.name)

    if target is None:
        print("Step 2/3: device not found in paired list, scanning nearby devices...")
        discovered = blueutil_inquiry(args.scan_seconds)
        target = find_target(discovered, args.name)

    if target is None:
        print("Device not found.")
        print("Tips:")
        print("- Turn on the GPS device and keep it near your Mac.")
        print("- Pair it first in macOS Bluetooth settings, then run this script again.")
        return 2

    print(f"Found: {target.name} ({target.address})")

    if is_connected(target.address):
        print("Already connected.")
    else:
        print("Step 3/3: connecting...")
        ok = connect_device(target.address, args.connect_timeout)
        if not ok:
            print("Connection failed or timed out.")
            return 3
        print("Connected successfully.")

    candidates = list_serial_candidates()
    if candidates:
        print("Possible serial ports:")
        for dev in candidates:
            print(f"- {dev}")
    else:
        print("No obvious serial BT GPS port found yet under /dev/cu.* or /dev/tty.*")
        print("Wait a few seconds and list devices with: ls /dev/cu.* /dev/tty.*")

    if not args.no_stream:
        port = args.port or best_serial_port(candidates)
        if not port:
            print("No serial port available to stream from.")
            return 4
        baud = args.baud
        if baud is None:
            print("Auto-detecting baud rate...")
            baud = detect_baud(port)
            if baud is None:
                print("Could not detect baud rate. Try --baud 4800 or --baud 9600.")
                return 5
        stream_nmea(port, baud)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
