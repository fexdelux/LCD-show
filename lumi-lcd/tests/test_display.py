from pathlib import Path
import tempfile
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
            driver = root / "fb_ili9486"
            driver.mkdir()
            (spi / "driver").symlink_to(driver)
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
                info.id = b"fb_ili9486"
            else:
                self.assertEqual(request, FBIOGET_VSCREENINFO)
                info.xres, info.yres, info.xres_virtual, info.yres_virtual = 2, 1, 4, 2
                info.xoffset, info.yoffset, info.bits_per_pixel = 1, 1, 16
                for field, offset, size in ((info.red, 11, 5), (info.green, 5, 6), (info.blue, 0, 5)):
                    field.offset, field.length = offset, size

        with tempfile.TemporaryDirectory() as temp:
            fixture = Path(temp) / "framebuffer"
            fixture.write_bytes(b"\xaa" * 16)
            with patch("framebuffer.select_fb", return_value=fixture), \
                    patch("framebuffer.verify_write_target"), patch("framebuffer.fcntl.ioctl", ioctl):
                with Framebuffer() as fb:
                    draw_rgb(fb.buffer, fb.layout, bytes((0, 255, 0)) * 2, "little")
            self.assertEqual(fixture.read_bytes(), b"\xaa" * 10 + bytes.fromhex("e007 e007 aaaa"))

    def test_touch_events_at_release_and_after_dropped_sync(self):
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

    def test_fbtft_nonstd_flag_requires_known_id_and_16_bit_fields(self):
        fix, var = FixInfo(), VarInfo()
        fix.id, fix.visual = b"fb_ili9486", 2
        var.bits_per_pixel, var.nonstd = 16, 1
        for field, offset, size in ((var.red, 11, 5), (var.green, 5, 6), (var.blue, 0, 5)):
            field.offset, field.length = offset, size
        for width, height in ((480, 320), (320, 480)):
            var.xres = var.xres_virtual = width
            var.yres = var.yres_virtual = height
            fix.line_length = width * 2 + 8
            fix.smem_len = fix.line_length * height
            self.assertEqual(Layout.from_info(fix, var).width, width)
        var.xoffset = 1
        with self.assertRaisesRegex(ValueError, "limites virtuais"):
            Layout.from_info(fix, var)
        var.xoffset = 0
        fix.id = b"vc4drmfb"
        with self.assertRaisesRegex(ValueError, "Formato"):
            Layout.from_info(fix, var)

    def test_no_spi_framebuffer_or_ambiguous_selection(self):
        from unittest.mock import patch
        from framebuffer import Framebuffer

        for devices, message in (([], "Nenhum"), ([Path("/dev/fb1"), Path("/dev/fb2")], "Vários")):
            with patch("framebuffer.candidates", return_value=devices):
                with self.assertRaisesRegex(ValueError, message):
                    Framebuffer()

    def test_explicit_hdmi_rejected_before_ioctl_or_mapping(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from framebuffer import Framebuffer
        import stat

        with patch("framebuffer.os.open", return_value=123), patch("framebuffer.os.close") as close, \
                patch("framebuffer.os.fstat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR)), \
                patch("framebuffer.spi_panel", return_value=False), patch("framebuffer.fcntl.ioctl") as ioctl, \
                patch("framebuffer.mmap.mmap") as mapping:
            with self.assertRaisesRegex(ValueError, "SPI ILI9486"):
                with Framebuffer("/dev/fb0"):
                    self.fail("HDMI opened for drawing")
            close.assert_called_once_with(123)
            ioctl.assert_not_called()
            mapping.assert_not_called()

    def test_framebuffer_permission_error_names_device(self):
        from unittest.mock import patch
        from framebuffer import Framebuffer

        with patch("framebuffer.os.open", side_effect=PermissionError):
            with self.assertRaisesRegex(PermissionError, "/dev/fb4"):
                with Framebuffer("/dev/fb4"):
                    self.fail("permission denied")

    def test_touch_spi_capabilities_exclude_usb_and_incomplete_devices(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from touch import open_touch

        codes = SimpleNamespace(EV_KEY=1, EV_ABS=3, ABS_X=0, ABS_Y=1, BTN_TOUCH=330, BUS_SPI=28)
        correct = {3: [0, 1], 1: [330]}
        usb = Mock(name="usb", info=SimpleNamespace(bustype=3))
        usb.name = "ADS7846 Touchscreen"
        usb.capabilities.return_value = correct
        incomplete = Mock(info=SimpleNamespace(bustype=28))
        incomplete.name = "ADS7846 Touchscreen"
        incomplete.capabilities.return_value = {3: [0, 1]}
        spi = Mock(info=SimpleNamespace(bustype=28))
        spi.name = "ADS7846 Touchscreen"
        spi.capabilities.return_value = correct
        evdev = SimpleNamespace(ecodes=codes, InputDevice=Mock(side_effect=[usb, incomplete, spi]))
        with patch.dict(sys.modules, {"evdev": evdev}), patch("touch.touch_paths", return_value=["a", "b", "c"]):
            self.assertIs(open_touch(), spi)
        usb.close.assert_called_once()
        incomplete.close.assert_called_once()
        spi.close.assert_not_called()

    def test_touch_permission_and_disconnect_are_actionable(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from touch import Touch, open_touch

        codes = SimpleNamespace(ABS_X=0, ABS_Y=1)
        evdev = SimpleNamespace(ecodes=codes, InputDevice=Mock(side_effect=PermissionError))
        device = SimpleNamespace(name="ADS7846 Touchscreen", phys="spi0.1/input0",
                                 absinfo=lambda axis: SimpleNamespace(min=0, max=4095, value=2),
                                 read=lambda: [])
        with patch.dict(sys.modules, {"evdev": evdev}):
            with self.assertRaisesRegex(PermissionError, "/dev/input/event2"):
                open_touch("/dev/input/event2")
            with self.assertRaisesRegex(OSError, "desconectado"):
                list(Touch(device).taps())

    def test_demo_coalesces_updates_and_blocks_when_static(self):
        from contextlib import ExitStack
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        import demo

        args = SimpleNamespace(fb=None, without_touch=False, touch=None, rotate=90,
                               calibrate=False, calibration=None, swap_axes=False,
                               invert_x=False, invert_y=False, state="aguardando")
        fb = Mock()
        fb.__enter__ = Mock(return_value=SimpleNamespace(layout=Layout(480, 320, 960, 0, 0, 307200)))
        fb.__exit__ = Mock()
        device = Mock(fd=4)
        touch = Mock(identity={})
        touch.taps.side_effect = [[(0.2, 0.3)], [(0.4, 0.5)]]
        selector = Mock()
        selector.__enter__ = Mock(return_value=selector)
        selector.__exit__ = Mock()
        selector.select.side_effect = [True, True, False, KeyboardInterrupt]
        times = [0, 0.01, 0.02, 0.03, 0.04, demo.FRAME_INTERVAL]
        with ExitStack() as resources:
            resources.enter_context(patch("demo.os.geteuid", return_value=1000))
            resources.enter_context(patch("demo.Framebuffer", return_value=fb))
            resources.enter_context(patch("demo.open_touch", return_value=device))
            resources.enter_context(patch("demo.Touch", return_value=touch))
            resources.enter_context(patch("demo.DefaultSelector", return_value=selector))
            resources.enter_context(patch("demo.monotonic", side_effect=times))
            draw = resources.enter_context(patch("demo.draw"))
            with self.assertRaises(KeyboardInterrupt):
                demo.run(args, resources)
            self.assertEqual([call.args[1] for call in draw.call_args_list], ["aguardando", "processando"])
            timeouts = [call.args[0] for call in selector.select.call_args_list]
            self.assertIsNone(timeouts[0])
            self.assertGreater(timeouts[1], 0)
            self.assertGreater(timeouts[2], 0)
            self.assertIsNone(timeouts[3])
        device.close.assert_called_once()
