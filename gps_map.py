#!/usr/bin/env python3
"""GPS web viewer — connect to USB GPS and show live position on OpenStreetMap.

Usage:
    python gps_map.py                  # auto-detect GPS, opens browser
    python gps_map.py --usb-direct     # force PyUSB bulk-transfer
    python gps_map.py --web-port 9000  # change web port (default: 8080)
    python gps_map.py --no-browser     # don't auto-open browser
"""

from __future__ import annotations

from typing import Any

import argparse
import glob
import json
import queue
import struct
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jinja2

DEFAULT_VID = 0x0E8D
DEFAULT_PID = 0x3329
DEFAULT_WEB_PORT = 8080

BAUD_CANDIDATES = (4800, 9600, 38400, 19200, 57600, 115200)
NMEA_PREFIXES = ("$GP", "$GN", "$GL", "$GA")

# ---------------------------------------------------------------------------
# Jinja2 template environment
# ---------------------------------------------------------------------------

_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=False,
)


# ---------------------------------------------------------------------------
# SSE broadcast (fan-out to all connected browser clients)
# ---------------------------------------------------------------------------

_clients: list[queue.Queue[dict]] = []
_clients_lock = threading.Lock()
_last_fix: dict | None = None


def broadcast(fix: dict) -> None:
    global _last_fix
    _last_fix = fix
    with _clients_lock:
        for q in list(_clients):
            try:
                q.put_nowait(fix)
            except queue.Full:
                pass


def _register() -> queue.Queue[dict]:
    q: queue.Queue[dict] = queue.Queue(maxsize=30)
    with _clients_lock:
        _clients.append(q)
    if _last_fix:
        try:
            q.put_nowait(_last_fix)
        except queue.Full:
            pass
    return q


def _unregister(q: queue.Queue[dict]) -> None:
    with _clients_lock:
        try:
            _clients.remove(q)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# NMEA line broadcast (raw sentences → browser log)
# ---------------------------------------------------------------------------

_nmea_clients: list[queue.Queue[str]] = []
_nmea_clients_lock = threading.Lock()


def broadcast_nmea(line: str) -> None:
    with _nmea_clients_lock:
        for q in list(_nmea_clients):
            try:
                q.put_nowait(line)
            except queue.Full:
                pass


def _register_nmea() -> queue.Queue[str]:
    q: queue.Queue[str] = queue.Queue(maxsize=50)
    with _nmea_clients_lock:
        _nmea_clients.append(q)
    return q


def _unregister_nmea(q: queue.Queue[str]) -> None:
    with _nmea_clients_lock:
        try:
            _nmea_clients.remove(q)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# State merging — combine GGA + RMC fields into one broadcast per fix
# ---------------------------------------------------------------------------

_current_state: dict = {}
_state_lock = threading.Lock()


def update_state(partial: dict) -> None:
    """Merge partial NMEA data into the running state and broadcast."""
    with _state_lock:
        _current_state.update(partial)
        if "lat" in _current_state and "lon" in _current_state:
            broadcast(dict(_current_state))


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            body = _jinja_env.get_template("index.html").render().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = _register()
            try:
                while True:
                    try:
                        data = q.get(timeout=20)
                        self.wfile.write(
                            f"data: {json.dumps(data)}\n\n".encode()
                        )
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                _unregister(q)

        elif self.path == "/nmea":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q_nmea = _register_nmea()
            try:
                while True:
                    try:
                        line = q_nmea.get(timeout=20)
                        self.wfile.write(f"data: {line}\n\n".encode())
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                _unregister_nmea(q_nmea)

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # silence HTTP access logs


# ---------------------------------------------------------------------------
# NMEA parsing
# ---------------------------------------------------------------------------

_gsv_accum: dict[int, dict] = {}


def _update_gsv(fields: list[str]) -> dict | None:
    """Accumulate satellite data from $GPGSV/$GNGSV sentences."""
    global _gsv_accum
    try:
        msg_num = int(fields[2])
        sv_total = int(fields[3]) if fields[3] else 0
        if msg_num == 1:
            _gsv_accum = {}
        i = 4
        while i + 4 <= len(fields):
            prn_s = fields[i]
            if prn_s:
                try:
                    _gsv_accum[int(prn_s)] = {
                        "p":  int(prn_s),
                        "el": int(fields[i + 1]) if fields[i + 1] else 0,
                        "az": int(fields[i + 2]) if fields[i + 2] else 0,
                        "s":  int(fields[i + 3]) if fields[i + 3] else 0,
                    }
                except ValueError:
                    pass
            i += 4
        return {
            "sv": sorted(_gsv_accum.values(), key=lambda x: -x["s"]),
            "sv_total": sv_total,
        }
    except (ValueError, IndexError):
        return None


def _latlon(deg_min: str, hemi: str) -> float | None:
    if not deg_min:
        return None
    try:
        dot = deg_min.index(".")
        deg = int(deg_min[: dot - 2])
        minutes = float(deg_min[dot - 2:])
        val = deg + minutes / 60.0
        if hemi in ("S", "W"):
            val = -val
        return val
    except (ValueError, IndexError):
        return None


def parse_nmea(line: str) -> dict | None:
    """Return a partial state dict from a GGA or RMC sentence, or None."""
    if "*" in line:
        line = line[: line.index("*")]
    if not line.startswith("$") or len(line) < 6:
        return None
    fields = line.split(",")
    stype = fields[0][3:]  # e.g. "GGA" from "$GPGGA" or "$GNGGA"

    if stype == "GGA" and len(fields) >= 10:
        if fields[6] == "0":
            return None  # no fix
        lat = _latlon(fields[2], fields[3])
        lon = _latlon(fields[4], fields[5])
        if lat is None or lon is None:
            return None
        fix: dict = {"lat": lat, "lon": lon}
        if fields[1]:
            fix["time"] = fields[1]        # hhmmss.ss UTC
        if fields[6]:
            fix["fix"] = fields[6]
        if fields[7]:
            fix["sats"] = fields[7]
        try:
            fix["hdop"] = round(float(fields[8]), 1)
        except (ValueError, IndexError):
            pass
        try:
            fix["alt"] = round(float(fields[9]), 1)
        except (ValueError, IndexError):
            pass
        return fix

    if stype == "RMC" and len(fields) >= 7:
        if fields[2] != "A":
            return None  # void / no fix
        lat = _latlon(fields[3], fields[4])
        lon = _latlon(fields[5], fields[6])
        if lat is None or lon is None:
            return None
        fix = {"lat": lat, "lon": lon}
        if fields[1]:
            fix["time"] = fields[1]        # hhmmss.ss UTC
        try:
            fix["spd_kmh"] = round(float(fields[7]) * 1.852, 1)
        except (ValueError, IndexError):
            pass
        try:
            fix["cog"] = round(float(fields[8]), 1)
        except (ValueError, IndexError):
            pass
        if len(fields) > 9 and fields[9]:
            fix["date"] = fields[9]        # ddmmyy
        return fix

    if stype == "GST" and len(fields) >= 9:
        # $GPGST gives actual RMS position error in metres
        try:
            lat_err = float(fields[6])
            lon_err = float(fields[7])
            return {"hacc": round((lat_err ** 2 + lon_err ** 2) ** 0.5, 1)}
        except (ValueError, IndexError):
            pass
        return None

    if stype == "GSV":
        return _update_gsv(fields)

    return None


# ---------------------------------------------------------------------------
# GPS thread — serial port
# ---------------------------------------------------------------------------

def _find_usb_serial_ports() -> list[str]:
    patterns = [
        "/dev/cu.usbmodem*", "/dev/cu.usbserial*",
        "/dev/cu.SLAB_USBtoUART*", "/dev/cu.wchusbserial*",
        "/dev/cu.CH340*", "/dev/cu.PL2303*",
    ]
    ports: list[str] = []
    for p in patterns:
        ports.extend(glob.glob(p))
    return sorted(set(ports))


def _open_serial(port: str, baud: int) -> "serial.Serial":  # type: ignore[name-defined]
    import serial  # type: ignore[import]
    return serial.Serial(
        port, baudrate=baud,
        bytesize=serial.EIGHTBITS,
        stopbits=serial.STOPBITS_ONE,
        parity=serial.PARITY_NONE,
        timeout=2.0,
    )


def _detect_baud(port: str) -> int | None:
    for baud in BAUD_CANDIDATES:
        try:
            with _open_serial(port, baud) as ser:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    line = ser.readline().decode("ascii", errors="replace").strip()
                    if any(line.startswith(p) for p in NMEA_PREFIXES):
                        return baud
        except Exception:
            pass
    return None


def gps_thread_serial(port: str, baud: int | None) -> None:
    if baud is None:
        print(f"  Auto-detecting baud on {port}...")
        baud = _detect_baud(port)
        if baud is None:
            print("  Could not detect baud rate.")
            return
        print(f"  Baud: {baud}")
    print(f"GPS (serial) — {port} @ {baud} baud")
    try:
        with _open_serial(port, baud) as ser:
            while True:
                raw = ser.readline()
                if raw:
                    line = raw.decode("ascii", errors="replace").strip()
                    if any(line.startswith(p) for p in NMEA_PREFIXES):
                        broadcast_nmea(line)
                    fix = parse_nmea(line)
                    if fix:
                        update_state(fix)
    except Exception as exc:
        print(f"Serial GPS error: {exc}")


# ---------------------------------------------------------------------------
# GPS thread — PyUSB bulk-transfer (CDC ACM)
# ---------------------------------------------------------------------------

def _cdc_acm_init(dev: object, comm_intf_num: int, baud: int = 9600) -> None:
    import usb.core  # type: ignore[import]

    line_coding = struct.pack("<LBBB", baud, 0, 0, 8)
    try:
        dev.ctrl_transfer(0x21, 0x20, 0, comm_intf_num, line_coding)  # type: ignore[attr-defined]
        print(f"  SET_LINE_CODING  → {baud} baud, 8N1")
    except usb.core.USBError as exc:
        print(f"  SET_LINE_CODING failed (non-fatal): {exc}")
    try:
        dev.ctrl_transfer(0x21, 0x22, 0x01, comm_intf_num, None)  # type: ignore[attr-defined]
        print("  SET_CONTROL_LINE_STATE → DTR=1")
    except usb.core.USBError as exc:
        print(f"  SET_CONTROL_LINE_STATE failed (non-fatal): {exc}")


def gps_thread_usb(vid: int, pid: int) -> None:
    import usb.core  # type: ignore[import]
    import usb.util  # type: ignore[import]

    print(f"Looking for USB device {vid:#06x}:{pid:#06x} ...")
    dev: Any = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        print(f"  USB device {vid:#06x}:{pid:#06x} not found.")
        return
    try:
        manufacturer = dev.manufacturer or "?"  # type: ignore[union-attr]
        product = dev.product or "?"  # type: ignore[union-attr]
        print(f"Found: {manufacturer} — {product}")
    except usb.core.USBError:
        print("Found: (USB strings unavailable)")
    for cfg in dev:
        for intf in cfg:
            try:
                if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                    dev.detach_kernel_driver(intf.bInterfaceNumber)
            except usb.core.USBError:
                pass

    dev.set_configuration()
    cfg = dev.get_active_configuration()

    def is_bulk_in(e: object) -> bool:
        return (
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN  # type: ignore[attr-defined]
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK  # type: ignore[attr-defined]
        )

    comm_intf = None
    data_intf = None
    fallback_intf = None
    fallback_ep = None

    for intf in cfg:
        ep = usb.util.find_descriptor(intf, find_all=False, custom_match=is_bulk_in)
        if intf.bInterfaceClass == 0x02:
            comm_intf = intf
        elif intf.bInterfaceClass == 0x0A:
            data_intf = intf
        elif ep is not None and fallback_intf is None:
            fallback_intf, fallback_ep = intf, ep

    read_intf = data_intf or fallback_intf
    if read_intf is None:
        print("  No suitable USB data interface found.")
        return

    ep_in: Any = usb.util.find_descriptor(read_intf, find_all=False, custom_match=is_bulk_in)
    if ep_in is None:
        ep_in = fallback_ep
    if ep_in is None:
        print("  No bulk IN endpoint found.")
        return

    if comm_intf is not None:
        usb.util.claim_interface(dev, comm_intf)
    usb.util.claim_interface(dev, read_intf)

    print("Initialising CDC ACM ...")
    init_num = (comm_intf or read_intf).bInterfaceNumber
    _cdc_acm_init(dev, init_num)

    print(
        f"GPS (USB) — interface {read_intf.bInterfaceNumber}, "
        f"endpoint 0x{ep_in.bEndpointAddress:02x}\n"
    )

    buf = b""
    try:
        while True:
            try:
                chunk = dev.read(ep_in.bEndpointAddress, 512, timeout=2000)
                buf += bytes(chunk)
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    line = line_bytes.decode("ascii", errors="replace").strip()
                    if any(line.startswith(p) for p in NMEA_PREFIXES):
                        broadcast_nmea(line)
                    fix = parse_nmea(line)
                    if fix:
                        update_state(fix)
                        print(line)
            except usb.core.USBTimeoutError:
                pass
    except Exception as exc:
        print(f"USB GPS error: {exc}")
    finally:
        try:
            usb.util.release_interface(dev, read_intf)
            if comm_intf:
                usb.util.release_interface(dev, comm_intf)
            usb.util.dispose_resources(dev)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Connect to USB GPS and show live position on OpenStreetMap."
    )
    parser.add_argument("--port", default=None, help="Serial port (default: auto-detect)")
    parser.add_argument("--baud", type=int, default=None, help="Baud rate (default: auto)")
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=DEFAULT_VID,
                        help=f"USB Vendor ID (default: {DEFAULT_VID:#06x})")
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=DEFAULT_PID,
                        help=f"USB Product ID (default: {DEFAULT_PID:#06x})")
    parser.add_argument("--usb-direct", action="store_true",
                        help="Force PyUSB bulk-transfer mode")
    parser.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT, metavar="PORT",
                        help=f"HTTP port (default: {DEFAULT_WEB_PORT})")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't open the browser automatically")
    args = parser.parse_args()

    use_usb = args.usb_direct
    port = args.port

    if not use_usb:
        try:
            import serial  # noqa: F401
        except ImportError:
            print("pyserial not installed — using PyUSB direct mode.")
            use_usb = True

    if not use_usb and port is None:
        print("Scanning for USB GPS serial ports...")
        candidates = _find_usb_serial_ports()
        if candidates:
            port = candidates[0]
            print(f"Found: {port}")
        else:
            print("No USB serial port found — switching to PyUSB direct mode.")
            use_usb = True

    if use_usb:
        try:
            import usb.core  # type: ignore[import]  # noqa: F401
        except ImportError:
            print("pyusb not installed.\nInstall with: pip install pyusb")
            return 1
        gps = threading.Thread(
            target=gps_thread_usb, args=(args.vid, args.pid), daemon=True
        )
    else:
        gps = threading.Thread(
            target=gps_thread_serial, args=(port, args.baud), daemon=True
        )
    gps.start()

    url = f"http://localhost:{args.web_port}"
    print(f"\nMap: {url}")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server = ThreadingHTTPServer(("", args.web_port), Handler)
        print(f"Web server running on port {args.web_port} — Ctrl+C to stop.\n")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
