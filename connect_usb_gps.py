#!/usr/bin/env python3
"""Connect to a USB GPS receiver on macOS and stream NMEA sentences.

Detection order:
  1. Serial port matching USB-serial patterns (/dev/cu.usbmodem*, etc.)
  2. PyUSB bulk-transfer directly to the CDC device (fallback / --usb-direct)

USB device detected on this machine:
  USB Vendor Name : MTK
  USB Product Name: GPS Receiver
  idVendor        : 0x0E8D
  idProduct       : 0x3329
  bDeviceClass    : 0x02 (CDC)
"""

from __future__ import annotations

import argparse
import glob
import sys
import time

DEFAULT_VID = 0x0E8D  # MediaTek / MTK
DEFAULT_PID = 0x3329  # GPS Receiver

NMEA_PREFIXES = ("$GP", "$GN", "$GL", "$GA", "$GB", "$BD", "$QZ", "$II")
BAUD_CANDIDATES = (4800, 9600, 38400, 57600, 19200, 115200)


# ---------------------------------------------------------------------------
# Dependency guards
# ---------------------------------------------------------------------------

def require_pyserial() -> None:
    try:
        import serial  # noqa: F401
    except ImportError:
        print("pyserial is not installed.\nInstall with: pip install pyserial")
        sys.exit(1)


def require_pyusb() -> None:
    try:
        import usb.core  # noqa: F401
    except ImportError:
        print("pyusb is not installed.\nInstall with: pip install pyusb")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Serial-port helpers
# ---------------------------------------------------------------------------

def find_usb_serial_ports() -> list[str]:
    """Return /dev/cu.* devices that look like USB-to-serial adapters."""
    patterns = [
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
        "/dev/cu.SLAB_USBtoUART*",
        "/dev/cu.wchusbserial*",
        "/dev/cu.CH340*",
        "/dev/cu.PL2303*",
    ]
    ports: list[str] = []
    for pattern in patterns:
        ports.extend(glob.glob(pattern))
    return sorted(set(ports))


def _open_serial(port: str, baud: int) -> "serial.Serial":  # type: ignore[name-defined]
    import serial  # type: ignore[import]
    return serial.Serial(
        port,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        stopbits=serial.STOPBITS_ONE,
        parity=serial.PARITY_NONE,
        timeout=2.0,
    )


def detect_baud(port: str, probe_secs: int = 3) -> int | None:
    """Try common baud rates; return first that yields valid NMEA."""
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
                        print(f"NMEA detected at {baud} baud.")
                        return baud
        except Exception:
            pass
        print("no NMEA.")
    return None


def stream_nmea_serial(port: str, baud: int) -> None:
    print(f"Streaming NMEA from {port} at {baud} baud — Ctrl+C to stop.\n")
    try:
        with _open_serial(port, baud) as ser:
            while True:
                raw = ser.readline()
                if raw:
                    line = raw.decode("ascii", errors="replace").strip()
                    if line:
                        print(line)
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as exc:
        print(f"Serial error: {exc}")


# ---------------------------------------------------------------------------
# PyUSB bulk-transfer helpers (CDC ACM device)
# ---------------------------------------------------------------------------

def _cdc_acm_init(dev: "usb.core.Device", comm_intf_num: int, baud: int = 9600) -> None:  # type: ignore[name-defined]
    """Send CDC ACM SET_LINE_CODING + SET_CONTROL_LINE_STATE.

    Without these two control transfers many CDC ACM GPS devices stay silent
    even though the USB enumeration succeeded.
    """
    import struct
    import usb.core  # type: ignore[import]

    # SET_LINE_CODING: dwDTERate(4) bCharFormat(1) bParityType(1) bDataBits(1)
    #   bCharFormat=0 → 1 stop bit, bParityType=0 → none, bDataBits=8
    line_coding = struct.pack("<LBBB", baud, 0, 0, 8)
    try:
        dev.ctrl_transfer(
            bmRequestType=0x21,   # OUT | Class | Interface
            bRequest=0x20,        # SET_LINE_CODING
            wValue=0,
            wIndex=comm_intf_num,
            data_or_wLength=line_coding,
        )
        print(f"  SET_LINE_CODING  → {baud} baud, 8N1 (interface {comm_intf_num})")
    except usb.core.USBError as exc:
        print(f"  SET_LINE_CODING failed (non-fatal): {exc}")

    # SET_CONTROL_LINE_STATE: bit0=DTR, bit1=RTS  →  DTR=1, RTS=0
    try:
        dev.ctrl_transfer(
            bmRequestType=0x21,   # OUT | Class | Interface
            bRequest=0x22,        # SET_CONTROL_LINE_STATE
            wValue=0x01,          # DTR=1
            wIndex=comm_intf_num,
            data_or_wLength=None,
        )
        print(f"  SET_CONTROL_LINE_STATE → DTR=1 (interface {comm_intf_num})")
    except usb.core.USBError as exc:
        print(f"  SET_CONTROL_LINE_STATE failed (non-fatal): {exc}")


def stream_nmea_usb(vid: int, pid: int, read_size: int = 512) -> None:
    """Read NMEA sentences via PyUSB from a USB CDC GPS device."""
    import usb.core  # type: ignore[import]
    import usb.util  # type: ignore[import]

    print(f"Looking for USB device {vid:#06x}:{pid:#06x} ...")
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        print(
            f"USB device {vid:#06x}:{pid:#06x} not found.\n"
            "Make sure the GPS is plugged in and macOS has enumerated it."
        )
        sys.exit(1)

    try:
        manufacturer = dev.manufacturer or "?"
        product = dev.product or "?"
    except usb.core.USBError:
        manufacturer, product = "?", "?"
    print(f"Found: {manufacturer} — {product}")

    # Detach any kernel driver that may hold any interface
    for cfg in dev:
        for intf in cfg:
            try:
                if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                    dev.detach_kernel_driver(intf.bInterfaceNumber)
            except usb.core.USBError:
                pass

    dev.set_configuration()
    cfg = dev.get_active_configuration()

    # Scan interfaces: locate CDC Communication (0x02) and CDC Data (0x0A)
    comm_intf = None   # CDC Abstract Control Model — used for control transfers
    data_intf = None   # CDC Data — bulk IN lives here
    fallback_intf = None
    fallback_ep = None

    for intf in cfg:
        is_bulk_in = usb.util.find_descriptor(
            intf,
            find_all=False,
            custom_match=lambda e: (
                usb.util.endpoint_direction(e.bEndpointAddress)
                == usb.util.ENDPOINT_IN
                and usb.util.endpoint_type(e.bmAttributes)
                == usb.util.ENDPOINT_TYPE_BULK
            ),
        )
        if intf.bInterfaceClass == 0x02:   # CDC Communication
            comm_intf = intf
        elif intf.bInterfaceClass == 0x0A:  # CDC Data
            data_intf = intf
        elif is_bulk_in is not None and fallback_intf is None:
            fallback_intf = intf
            fallback_ep = is_bulk_in

    # Resolve which interface+endpoint to read from
    if data_intf is not None:
        ep_in = usb.util.find_descriptor(
            data_intf,
            find_all=False,
            custom_match=lambda e: (
                usb.util.endpoint_direction(e.bEndpointAddress)
                == usb.util.ENDPOINT_IN
                and usb.util.endpoint_type(e.bmAttributes)
                == usb.util.ENDPOINT_TYPE_BULK
            ),
        )
        read_intf = data_intf
    elif fallback_intf is not None:
        ep_in = fallback_ep
        read_intf = fallback_intf
    else:
        print("No bulk IN endpoint found on the USB device.")
        sys.exit(1)

    if ep_in is None:
        print("No bulk IN endpoint found on the CDC Data interface.")
        sys.exit(1)

    # Claim the communication interface (needed for control transfers)
    if comm_intf is not None:
        usb.util.claim_interface(dev, comm_intf)

    # Claim the data / read interface
    usb.util.claim_interface(dev, read_intf)

    # --- CDC ACM initialisation -------------------------------------------
    # GPS devices typically output NMEA at 9600 baud. Sending SET_LINE_CODING
    # + SET_CONTROL_LINE_STATE (DTR=1) tells the device to start the data flow.
    print("Initialising CDC ACM ...")
    init_intf_num = comm_intf.bInterfaceNumber if comm_intf else read_intf.bInterfaceNumber
    _cdc_acm_init(dev, init_intf_num, baud=9600)

    print(
        f"Reading from interface {read_intf.bInterfaceNumber}, "
        f"endpoint 0x{ep_in.bEndpointAddress:02x} — Ctrl+C to stop.\n"
    )

    buf = b""
    try:
        while True:
            try:
                chunk = dev.read(ep_in.bEndpointAddress, read_size, timeout=2000)
                buf += bytes(chunk)
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    line = line_bytes.decode("ascii", errors="replace").strip()
                    if line:
                        print(line)
            except usb.core.USBTimeoutError:
                pass
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        try:
            usb.util.release_interface(dev, read_intf)
            if comm_intf is not None:
                usb.util.release_interface(dev, comm_intf)
            usb.util.dispose_resources(dev)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Connect to a USB GPS receiver (MTK / CDC) on macOS "
            "and stream NMEA sentences."
        )
    )
    parser.add_argument(
        "--port",
        default=None,
        help="Serial port to use (default: auto-detect)",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=None,
        help="Serial baud rate (default: auto-detect)",
    )
    parser.add_argument(
        "--vid",
        type=lambda x: int(x, 0),
        default=DEFAULT_VID,
        help=f"USB Vendor ID (default: {DEFAULT_VID:#06x})",
    )
    parser.add_argument(
        "--pid",
        type=lambda x: int(x, 0),
        default=DEFAULT_PID,
        help=f"USB Product ID (default: {DEFAULT_PID:#06x})",
    )
    parser.add_argument(
        "--usb-direct",
        action="store_true",
        help="Skip serial-port detection; use PyUSB bulk transfer directly",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Detect device only; do not stream NMEA",
    )
    args = parser.parse_args()

    use_usb_direct: bool = args.usb_direct
    port: str | None = args.port

    if not use_usb_direct:
        require_pyserial()

        if port is None:
            print("Scanning for USB GPS serial ports...")
            candidates = find_usb_serial_ports()
            if candidates:
                print(f"Found: {', '.join(candidates)}")
                port = candidates[0]
            else:
                print(
                    "No USB serial port detected — "
                    "switching to PyUSB direct (bulk-transfer) mode."
                )
                use_usb_direct = True

    if use_usb_direct:
        require_pyusb()
        if not args.no_stream:
            stream_nmea_usb(args.vid, args.pid)
        return 0

    # ---- Serial path ----
    print(f"Using port: {port}")
    if args.no_stream:
        return 0

    baud = args.baud
    if baud is None:
        print("Auto-detecting baud rate...")
        baud = detect_baud(port)  # type: ignore[arg-type]
        if baud is None:
            print(
                "Could not detect baud rate on the serial port.\n"
                "Try: python connect_usb_gps.py --usb-direct\n"
                "  or specify a baud rate with --baud."
            )
            return 1

    stream_nmea_serial(port, baud)
    return 0


if __name__ == "__main__":
    sys.exit(main())
