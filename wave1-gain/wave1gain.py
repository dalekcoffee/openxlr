#!/usr/bin/python3 -I
"""wave1-gain: read and set the hardware gain of an Elgato Wave:1.

This program does one thing: it reads and writes the microphone's 16-byte
settings block over USB, and it only ever changes the gain (bytes 0 and 1)
or the target of the physical dial (byte 12). Every other byte is written
back exactly as the microphone reported it.

What it touches, in full:
  reads   /sys/bus/usb/devices/*/idVendor, idProduct, busnum, devnum
  opens   /dev/bus/usb/BBB/DDD of the one Wave:1 found (0fd9:006e)
  sends   two USB control requests and no others (see ALLOWED_REQUESTS)
It opens no network connection, starts no other program, writes no file
and needs no root: the udev rule beside it gives the logged-in user access
to this one microphone.

Protocol, as documented by OpenXLR (src/OpenXLR.Core/Devices/Wave3Device.cs
and Wave1Device.cs), which took it from public research on the Wave:3:
  read   bmRequestType 0xA1, bRequest 0x85, wValue 0, wIndex 0x3303, 16 bytes
  write  bmRequestType 0x21, bRequest 0x05, wValue 0, wIndex 0x3303, 16 bytes
  bytes 0..1   gain, little-endian Q8.8 dB, 0 to 40
  byte  12     dial target: 1 gain, 2 headphones, 3 monitor mix
OpenXLR confirmed gain writes on a real Wave:1. It never wrote byte 12, so
the `dial` command is the one part not yet confirmed on this microphone.
Vendor-type requests and other wValue numbers are never sent: on a Wave:3 a
scan with them rebooted the unit into firmware-update mode.
"""

import argparse
import ctypes
import errno
import fcntl
import os
import struct
import sys
import time

VENDOR_ID = 0x0FD9
PRODUCT_ID = 0x006E
INTERFACE = 3            # the control interface; no kernel driver binds it

RT_READ, REQ_READ = 0xA1, 0x85
RT_WRITE, REQ_WRITE = 0x21, 0x05
BLOCK_CONFIG = 0x0000
INDEX = 0x3303
CONFIG_LEN = 16
TIMEOUT_MS = 1000

OFF_GAIN = 0
OFF_DIAL = 12
DIAL_GAIN, DIAL_HEADPHONES, DIAL_MIX = 1, 2, 3
DIAL_NAMES = {DIAL_GAIN: "gain", DIAL_HEADPHONES: "headphones", DIAL_MIX: "monitor mix"}
GAIN_MIN_DB, GAIN_MAX_DB = 0, 40

# The only two transfers this program can send. Transport.control refuses
# anything else before it reaches the kernel.
ALLOWED_REQUESTS = frozenset({
    (RT_READ, REQ_READ, BLOCK_CONFIG, INDEX, CONFIG_LEN),
    (RT_WRITE, REQ_WRITE, BLOCK_CONFIG, INDEX, CONFIG_LEN),
})

SYSFS_USB = "/sys/bus/usb/devices"
DEV_USB = "/dev/bus/usb"


class Wave1Error(Exception):
    """A problem to report to the user; never a crash."""


class Wave1Busy(Wave1Error):
    """Another process holds the control interface for the moment."""


# ---------------------------------------------------------------- block logic
# Pure functions on the 16 bytes, so they can be tested without hardware.

def check_block(block):
    """Refuse anything that is not the layout this program knows."""
    if len(block) != CONFIG_LEN:
        raise Wave1Error(f"the microphone answered {len(block)} bytes, expected {CONFIG_LEN}; "
                         "refusing to write to an unknown layout")
    if block[OFF_DIAL] not in DIAL_NAMES:
        raise Wave1Error(f"dial byte reads {block[OFF_DIAL]}, expected 1, 2 or 3; "
                         "refusing to write to an unknown layout")


def gain_db(block):
    """The gain word as dB, rounded to a whole number."""
    raw = struct.unpack_from("<h", block, OFF_GAIN)[0]
    return round(raw / 256)


def dial_name(block):
    return DIAL_NAMES.get(block[OFF_DIAL], f"unknown ({block[OFF_DIAL]})")


def check_gain(db):
    if isinstance(db, bool) or not isinstance(db, int) or not GAIN_MIN_DB <= db <= GAIN_MAX_DB:
        raise Wave1Error(f"gain must be a whole number from {GAIN_MIN_DB} to {GAIN_MAX_DB} dB")


def with_gain(block, db):
    """A copy of the block with only the gain word changed."""
    check_gain(db)
    check_block(block)
    out = bytearray(block)
    struct.pack_into("<h", out, OFF_GAIN, db * 256)
    return bytes(out)


def with_dial(block, target):
    """A copy of the block with only the dial target changed."""
    if target not in (DIAL_GAIN, DIAL_HEADPHONES):
        raise Wave1Error("the Wave:1's dial can target gain or headphones only")
    check_block(block)
    out = bytearray(block)
    out[OFF_DIAL] = target
    return bytes(out)


def check_request(request_type, request, value, index, length):
    if (request_type, request, value, index, length) not in ALLOWED_REQUESTS:
        raise Wave1Error("refused a USB request outside the allowed list")


# ------------------------------------------------------------- USB transport

class _CtrlTransfer(ctypes.Structure):
    """struct usbdevfs_ctrltransfer from <linux/usbdevice_fs.h>."""
    _fields_ = [
        ("bRequestType", ctypes.c_uint8),
        ("bRequest", ctypes.c_uint8),
        ("wValue", ctypes.c_uint16),
        ("wIndex", ctypes.c_uint16),
        ("wLength", ctypes.c_uint16),
        ("timeout", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    ]


def _ioc(direction, number, size):
    # The generic Linux _IOC encoding (x86, ARM, RISC-V): dir 2 bits, size 14, type 8, nr 8.
    return (direction << 30) | (size << 16) | (ord("U") << 8) | number


_IOC_WRITE, _IOC_READ = 1, 2
USBDEVFS_CONTROL = _ioc(_IOC_READ | _IOC_WRITE, 0, ctypes.sizeof(_CtrlTransfer))
USBDEVFS_CLAIMINTERFACE = _ioc(_IOC_READ, 15, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_RELEASEINTERFACE = _ioc(_IOC_READ, 16, ctypes.sizeof(ctypes.c_uint))


def _read_attr(path):
    with open(path, encoding="ascii") as f:
        return f.read(64).strip()


def find_device(sysfs_root=SYSFS_USB):
    """(bus, address) of the single Wave:1 attached, or an error."""
    found = []
    try:
        entries = sorted(os.listdir(sysfs_root))
    except OSError as e:
        raise Wave1Error(f"cannot list {sysfs_root}: {e.strerror}") from None
    for name in entries:
        if ":" in name:          # an interface, not a device
            continue
        base = os.path.join(sysfs_root, name)
        try:
            if (int(_read_attr(os.path.join(base, "idVendor")), 16) != VENDOR_ID
                    or int(_read_attr(os.path.join(base, "idProduct")), 16) != PRODUCT_ID):
                continue
            bus = int(_read_attr(os.path.join(base, "busnum")))
            address = int(_read_attr(os.path.join(base, "devnum")))
        except (OSError, ValueError):
            continue
        if 1 <= bus <= 999 and 1 <= address <= 999:
            found.append((bus, address))
    if not found:
        raise Wave1Error("no Elgato Wave:1 (0fd9:006e) found; is it plugged in?")
    if len(found) > 1:
        raise Wave1Error("more than one Wave:1 is attached; unplug the others")
    return found[0]


def check_descriptor(descriptor):
    """The opened node really is a Wave:1 (guards against a replug between lookup and open)."""
    if len(descriptor) < 18 or descriptor[0] != 18 or descriptor[1] != 1:
        raise Wave1Error("the USB node did not return a device descriptor")
    vendor, product = struct.unpack_from("<HH", descriptor, 8)
    if (vendor, product) != (VENDOR_ID, PRODUCT_ID):
        raise Wave1Error(f"the USB node is {vendor:04x}:{product:04x}, not a Wave:1")


class Transport:
    """One open Wave:1 through the kernel's usbfs; no libusb, no helper process."""

    def __init__(self, bus, address, dev_root=DEV_USB):
        path = os.path.join(dev_root, f"{bus:03d}", f"{address:03d}")
        try:
            self._fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        except PermissionError:
            raise Wave1Error(f"no permission to open {path}; install the udev rule and replug") from None
        except OSError as e:
            raise Wave1Error(f"cannot open {path}: {e.strerror}") from None
        try:
            check_descriptor(os.read(self._fd, 18))
            fcntl.ioctl(self._fd, USBDEVFS_CLAIMINTERFACE, ctypes.c_uint(INTERFACE))
        except OSError as e:
            os.close(self._fd)
            if e.errno == errno.EBUSY:
                raise Wave1Busy("another program holds the microphone's control "
                                "interface (OpenXLR?); stop it first") from None
            raise Wave1Error(f"cannot claim the control interface: {e.strerror}") from None
        except Wave1Error:
            os.close(self._fd)
            raise

    def control(self, request_type, request, value, index, data, length):
        """One control transfer; returns the bytes read (or written)."""
        check_request(request_type, request, value, index, length)
        buffer = ctypes.create_string_buffer(bytes(data), length)
        transfer = _CtrlTransfer(request_type, request, value, index, length,
                                 TIMEOUT_MS, ctypes.cast(buffer, ctypes.c_void_p))
        try:
            n = fcntl.ioctl(self._fd, USBDEVFS_CONTROL, transfer)
        except OSError as e:
            raise Wave1Error(f"USB transfer failed: {e.strerror}") from None
        return buffer.raw[:n]

    def close(self):
        if self._fd >= 0:
            try:
                fcntl.ioctl(self._fd, USBDEVFS_RELEASEINTERFACE, ctypes.c_uint(INTERFACE))
            except OSError:
                pass
            os.close(self._fd)
            self._fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# -------------------------------------------------------------- the device

class Wave1:
    def __init__(self, transport):
        self._usb = transport

    def read(self, strict=True):
        """The block; strict=False lets `status` show an unexpected dial byte (writes stay strict)."""
        block = self._usb.control(RT_READ, REQ_READ, BLOCK_CONFIG, INDEX, bytes(CONFIG_LEN), CONFIG_LEN)
        if len(block) != CONFIG_LEN or strict:
            check_block(block)
        return block

    def _write_verified(self, new):
        written = self._usb.control(RT_WRITE, REQ_WRITE, BLOCK_CONFIG, INDEX, new, CONFIG_LEN)
        if len(written) != CONFIG_LEN:
            raise Wave1Error(f"the microphone accepted {len(written)} bytes, expected {CONFIG_LEN}")
        return self.read()

    def set_gain(self, db):
        new = with_gain(self.read(), db)
        back = self._write_verified(new)
        if back[OFF_GAIN:OFF_GAIN + 2] != new[OFF_GAIN:OFF_GAIN + 2]:
            raise Wave1Error(f"the microphone did not keep the gain (reads {gain_db(back)} dB)")
        return back

    def set_dial(self, target):
        new = with_dial(self.read(), target)
        back = self._write_verified(new)
        if back[OFF_DIAL] != target:
            raise Wave1Error(f"the microphone did not keep the dial target (reads {dial_name(back)})")
        return back


def changes(previous, block):
    """What a watcher should print for this read, or None."""
    now = (gain_db(block), dial_name(block))
    if previous is not None and (gain_db(previous), dial_name(previous)) == now:
        return None
    return f"gain {now[0]} dB  (dial: {now[1]})"


# --------------------------------------------------------------------- CLI

def _open(attempts=10):
    # `watch` holds the interface for a few milliseconds per read, so a
    # command run beside it may find it busy; wait for it briefly.
    for attempt in range(attempts):
        try:
            return Transport(*find_device())
        except Wave1Busy:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def cmd_status(args):
    with _open() as usb:
        block = Wave1(usb).read(strict=False)
    print(f"gain: {gain_db(block)} dB")
    print(f"dial: {dial_name(block)}")
    if args.raw:
        print(f"raw:  {block.hex(' ')}")


def cmd_set(args):
    check_gain(args.db)  # before any USB access
    with _open() as usb:
        block = Wave1(usb).set_gain(args.db)
    print(f"gain: {gain_db(block)} dB (read back from the microphone)")


def cmd_dial(args):
    target = DIAL_GAIN if args.target == "gain" else DIAL_HEADPHONES
    with _open() as usb:
        block = Wave1(usb).set_dial(target)
    print(f"dial: {dial_name(block)} (read back from the microphone)")


def cmd_watch(args):
    # Opens the device for each read and releases it straight after, so
    # `set` and `dial` keep working in another terminal while this runs.
    interval = max(0.1, args.interval)
    previous = None
    waiting = None
    try:
        while True:
            try:
                with _open() as usb:
                    block = Wave1(usb).read()
                waiting = None
                line = changes(previous, block)
                if line:
                    print(line, flush=True)
                previous = block
                time.sleep(interval)
            except Wave1Error as e:
                if str(e) != waiting:
                    print(f"waiting: {e}", file=sys.stderr, flush=True)
                    waiting = str(e)
                previous = None
                time.sleep(2)
    except KeyboardInterrupt:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wave1-gain", description="Elgato Wave:1 hardware gain.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("status", help="show the gain and the dial target")
    p.add_argument("--raw", action="store_true", help="also print the 16-byte block")
    p.set_defaults(run=cmd_status)
    p = sub.add_parser("set", help="set the hardware gain")
    p.add_argument("db", type=int, help=f"{GAIN_MIN_DB} to {GAIN_MAX_DB}")
    p.set_defaults(run=cmd_set)
    p = sub.add_parser("dial", help="choose what the physical dial controls")
    p.add_argument("target", choices=["gain", "headphones"])
    p.set_defaults(run=cmd_dial)
    p = sub.add_parser("watch", help="print the gain whenever it changes (Ctrl+C to stop)")
    p.add_argument("--interval", type=float, default=0.25, help="seconds between reads (min 0.1)")
    p.set_defaults(run=cmd_watch)
    args = parser.parse_args(argv)
    try:
        args.run(args)
    except Wave1Error as e:
        print(f"wave1-gain: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
