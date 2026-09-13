# -*- coding: utf-8 -*-
"""更新模块的版本解析、Release 解析、限流兜底与替换脚本测试。"""

import os
import unittest
import urllib.error
from unittest import mock

import updater


class _FakeResponse:
    """模拟 urllib 响应，只提供 geturl() 与上下文管理协议。"""

    def __init__(self, url):
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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


class WebFallbackTests(unittest.TestCase):
    def test_rate_limit_messages_are_friendly(self):
        self.assertIn("频率", updater._http_error_message(403))
        self.assertIn("频率", updater._http_error_message(429))
        self.assertIn("Release", updater._http_error_message(404))

    def test_check_for_update_falls_back_to_web_when_api_limited(self):
        limited = updater.UpdateError("更新服务暂时限制了访问频率，请稍后再试。")
        tag_url = f"https://github.com/{updater.REPO}/releases/tag/v9.9.9"
        with mock.patch.object(updater, "_get_json", side_effect=limited), mock.patch.object(
            updater.urllib.request, "urlopen", return_value=_FakeResponse(tag_url)
        ):
            info = updater.check_for_update("1.5.0")
        self.assertIsNotNone(info)
        self.assertEqual(info.tag, "v9.9.9")
        # 兜底路径按当前平台拼出带版本号与平台标识的资产名
        expected = f"{updater.ASSET_STEM}-9.9.9-{updater.platform_tag()}{updater._platform_exts()[0]}"
        self.assertEqual(info.asset_name, expected)
        self.assertEqual(
            info.asset_url,
            f"https://github.com/{updater.REPO}/releases/download/v9.9.9/{expected}",
        )

    def test_web_fallback_reports_no_update_when_tag_is_current(self):
        limited = updater.UpdateError("nope")
        tag_url = f"https://github.com/{updater.REPO}/releases/tag/v1.5.0"
        with mock.patch.object(updater, "_get_json", side_effect=limited), mock.patch.object(
            updater.urllib.request, "urlopen", return_value=_FakeResponse(tag_url)
        ):
            self.assertIsNone(updater.check_for_update("1.5.0"))

    def test_check_for_update_raises_when_both_paths_fail(self):
        with mock.patch.object(
            updater, "_get_json", side_effect=updater.UpdateError("API 不可用")
        ), mock.patch.object(
            updater.urllib.request, "urlopen", side_effect=urllib.error.URLError("no net")
        ):
            with self.assertRaises(updater.UpdateError):
                updater.check_for_update("1.5.0")


class PlatformAssetTests(unittest.TestCase):
    """按平台/架构挑选 Release 资产。"""

    def test_platform_tag_shape(self):
        tag = updater.platform_tag()
        self.assertRegex(tag, r"^(windows|macos|linux|unknown)-(x64|x86|arm64)$")

    def _with_windows(self):
        return (
            mock.patch.object(updater, "platform_tag", return_value="windows-x64"),
            mock.patch.object(updater, "_platform_exts", return_value=(".exe", ".zip")),
        )

    def test_prefers_matching_platform_over_legacy(self):
        assets = [
            {"name": "ShadowMusicGrabber-1.9.0-macos-arm64.zip", "browser_download_url": "u-mac", "size": 1},
            {"name": "ShadowMusicGrabber-1.9.0-windows-x64.exe", "browser_download_url": "u-win", "size": 2},
            {"name": "ShadowMusicGrabber.exe", "browser_download_url": "u-legacy", "size": 3},
        ]
        patch_a, patch_b = self._with_windows()
        with patch_a, patch_b:
            picked = updater._select_asset(assets, "v1.9.0")
        self.assertEqual(picked["browser_download_url"], "u-win")

    def test_skips_other_platforms(self):
        assets = [
            {"name": "ShadowMusicGrabber-1.9.0-macos-arm64.zip", "browser_download_url": "u-mac", "size": 1},
        ]
        patch_a, patch_b = self._with_windows()
        with patch_a, patch_b:
            self.assertIsNone(updater._select_asset(assets, "v1.9.0"))

    def test_legacy_name_still_accepted(self):
        assets = [
            {"name": "ShadowMusicGrabber.exe", "browser_download_url": "u-legacy", "size": 1},
        ]
        patch_a, patch_b = self._with_windows()
        with patch_a, patch_b:
            picked = updater._select_asset(assets, "v1.9.0")
        self.assertEqual(picked["browser_download_url"], "u-legacy")


class DownloadTests(unittest.TestCase):
    def test_download_update_requires_asset_url(self):
        info = updater.UpdateInfo(tag="v1.6.0")
        with self.assertRaises(updater.UpdateError):
            updater.download_update(info)


class ApplyScriptTests(unittest.TestCase):
    def test_apply_script_waits_then_replaces_and_restarts(self):
        script = updater.build_apply_script(
            r"C:\app\MusicGrabber.exe", r"C:\tmp\MusicGrabber.exe", 4242
        )
        self.assertIn("PID eq 4242", script)
        self.assertIn(r'copy /Y "%SOURCE%" "%TARGET%"', script)
        self.assertIn(r'start "" "%TARGET%"', script)
        self.assertIn(r"TARGET=C:\app\MusicGrabber.exe", script)

    def test_apply_script_has_wait_timeout(self):
        """等待不能无限循环, 否则更新会卡死。"""
        script = updater.build_apply_script(r"C:\app\a.exe", r"C:\tmp\b.exe", 1)
        self.assertIn("set /a WAIT=0", script)
        self.assertIn("GEQ 120", script)

    def test_apply_vbs_waits_replaces_and_cleans_up(self):
        """VBS 主方案: WMI 等待 + 超时 + 覆盖 + 清理(且不依赖 cmd)。"""
        vbs = updater.build_apply_vbs(r"C:\app\a.exe", r"C:\tmp\b.exe", 4242)
        self.assertIn("pid = 4242", vbs)
        self.assertIn(r'target = "C:\app\a.exe"', vbs)
        self.assertIn(r'source = "C:\tmp\b.exe"', vbs)
        self.assertIn("Win32_Process", vbs)
        self.assertIn("i < 120", vbs)
        self.assertIn("fso.CopyFile source, target, True", vbs)
        self.assertIn("WScript.ScriptFullName", vbs)
        self.assertNotIn("cmd.exe", vbs)

    def test_install_and_restart_prefers_vbs(self):
        """install_and_restart 应优先用 wscript, 且不再使用 DETACHED_PROCESS。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            downloaded = os.path.join(td, "ShadowMusicGrabber-1.9.0-windows-x64.exe")
            open(downloaded, "wb").write(b"new")
            calls = []

            def fake_popen(args, **kwargs):
                calls.append((args, kwargs))
                return mock.Mock()

            with mock.patch.object(updater, "can_self_update", return_value=True), \
                 mock.patch.object(updater.subprocess, "Popen", side_effect=fake_popen):
                script = updater.install_and_restart(downloaded, target_exe=r"C:\app\a.exe")

        self.assertTrue(script.endswith(".vbs"), script)
        self.assertEqual(calls[0][0][0], "wscript.exe")
        flags = calls[0][1].get("creationflags", 0)
        self.assertFalse(flags & getattr(updater.subprocess, "DETACHED_PROCESS", 0))
        self.assertTrue(flags & getattr(updater.subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    unittest.main()
