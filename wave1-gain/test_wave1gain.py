"""Tests for wave1gain.py. No hardware needed: python3 -I test_wave1gain.py -v"""

import ast
import contextlib
import ctypes
import io
import os
import struct
import sys
import tempfile
import unittest

# python3 -I leaves the script's folder off the import path; add this one only.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wave1gain as w  # noqa: E402

# The block an owner's Wave:1 returned to OpenXLR (docs/hardware-support.md):
# gain 11 dB, ClipGuard on, headphones 0 dB, dial on headphones.
OWNER_BLOCK = bytes.fromhex("000b ec00 0001 0000 0000 0000 0200 0000".replace(" ", ""))


class FakeUsb:
    """Holds a block like the microphone; records every request."""

    def __init__(self, block=OWNER_BLOCK, keep=True, short_write=False):
        self.block = bytearray(block)
        self.keep = keep
        self.short_write = short_write
        self.requests = []

    def control(self, request_type, request, value, index, data, length):
        w.check_request(request_type, request, value, index, length)
        self.requests.append((request_type, request, value, index, length, bytes(data)))
        if request_type == w.RT_READ:
            return bytes(self.block)
        if self.keep:
            self.block[:] = data
        return bytes(data[:8]) if self.short_write else bytes(data)


class BlockTests(unittest.TestCase):
    def test_owner_block_decodes(self):
        self.assertEqual(w.gain_db(OWNER_BLOCK), 11)
        self.assertEqual(w.dial_name(OWNER_BLOCK), "headphones")

    def test_with_gain_changes_only_bytes_0_and_1(self):
        new = w.with_gain(OWNER_BLOCK, 5)
        self.assertEqual(new[:2], struct.pack("<h", 5 * 256))
        self.assertEqual(new[2:], OWNER_BLOCK[2:])

    def test_gain_bounds(self):
        for bad in (-1, 41, 1000, 2.5, "10", True, None):
            with self.assertRaises(w.Wave1Error, msg=repr(bad)):
                w.with_gain(OWNER_BLOCK, bad)
        self.assertEqual(w.gain_db(w.with_gain(OWNER_BLOCK, 0)), 0)
        self.assertEqual(w.gain_db(w.with_gain(OWNER_BLOCK, 40)), 40)

    def test_with_dial_changes_only_byte_12(self):
        new = w.with_dial(OWNER_BLOCK, w.DIAL_GAIN)
        self.assertEqual(new[12], 1)
        self.assertEqual(new[:12] + new[13:], OWNER_BLOCK[:12] + OWNER_BLOCK[13:])

    def test_dial_refuses_monitor_mix_and_junk(self):
        for bad in (0, 3, 4, 255):
            with self.assertRaises(w.Wave1Error):
                w.with_dial(OWNER_BLOCK, bad)

    def test_unknown_layout_refused(self):
        with self.assertRaises(w.Wave1Error):
            w.check_block(OWNER_BLOCK[:15])
        with self.assertRaises(w.Wave1Error):
            w.check_block(OWNER_BLOCK + b"\0")
        odd = bytearray(OWNER_BLOCK)
        odd[12] = 9
        with self.assertRaises(w.Wave1Error):
            w.with_gain(bytes(odd), 10)


class RequestAllowlistTests(unittest.TestCase):
    def test_only_two_requests_exist(self):
        self.assertEqual(w.ALLOWED_REQUESTS, {
            (0xA1, 0x85, 0x0000, 0x3303, 16),
            (0x21, 0x05, 0x0000, 0x3303, 16),
        })

    def test_everything_else_refused(self):
        refused = [
            (0xC1, 0x85, 0, 0x3303, 16),   # vendor read: rebooted a Wave:3 into DFU
            (0x41, 0x05, 0, 0x3303, 16),   # vendor write
            (0xA1, 0x85, 0x000A, 0x3303, 64),  # device info block
            (0x21, 0x05, 0x0001, 0x3303, 16),  # other block number
            (0x21, 0x05, 0, 0x3304, 16),   # other entity
            (0x21, 0x05, 0, 0x3303, 15),   # other length
            (0x00, 0x09, 1, 0, 0),         # SET_CONFIGURATION
        ]
        for request in refused:
            with self.assertRaises(w.Wave1Error, msg=repr(request)):
                w.check_request(*request)


class DeviceTests(unittest.TestCase):
    def test_set_gain_reads_writes_verifies(self):
        usb = FakeUsb()
        back = w.Wave1(usb).set_gain(5)
        self.assertEqual(w.gain_db(back), 5)
        kinds = [r[0] for r in usb.requests]
        self.assertEqual(kinds, [0xA1, 0x21, 0xA1])
        self.assertEqual(usb.requests[1][5][2:], OWNER_BLOCK[2:])

    def test_dropped_write_reported(self):
        with self.assertRaisesRegex(w.Wave1Error, "did not keep the gain"):
            w.Wave1(FakeUsb(keep=False)).set_gain(5)
        with self.assertRaisesRegex(w.Wave1Error, "did not keep the dial"):
            w.Wave1(FakeUsb(keep=False)).set_dial(w.DIAL_GAIN)

    def test_short_write_reported(self):
        with self.assertRaisesRegex(w.Wave1Error, "accepted 8 bytes"):
            w.Wave1(FakeUsb(short_write=True)).set_gain(5)

    def test_nothing_written_after_bad_read(self):
        usb = FakeUsb(block=OWNER_BLOCK[:10])
        with self.assertRaises(w.Wave1Error):
            w.Wave1(usb).set_gain(5)
        self.assertTrue(all(r[0] == w.RT_READ for r in usb.requests))

    def test_status_shows_unknown_dial_but_writes_refuse(self):
        odd = bytearray(OWNER_BLOCK)
        odd[12] = 7
        usb = FakeUsb(block=bytes(odd))
        self.assertEqual(w.dial_name(w.Wave1(usb).read(strict=False)), "unknown (7)")
        with self.assertRaises(w.Wave1Error):
            w.Wave1(usb).set_gain(5)
        self.assertTrue(all(r[0] == w.RT_READ for r in usb.requests))
        with self.assertRaises(w.Wave1Error):
            w.Wave1(FakeUsb(block=OWNER_BLOCK[:12])).read(strict=False)

    def test_write_buffer_is_built(self):
        import ctypes as c
        self.assertEqual(c.create_string_buffer(OWNER_BLOCK, 16).raw, OWNER_BLOCK)

    def test_set_dial(self):
        usb = FakeUsb()
        back = w.Wave1(usb).set_dial(w.DIAL_GAIN)
        self.assertEqual(w.dial_name(back), "gain")
        self.assertEqual(w.gain_db(back), 11)

    def test_changes(self):
        self.assertIn("11 dB", w.changes(None, OWNER_BLOCK))
        self.assertIsNone(w.changes(OWNER_BLOCK, OWNER_BLOCK))
        self.assertIn("20 dB", w.changes(OWNER_BLOCK, w.with_gain(OWNER_BLOCK, 20)))
        self.assertIn("gain)", w.changes(OWNER_BLOCK, w.with_dial(OWNER_BLOCK, 1)))


class DiscoveryTests(unittest.TestCase):
    def make(self, root, name, vendor, product, bus=1, dev=5):
        d = os.path.join(root, name)
        os.makedirs(d)
        for attr, value in (("idVendor", vendor), ("idProduct", product), ("busnum", bus), ("devnum", dev)):
            with open(os.path.join(d, attr), "w") as f:
                f.write(f"{value}\n")

    def test_finds_one(self):
        with tempfile.TemporaryDirectory() as root:
            self.make(root, "1-1", "0fd9", "0070", 1, 4)      # a Wave:3 is ignored
            self.make(root, "1-2", "0fd9", "006e", 1, 7)
            self.make(root, "1-2:1.3", "0fd9", "006e")         # interface entries ignored
            self.assertEqual(w.find_device(root), (1, 7))

    def test_none_and_many(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(w.Wave1Error, "no Elgato"):
                w.find_device(root)
            self.make(root, "1-1", "0fd9", "006e", 1, 3)
            self.make(root, "2-1", "0fd9", "006e", 2, 3)
            with self.assertRaisesRegex(w.Wave1Error, "more than one"):
                w.find_device(root)

    def test_descriptor_check(self):
        good = bytes([18, 1]) + bytes(6) + struct.pack("<HH", 0x0FD9, 0x006E) + bytes(6)
        w.check_descriptor(good)
        other = bytes([18, 1]) + bytes(6) + struct.pack("<HH", 0x0FD9, 0x0070) + bytes(6)
        for bad in (other, good[:10], b"\x09\x02" + good[2:]):
            with self.assertRaises(w.Wave1Error):
                w.check_descriptor(bad)


class KernelAbiTests(unittest.TestCase):
    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "values below are for 64-bit Linux")
    def test_ioctl_numbers_match_linux_headers(self):
        # From <linux/usbdevice_fs.h>, compiled on x86_64.
        self.assertEqual(ctypes.sizeof(w._CtrlTransfer), 24)
        self.assertEqual(w._CtrlTransfer.data.offset, 16)
        self.assertEqual(w.USBDEVFS_CONTROL, 0xC0185500)
        self.assertEqual(w.USBDEVFS_CLAIMINTERFACE, 0x8004550F)
        self.assertEqual(w.USBDEVFS_RELEASEINTERFACE, 0x80045510)


class CliTests(unittest.TestCase):
    def test_bad_gain_rejected_before_touching_usb(self):
        opened = []
        original = w._open
        w._open = lambda *a: opened.append(1)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(w.main(["set", "41"]), 1)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                w.main(["set", "loud"])
        finally:
            w._open = original
        self.assertEqual(opened, [])


class SurfaceTests(unittest.TestCase):
    """Fails if someone adds a network, process or file-writing capability."""

    ALLOWED_IMPORTS = {"argparse", "ctypes", "errno", "fcntl", "os", "struct", "sys", "time"}
    FORBIDDEN_NAMES = {"system", "popen", "spawn", "exec", "eval", "compile", "__import__",
                       "socket", "subprocess", "urllib", "http", "requests", "fork", "remove",
                       "unlink", "rename", "replace", "makedirs", "mkdir", "chmod", "chown", "CDLL"}

    def tree(self):
        with open(os.path.join(HERE, "wave1gain.py"), encoding="utf-8") as f:
            return ast.parse(f.read())

    def test_imports(self):
        names = set()
        for node in ast.walk(self.tree()):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
        self.assertEqual(names, self.ALLOWED_IMPORTS)

    def test_no_dangerous_calls(self):
        used = set()
        for node in ast.walk(self.tree()):
            if isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Name):
                used.add(node.id)
        self.assertEqual(used & self.FORBIDDEN_NAMES, set())

    def test_files_opened_read_only_except_usb_node(self):
        opens = [n for n in ast.walk(self.tree())
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open"]
        for call in opens:
            mode = call.args[1].value if len(call.args) > 1 else "r"
            self.assertEqual(mode, "r")


if __name__ == "__main__":
    unittest.main()
