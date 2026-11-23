"""Автотесты критических сценариев VoicePad (без звуковых устройств и сети)."""
import io
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

import voicepad
import voicepad_config
import voicepad_platform


class ResampleTests(unittest.TestCase):
    def test_empty_and_equal_rate(self):
        empty = np.zeros((0, 1), dtype=np.float32)
        self.assertEqual(voicepad.resample(empty, 44100, 48000).shape, (0, 1))
        x = np.ones((128, 2), dtype=np.float32)
        y = voicepad.resample(x, 48000, 48000)
        np.testing.assert_array_equal(y, x)

    def test_lengths_mono_stereo(self):
        for sr, new_sr in ((44100, 48000), (48000, 44100), (48000, 16000)):
            x = np.zeros(1000, dtype=np.float32)
            y = voicepad.resample(x, sr, new_sr)
            self.assertEqual(y.shape, (int(round(1000 * new_sr / sr)),))
            self.assertEqual(y.dtype, np.float32)
            stereo = voicepad.resample(np.zeros((1000, 2), dtype=np.float32), sr, new_sr)
            self.assertEqual(stereo.shape, (len(y), 2))

    def test_tone_frequency_kept_and_no_clipping(self):
        sr = 44100
        t = np.arange(8192) / sr
        x = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        y = voicepad.resample(x, sr, 48000)
        self.assertLessEqual(float(np.abs(y).max()), 0.55)
        spectrum = np.abs(np.fft.rfft(y * np.hanning(len(y))))
        peak_hz = np.fft.rfftfreq(len(y), d=1.0 / 48000)[int(spectrum.argmax())]
        self.assertLess(abs(peak_hz - 1000), 40)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg_path = d / "config.json"
        self.backup_path = d / "config.json.bak"
        self.key_path = d / "key.dat"
        self.patchers = [
            patch.object(voicepad_config, "APP_DIR", d),
            patch.object(voicepad_config, "CFG_PATH", self.cfg_path),
            patch.object(voicepad_config, "CFG_BACKUP", self.backup_path),
            patch.object(voicepad_config, "KEY_PATH", self.key_path),
        ]
        for p in self.patchers:
            p.start()
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def write_cfg(self, data):
        self.cfg_path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    def test_missing_cfg_uses_defaults(self):
        cfg = voicepad.load_cfg()
        self.assertEqual(cfg["model"], voicepad.DEFAULT_MODEL)
        self.assertEqual(cfg["api_key"], "")
        self.assertEqual(cfg["voices"][0]["name"], "Стандартный")

    def test_corrupted_cfg_recovers_from_backup(self):
        self.backup_path.write_text(json.dumps({"model": "from-backup"}), "utf-8")
        self.cfg_path.write_text("{это не JSON", "utf-8")
        self.assertEqual(voicepad.load_cfg()["model"], "from-backup")

    def test_invalid_values_are_dropped(self):
        self.write_cfg({
            "model": "", "vol_cable": "громко", "vol_monitor": 5,
            "passthrough": "да", "history": "мусор", "voices": [],
            "unknown_field": 123,
        })
        cfg = voicepad.load_cfg()
        self.assertEqual(cfg["model"], voicepad.DEFAULT_MODEL)
        self.assertEqual(cfg["vol_cable"], 1.0)
        self.assertEqual(cfg["vol_monitor"], 1.0)
        self.assertIs(cfg["passthrough"], False)
        self.assertEqual(cfg["history"], [])
        self.assertTrue(cfg["voices"])
        self.assertNotIn("unknown_field", cfg)

    def test_history_limited(self):
        self.write_cfg({"history": [{"text": str(i)} for i in range(70)]})
        self.assertEqual(len(voicepad.load_cfg()["history"]), voicepad.HISTORY_MAX)

    @unittest.skipIf(sys.platform != "win32", "DPAPI — только Windows")
    def test_legacy_key_migrates_out_of_json(self):
        self.write_cfg({"api_key": "legacy-secret-key"})
        cfg = voicepad.load_cfg()
        self.assertEqual(cfg["api_key"], "legacy-secret-key")
        self.assertTrue(self.key_path.exists())
        self.assertNotIn(b"legacy-secret-key", self.key_path.read_bytes())
        self.assertNotIn("legacy-secret-key", self.cfg_path.read_text("utf-8"))

    @unittest.skipIf(sys.platform != "win32", "DPAPI — только Windows")
    def test_save_and_delete_key(self):
        cfg = voicepad.load_cfg()
        voicepad.save_cfg(cfg)   # первый запуск: config.json ещё не было
        cfg["api_key"] = "new-secret-key"
        voicepad.save_cfg(cfg)   # повторная запись делает резервную копию
        self.assertNotIn("api_key", json.loads(self.cfg_path.read_text("utf-8")))
        self.assertTrue(self.backup_path.exists())
        self.assertEqual(voicepad.load_api_key(), "new-secret-key")
        cfg["api_key"] = ""
        voicepad.save_cfg(cfg)
        self.assertFalse(self.key_path.exists())
        self.assertEqual(voicepad.load_cfg()["api_key"], "")


class CableSafetyTests(unittest.TestCase):
    def test_only_official_https_urls(self):
        good = "https://vb-audio.com/Cable/VBCABLE_Driver_Pack43.zip"
        self.assertTrue(voicepad.official_cable_url(good))
        self.assertTrue(voicepad.official_cable_url(good.replace("vb-audio.com", "www.vb-audio.com")))
        for bad in ("http://vb-audio.com/Cable/x.zip", "https://evil.com/Cable/x.zip",
                    "https://vb-audio.com.attacker/Cable/x.zip", "https://vb-audio.com/Other/x.zip"):
            self.assertFalse(voicepad.official_cable_url(bad))

    def test_html_instead_of_zip_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(RuntimeError, "ZIP"):
                voicepad.extract_cable_setup(b"<!doctype html><html>x</html>", Path(d))

    def test_traversal_and_missing_setup_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            fake_exe = b"MZ" + b"\0" * 120_000
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("VBCABLE_Driver_Pack43/VBCABLE_Setup_x64.exe", fake_exe)
                z.writestr("VBCABLE_Driver_Pack43/../../evil.txt", "взлом")
            with patch.object(voicepad_platform, "verify_cable_setup"):
                setup = voicepad.extract_cable_setup(buf.getvalue(), base / "vbcable")
            self.assertTrue(setup.exists())
            self.assertFalse((base / "evil.txt").exists())

            empty = io.BytesIO()
            with zipfile.ZipFile(empty, "w") as z:
                z.writestr("readme.txt", "hi")
            with self.assertRaisesRegex(RuntimeError, "архиве нет"):
                voicepad.extract_cable_setup(empty.getvalue(), base / "vbcable")

    def test_unsigned_installer_never_runs(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / voicepad.CABLE_SETUP
            fake.write_bytes(b"MZ" + b"\0" * 120_000)
            result = Mock(
                stdout="STATUS=NotSigned\nSUBJECT=\n", stderr="", returncode=0)
            with patch.object(voicepad_platform.subprocess, "run", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "подпись"):
                    voicepad.verify_cable_setup(fake)


class PlayerTests(unittest.TestCase):
    def test_tokens_unique_under_concurrency(self):
        player = voicepad.Player({}, lambda msg: None)
        tokens = []
        threads = [threading.Thread(target=lambda: tokens.append(player._next_token()))
                   for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(tokens), 50)
        self.assertEqual(len(set(tokens)), 50)


if __name__ == "__main__":
    unittest.main()
