# -*- coding: utf-8 -*-
"""更新模块的版本解析、Release 解析与替换脚本测试。"""

import unittest
from unittest import mock

import updater


class VersionParsingTests(unittest.TestCase):
    def test_parse_version_extracts_numbers(self):
        self.assertEqual(updater.parse_version("v1.5.0"), (1, 5, 0))
        self.assertEqual(updater.parse_version("1.4"), (1, 4))
        self.assertEqual(updater.parse_version(""), (0,))

    def test_is_newer_compares_numerically(self):
        self.assertTrue(updater.is_newer("v1.5.0", "1.4.0"))
        self.assertTrue(updater.is_newer("1.10.0", "1.9.9"))
        self.assertFalse(updater.is_newer("1.4.0", "1.4.0"))
        self.assertFalse(updater.is_newer("1.3.9", "1.4.0"))


class ReleaseParsingTests(unittest.TestCase):
    @staticmethod
    def _release(tag, assets):
        return {
            "tag_name": tag,
            "name": f"MusicGrabber {tag}",
            "body": "notes",
            "assets": assets,
        }

    def test_check_for_update_returns_none_when_current(self):
        payload = self._release("v1.4.0", [])
        with mock.patch.object(updater, "_get_json", return_value=payload):
            self.assertIsNone(updater.check_for_update("1.4.0"))

    def test_check_for_update_prefers_exe_asset(self):
        payload = self._release(
            "v1.6.0",
            [
                {
                    "name": "MusicGrabber-1.6.0.zip",
                    "browser_download_url": "https://example.com/a.zip",
                    "size": 10,
                },
                {
                    "name": "MusicGrabber.exe",
                    "browser_download_url": "https://example.com/a.exe",
                    "size": 20,
                },
            ],
        )
        with mock.patch.object(updater, "_get_json", return_value=payload):
            info = updater.check_for_update("1.5.0")
        self.assertIsNotNone(info)
        self.assertEqual(info.tag, "v1.6.0")
        self.assertEqual(info.asset_name, "MusicGrabber.exe")
        self.assertEqual(info.asset_url, "https://example.com/a.exe")

    def test_check_for_update_without_asset_still_reports_version(self):
        payload = self._release("v1.6.0", [])
        with mock.patch.object(updater, "_get_json", return_value=payload):
            info = updater.check_for_update("1.5.0")
        self.assertIsNotNone(info)
        self.assertEqual(info.tag, "v1.6.0")
        self.assertEqual(info.asset_url, "")

    def test_download_update_requires_asset_url(self):
        info = updater.UpdateInfo(
            tag="v1.6.0", name="", notes="", asset_name="", asset_url="", asset_size=0
        )
        with self.assertRaises(updater.UpdateError):
            updater.download_update(info)


class ApplyScriptTests(unittest.TestCase):
    def test_apply_script_waits_then_replaces_and_restarts(self):
        script = updater.build_apply_script(
            r"C:\app\MusicGrabber.exe", r"C:\tmp\MusicGrabber.exe", 4242
        )
        self.assertIn('PID eq 4242', script)
        self.assertIn(r'copy /Y "%SOURCE%" "%TARGET%"', script)
        self.assertIn(r'start "" "%TARGET%"', script)
        self.assertIn(r"TARGET=C:\app\MusicGrabber.exe", script)


if __name__ == "__main__":
    unittest.main()
