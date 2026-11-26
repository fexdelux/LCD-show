from pathlib import Path
import tempfile
import unittest

from framebuffer import FixInfo, Layout, VarInfo, candidates, draw_rgb, rgb565
from touch import TARGETS, apply_affine, fit_calibration, load_calibration, normalize, orient, save_calibration


class DisplayTests(unittest.TestCase):
    def test_primary_colors_in_both_byte_orders(self):
        channels = bytes((255, 0, 0, 0, 255, 0, 0, 0, 255))
        self.assertEqual(rgb565(channels, "little"), bytes.fromhex("00f8 e007 1f00"))
        self.assertEqual(rgb565(channels, "big"), bytes.fromhex("f800 07e0 001f"))

    def test_padded_stride_and_offsets(self):
        layout = Layout(2, 2, 10, 1, 1, 30)
        buf = bytearray(b"\xaa" * 30)
        draw_rgb(buf, layout, bytes((255, 0, 0)) * 4, "little")
        expected = bytes.fromhex("aaaa aaaa aaaa aaaa aaaa aaaa 00f8 00f8 aaaa aaaa aaaa 00f8 00f8 aaaa aaaa")
        self.assertEqual(buf, expected)

    def test_reject_bad_layout_without_writes(self):
        buf = bytearray(10)
        for layout, rgb in ((Layout(2, 1, 2, 0, 0, 10), b"\xff" * 6),
                            (Layout(2, 1, 4, 0, 3, 10), b"\xff" * 6),
                            (Layout(2, 1, 4, 0, 0, 10), b"\xff")):
            with self.assertRaises(ValueError):
                draw_rgb(buf, layout, rgb)
        self.assertEqual(buf, bytearray(10))

    def test_ioctl_format_validation(self):
        fix, var = FixInfo(), VarInfo()
        fix.visual, fix.smem_len, fix.line_length = 2, 64, 8
        var.xres = var.yres = var.xres_virtual = var.yres_virtual = 4
        var.bits_per_pixel = 16
        for field, offset, size in ((var.red, 11, 5), (var.green, 5, 6), (var.blue, 0, 5)):
            field.offset, field.length = offset, size
        self.assertEqual(Layout.from_info(fix, var), Layout(4, 4, 8, 0, 0, 64))
        var.red.msb_right = 1
        with self.assertRaisesRegex(ValueError, "Bitfields"):
            Layout.from_info(fix, var)
        var.bits_per_pixel = 32
        with self.assertRaisesRegex(ValueError, "Formato"):
            Layout.from_info(fix, var)

    def test_sysfs_does_not_assume_fb_number_or_accept_hdmi(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spi = root / "spi0.0"
            spi.mkdir()
            hdmi = root / "platform"
            hdmi.mkdir()
            devices = root / "dev"
            devices.mkdir()
            for name, driver, parent in (("fb0", "vc4drmfb", hdmi), ("fb3", "fb_ili9486", spi)):
                entry = root / name
                entry.mkdir()
                (entry / "name").write_text(driver)
                (entry / "device").symlink_to(parent)
                (devices / name).touch()
            self.assertEqual(candidates(root, devices), [devices / "fb3"])


class TouchTests(unittest.TestCase):
    def test_rotation_swap_and_inversion(self):
        self.assertEqual([orient(0, 0, angle) for angle in (0, 90, 180, 270)],
                         [(0, 0), (1, 0), (1, 1), (0, 1)])
        self.assertEqual(orient(0, 1, 180, swap=True, invert_x=True), (1, 1))
        self.assertEqual(normalize(600, (100, 1100)), 0.5)
        with self.assertRaises(ValueError):
            normalize(1, (1, 1))

    def test_five_point_transform_and_persistence(self):
        samples = [(1 - y * 0.8, x * 0.9 + 0.02) for x, y in TARGETS]
        matrix = fit_calibration(samples)
        for sample, target in zip(samples, TARGETS):
            for actual, expected in zip(apply_affine(matrix, sample), target):
                self.assertAlmostEqual(actual, expected)
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "touch.json"
            identity = {"name": "ADS7846", "rotation": 90}
            save_calibration(file, identity, matrix)
            self.assertEqual(load_calibration(file, identity), [list(row) for row in matrix])
            with self.assertRaises(ValueError):
                load_calibration(file, {**identity, "rotation": 0})

    def test_reject_invalid_corners_and_center(self):
        for samples in ([TARGETS[0]] * 5, list(TARGETS[:4]) + [(0, 0)]):
            with self.assertRaises(ValueError):
                fit_calibration(samples)


class DeviceIOTests(unittest.TestCase):
    def test_ioctl_and_mmap_against_fixture_file(self):
        from unittest.mock import patch
        from framebuffer import FBIOGET_FSCREENINFO, FBIOGET_VSCREENINFO, Framebuffer

        def ioctl(_fd, request, info):
            if request == FBIOGET_FSCREENINFO:
                info.visual, info.smem_len, info.line_length = 2, 16, 8
            else:
                self.assertEqual(request, FBIOGET_VSCREENINFO)
                info.xres, info.yres, info.xres_virtual, info.yres_virtual = 2, 1, 4, 2
                info.xoffset, info.yoffset, info.bits_per_pixel = 1, 1, 16
                for field, offset, size in ((info.red, 11, 5), (info.green, 5, 6), (info.blue, 0, 5)):
                    field.offset, field.length = offset, size

        with tempfile.TemporaryDirectory() as temp:
            fixture = Path(temp) / "framebuffer"
            fixture.write_bytes(b"\xaa" * 16)
            with patch("framebuffer.select_fb", return_value=fixture), patch("framebuffer.fcntl.ioctl", ioctl):
                with Framebuffer() as fb:
                    draw_rgb(fb.buffer, fb.layout, bytes((0, 255, 0)) * 2, "little")
            self.assertEqual(fixture.read_bytes(), b"\xaa" * 10 + bytes.fromhex("e007 e007 aaaa"))

    def test_touch_events_at_release_and_after_dropped_sync(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        from touch import Touch

        codes = SimpleNamespace(EV_SYN=0, EV_KEY=1, EV_ABS=3, SYN_REPORT=0, SYN_DROPPED=3,
                                ABS_X=0, ABS_Y=1, BTN_TOUCH=330)
        sequence = [
            (1, 330, 1), (3, 0, 20), (3, 1, 40), (0, 0, 0), (1, 330, 0), (0, 0, 0),
            (1, 330, 1), (0, 3, 0), (1, 330, 0), (0, 0, 0),
            (1, 330, 1), (3, 1, 70), (1, 330, 0), (0, 0, 0),
        ]
        device = SimpleNamespace(
            name="ADS7846", phys="spi0.1/input0",
            absinfo=lambda _: SimpleNamespace(min=0, max=100, value=25),
            read=lambda: (SimpleNamespace(type=kind, code=code, value=value)
                          for kind, code, value in sequence),
        )
        with patch.dict(sys.modules, {"evdev": SimpleNamespace(ecodes=codes)}):
            self.assertEqual(list(Touch(device).taps()), [(0.2, 0.4), (0.25, 0.7)])
