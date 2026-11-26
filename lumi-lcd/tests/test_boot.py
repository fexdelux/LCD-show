from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import os
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boot


REPO = boot.ASSET.parent.parent


class BootTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="boot image ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / boot.CONFIG
        self.overlay = self.root / boot.OVERLAY
        self.overlay.parent.mkdir()
        self.config.write_bytes(b"dtparam=audio=on\n[pi4]\narm_boost=1\n")

    def apply(self, action="install", rotate="90"):
        with boot.boot_lock(self.root):
            boot.transact(self.root, boot.plan(self.root, action, rotate))

    def run_cli(self, command, *args):
        return subprocess.run([REPO / f"LCD35-lite-{command}", *args],
                              cwd=self.root, capture_output=True, text=True)

    def test_layouts_and_dry_run(self):
        for path in ("boot", "boot/firmware"):
            image = self.root / path
            (image / "overlays").mkdir(parents=True)
            original = self.config.read_bytes()
            (image / boot.CONFIG).write_bytes(original)
            result = self.run_cli("show", "--boot-dir", image, "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(image / boot.OVERLAY), result.stdout)
            self.assertEqual(sorted(p.name for p in image.iterdir()), [boot.CONFIG, "overlays"])
            self.assertEqual((image / boot.CONFIG).read_bytes(), original)

    def test_install_reinstall_and_restore_preserving_edits(self):
        original = self.config.read_bytes()
        self.config.chmod(0o640)
        self.apply()
        output = self.config.read_bytes()
        self.assertEqual(output, original + boot.managed_block(original, "90"))
        self.assertEqual(boot.sha(self.overlay.read_bytes()), boot.OVERLAY_SHA)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        saved = self.root / boot.STATE
        before = saved.stat()
        self.apply()
        self.assertEqual(saved.stat().st_ino, before.st_ino)
        self.apply(rotate="270")
        self.assertIn(b"rotate=270", self.config.read_bytes())
        manifest = boot.read_manifest(boot.snapshot(saved), self.root, False)
        self.assertEqual(boot.payload(manifest["original_config"]), original)
        edit = b"dtparam=i2c_arm=on\n"
        self.config.write_bytes(edit + self.config.read_bytes())
        result = self.run_cli("restore", "--boot-dir", self.root, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.overlay.exists())
        self.apply("restore")
        self.assertEqual(self.config.read_bytes(), edit + original)
        self.assertFalse(self.overlay.exists())
        self.assertFalse(saved.exists())

    def test_reuse_overlay_and_spi(self):
        original = b"dtparam=audio=on,spi=on\n[pi4]\r\n# config"
        self.config.write_bytes(original)
        self.overlay.write_bytes(boot.ASSET.read_bytes())
        previous = self.overlay.stat()
        self.apply()
        self.assertEqual(self.config.read_bytes().count(b"spi=on"), 1)
        self.apply("restore")
        self.assertEqual(self.config.read_bytes(), original)
        self.assertEqual(self.overlay.stat().st_ino, previous.st_ino)

    def test_reject_conflicts(self):
        for lines in (
            b"[pi3]\ndtparam=spi=on\n", b"dtparam=spi=off\n",
            b"dtparam=spi=on\ndtparam=spi=on\n",
            b"dtoverlay=tft35a:rotate=0\n", b"dtoverlay=spi0-1cs\n",
            b"dtoverlay=,param=on\n", b"include extra.txt\n",
            b"dtoverlay=vc4-kms-v3d\ndtparam=spi=on\n",
            b"os_prefix=somewhere/\n", b"[pi3\n", b"# BEGIN LUMI LCD35\n",
        ):
            with self.subTest(lines=lines):
                self.config.write_bytes(lines)
                with self.assertRaises(boot.BootError):
                    self.apply()
                self.assertEqual(self.config.read_bytes(), lines)
                self.assertFalse(self.overlay.exists())

    def test_overlay_parameter_scope(self):
        original = b"[all]\ndtoverlay=vc4-kms-v3d\n[pi4]\ndtoverlay=\n"
        self.config.write_bytes(original)
        self.apply()
        self.assertTrue(self.config.read_bytes().startswith(original))
        self.assertIn(b"dtoverlay=tft35a:rotate=90\ndtoverlay=\ndtparam=spi=on", self.config.read_bytes())
        self.apply("restore")
        self.assertEqual(self.config.read_bytes(), original)

        original = b"dtoverlay=vc4-kms-v3d\ndtoverlay=\ndtparam=spi=on\n"
        self.config.write_bytes(original)
        self.apply()
        self.assertEqual(self.config.read_bytes().count(b"dtparam=spi=on"), 1)

    def test_overlay_changed_or_missing_asset(self):
        self.overlay.write_bytes(b"other overlay")
        with self.assertRaises(boot.BootError):
            self.apply()
        self.assertEqual(self.overlay.read_bytes(), b"other overlay")
        self.overlay.unlink()
        with patch.object(boot, "ASSET", self.root / "missing"), self.assertRaises(FileNotFoundError):
            self.apply()

    def test_restore_rejects_foreign_edits_to_managed_area_or_manifest(self):
        self.apply()
        data = self.config.read_bytes()
        manifest = self.root / boot.STATE
        manifest_before = manifest.read_bytes()
        for config, backup in (
            (data + b"enable_uart=1\n", manifest_before),
            (data.replace(b"rotate=90", b"rotate=0"), manifest_before),
            (data, manifest_before.replace(b'"sha256": "', b'"sha256": "000')),
        ):
            with self.subTest(config=config, backup_valid=backup == manifest_before):
                self.config.write_bytes(config)
                manifest.write_bytes(backup)
                with self.assertRaises(boot.BootError):
                    self.apply("restore")
                self.assertEqual(self.config.read_bytes(), config)
                self.assertTrue(self.overlay.exists())

    def test_restore_leaves_a_replaced_overlay_untouched(self):
        self.apply()
        installed_config = self.config.read_bytes()
        self.overlay.write_bytes(b"replaced")
        with self.assertRaisesRegex(boot.BootError, "Overlay ausente ou alterado"):
            self.apply("restore")
        self.assertEqual(self.config.read_bytes(), installed_config)
        self.assertEqual(self.overlay.read_bytes(), b"replaced")

    def test_path_symlinks(self):
        self.overlay.symlink_to(self.config)
        with self.assertRaises(boot.BootError):
            self.apply()
        result = self.run_cli("show", "--boot-dir", self.root, "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("link simbólico", result.stderr)

    def test_missing_overlays_directory(self):
        self.overlay.parent.rmdir()
        result = self.run_cli("show", "--boot-dir", self.root, "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.overlay.parent.exists())

    def test_rollback_after_write_on_install_and_restore(self):
        for action in ("install", "restore"):
            before = [boot.snapshot(self.root / name) for name in (boot.CONFIG, boot.OVERLAY, boot.STATE)]
            replace = boot.replace

            def fail_once(path, record, fat=False):
                replace(path, record, fat)
                if path == self.config and record != before[0]:
                    raise OSError("simulated failure after rename")

            with patch.object(boot, "replace", side_effect=fail_once), self.assertRaises(OSError):
                self.apply(action)
            self.assertEqual(before, [boot.snapshot(self.root / name)
                                      for name in (boot.CONFIG, boot.OVERLAY, boot.STATE)])
            self.assertFalse((self.root / boot.JOURNAL).exists())
            if action == "install":
                self.apply()

    def test_journal_recovers_partial_transaction(self):
        changes = boot.plan(self.root, "install")
        boot.replace(self.root / boot.JOURNAL, boot.json_record(changes))
        name, _, after = changes[0]
        boot.replace(self.root / name, after)
        boot.rollback(self.root, changes)
        self.assertFalse(self.overlay.exists())
        self.assertEqual(self.config.read_bytes(), boot.payload(changes[1][1]))

    def test_fat_content_equivalence(self):
        old = boot.record_for(b"same", mode=0o600)
        new = boot.record_for(b"same", mode=0o755)
        self.assertTrue(boot.equivalent(old, new, fat=True))
        self.assertFalse(boot.equivalent(old, new, fat=False))

    def test_lock_rejects_parallel_writer(self):
        with boot.boot_lock(self.root), self.assertRaises(BlockingIOError):
            with boot.boot_lock(self.root):
                self.fail("acquired the same lock twice")

    def test_no_implicit_host_writes_and_offline_demo_rejected(self):
        result = self.run_cli("show", "--boot-dir", self.root, "--install-demo", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incompatível com --boot-dir", result.stderr)
        if os.geteuid() != 0:
            result = self.run_cli("show", "--boot-dir", self.root)
            self.assertIn("requerem root", result.stderr)
            self.assertFalse(self.overlay.exists())

    def test_default_boot_prefers_modern_and_falls_back(self):
        fake = self.root / "boot"
        fake.mkdir()
        (fake / boot.CONFIG).touch()
        firmware = fake / "firmware"
        firmware.mkdir()
        paths = {"/boot": fake, "/boot/firmware/config.txt": firmware / boot.CONFIG}
        real_path = Path
        with patch.object(boot, "Path", side_effect=lambda path: paths.get(path, real_path(path))):
            self.assertEqual(boot.boot_path(None), fake)
            (firmware / boot.CONFIG).touch()
            self.assertEqual(boot.boot_path(None), firmware)

    def test_recovery_dry_run_and_real_recovery_keep_previous_installation(self):
        self.apply(rotate="90")
        original = self.config.read_bytes()
        changes = boot.changed(boot.plan(self.root, "install", "180"))
        journal = self.root / boot.JOURNAL
        boot.replace(journal, boot.json_record(changes))
        boot.replace(self.config, changes[0][2])
        interrupted = self.config.read_bytes()
        result = self.run_cli("restore", "--boot-dir", self.root, "--recover-only", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-dtoverlay=tft35a:rotate=180", result.stdout)
        self.assertIn("+dtoverlay=tft35a:rotate=90", result.stdout)
        self.assertEqual(self.config.read_bytes(), interrupted)
        self.assertTrue(journal.exists())
        argv = ["boot.py", "restore", "--boot-dir", str(self.root), "--recover-only"]
        # Exercise the real entry point against an explicit fixture, without elevating privileges.
        with patch.object(sys, "argv", argv), patch("boot.os.geteuid", return_value=0), redirect_stdout(StringIO()):
            boot.main()
        self.assertEqual(self.config.read_bytes(), original)
        self.assertTrue(self.overlay.exists())
        self.assertFalse(journal.exists())

    def test_invalid_journal_rejected_before_any_write(self):
        expected = self.config.read_bytes()
        changes = boot.changed(boot.plan(self.root, "install"))
        for history in (
            {}, [None],
            [*changes, changes[-1]],
            [*changes[1:], changes[0]],
            [("outside", None, changes[0][2]), *changes[1:]],
            [(boot.OVERLAY, None, boot.record_for(b"invalid")), *changes[1:]],
            [changes[0], (boot.CONFIG, changes[1][1],
                          boot.record_for(b"dtparam=audio=off\n" + boot.managed_block(b"", "90"))),
             changes[-1]],
        ):
            with self.subTest(history_type=type(history)):
                boot.replace(self.root / boot.JOURNAL, boot.json_record(history))
                result = self.run_cli("restore", "--boot-dir", self.root, "--recover-only", "--dry-run")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.config.read_bytes(), expected)
                self.assertFalse(self.overlay.exists())

    def test_recovery_preflights_all_paths_before_reverting_any(self):
        changes = boot.changed(boot.plan(self.root, "install"))
        boot.replace(self.root / boot.JOURNAL, boot.json_record(changes))
        boot.replace(self.root / boot.STATE, changes[-1][2])
        self.config.write_bytes(b"foreign change\n")
        result = self.run_cli("restore", "--boot-dir", self.root, "--recover-only", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / boot.STATE).exists())
        self.assertTrue((self.root / boot.JOURNAL).exists())

    def test_dry_run_shows_diff_and_preserves_full_pi_fixture(self):
        config = (b"dtparam=audio=on\n[all]\ndtoverlay=vc4-kms-v3d\n"
                  b"max_framebuffers=2\n[pi4]\narm_boost=1\n[all]\n")
        self.config.write_bytes(config)
        result = self.run_cli("show", "--dry-run", "--boot-dir", self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("+dtoverlay=tft35a:rotate=90\n+dtoverlay=\n+dtparam=spi=on", result.stdout)
        self.assertIn(boot.JOURNAL, result.stdout)
        self.assertNotIn("-dtoverlay=vc4-kms-v3d", result.stdout)
        self.assertEqual(self.config.read_bytes(), config)
        self.assertFalse((self.root / boot.JOURNAL).exists())
        self.assertFalse((self.root / boot.STATE).exists())
        self.assertFalse(self.overlay.exists())
        self.apply()
        self.assertTrue(self.config.read_bytes().startswith(config))
        self.assertEqual(self.config.read_bytes().count(b"dtoverlay=vc4-kms-v3d"), 1)

    def test_native_driver_absence_stops_before_boot_access(self):
        distro = "ID=debian\nVERSION_ID=13"
        with patch.object(sys, "argv", ["boot.py", "install", "--dry-run"]), \
                patch.object(boot, "host", return_value=("Raspberry Pi 3 Model B Rev 1.2", distro)), \
                patch("boot.platform.machine", return_value="aarch64"), \
                patch.object(boot, "drivers", return_value=False), patch.object(boot, "boot_path") as path:
            with self.assertRaisesRegex(boot.BootError, "Drivers necessários"):
                boot.main()
            path.assert_not_called()

    def test_live_spi_refuses_another_driver(self):
        node = self.root / "spi0.0"
        node.mkdir()
        driver = self.root / "spidev"
        driver.mkdir()
        (node / "driver").symlink_to(driver)
        with patch.object(boot, "SPI_DEVICES", self.root):
            boot.check_live_spi()
            (node / "driver").unlink()
            (node / "driver").symlink_to(self.root / "ili9486")
            with self.assertRaisesRegex(boot.BootError, "spi0.0 já está ocupado"):
                boot.check_live_spi()

    def test_other_display_overlays_and_custom_spi0_reference_are_rejected(self):
        (self.overlay.parent / "peripheral.dtbo").write_bytes(b"sample\0spi0\0data")
        for name in ("fbtft", "ads7846", "peripheral"):
            self.config.write_text(f"dtoverlay={name}\n")
            with self.subTest(name=name), self.assertRaises(boot.BootError):
                self.apply()

    def test_signals_trigger_rollback_after_rename(self):
        import signal

        config = self.config.read_bytes()
        real_replace = boot.replace

        def interrupt(path, record, fat=False):
            real_replace(path, record, fat)
            if path == self.config and boot.payload(record) != config:
                boot.stop(signal.SIGINT, None)

        with patch.object(boot, "replace", side_effect=interrupt), self.assertRaises(InterruptedError):
            self.apply()
        self.assertEqual(self.config.read_bytes(), config)
        self.assertFalse(self.overlay.exists())
        self.assertFalse((self.root / boot.JOURNAL).exists())

    def test_journal_validation_accepts_interrupted_initial_install_and_restore(self):
        before = self.config.read_bytes()
        for action in ("install", "restore"):
            with self.subTest(action=action):
                state = boot.snapshot(self.root / boot.STATE)
                config = self.config.read_bytes()
                changes = boot.changed(boot.plan(self.root, action))
                journal = boot.json_record(changes)
                for name, _, proposed in changes[:-1]:
                    boot.replace(self.root / name, proposed)
                boot.rollback(self.root, boot.pending_changes(journal))
                self.assertEqual(self.config.read_bytes(), config)
                self.assertEqual(boot.snapshot(self.root / boot.STATE), state)
                self.assertEqual(self.overlay.exists(), state is not None)
                if action == "install":
                    self.apply()
        self.apply("restore")
        self.assertEqual(self.config.read_bytes(), before)
