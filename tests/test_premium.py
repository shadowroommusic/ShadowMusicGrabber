# -*- coding: utf-8 -*-
"""premium 模块测试:URL 识别、参数校验、错误路径。"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import premium  # noqa: E402


class TestUrlDetection(unittest.TestCase):
    def test_apple_music(self):
        self.assertTrue(premium.is_apple_music_url("https://music.apple.com/us/album/x/123?i=456"))
        self.assertFalse(premium.is_apple_music_url("https://www.beatport.com/track/x/1"))
        self.assertFalse(premium.is_apple_music_url("https://soundcloud.com/a/b"))

    def test_beatport(self):
        self.assertTrue(premium.is_beatport_url("https://www.beatport.com/track/strobe/1696999"))
        self.assertTrue(premium.is_beatport_url("https://www.beatport.com/release/x/1"))
        self.assertFalse(premium.is_beatport_url("https://music.apple.com/us/song/1"))

    def test_case_insensitive(self):
        self.assertTrue(premium.is_apple_music_url("HTTPS://MUSIC.APPLE.COM/US/SONG/1"))

    def test_reject_lookalike_hosts(self):
        self.assertFalse(premium.is_apple_music_url("https://music.apple.com.attacker.example/song/1"))
        self.assertFalse(premium.is_beatport_url("https://beatport.com.attacker.example/track/x/1"))


class TestBeatportdlDiscovery(unittest.TestCase):
    @unittest.skipUnless(
        os.path.isfile(
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "bin",
                "beatportdl.exe",
            )
        ),
        "仓库不包含第三方 beatportdl.exe，从源码树找不到时跳过",
    )
    def test_bundled_bin_exists(self):
        exe = premium._find_beatportdl()
        self.assertIsNotNone(exe, "应能找到 beatportdl")
        self.assertTrue(os.path.exists(exe), f"路径应存在: {exe}")


class TestGamdlErrors(unittest.TestCase):
    def test_non_apple_url(self):
        with self.assertRaises(premium.PremiumError):
            premium.gamdl_download("https://example.com/x", "cookies.txt", "out")

    def test_missing_cookies(self):
        with self.assertRaises(premium.PremiumError) as ctx:
            premium.gamdl_download(
                "https://music.apple.com/us/song/1", "/nonexistent/cookies.txt", "out")
        self.assertIn("cookies", str(ctx.exception))


class TestBeatportErrors(unittest.TestCase):
    def test_non_beatport_url(self):
        with self.assertRaises(premium.PremiumError):
            premium.beatportdl_download("https://example.com/x", "u", "p", "out")

    def test_missing_credentials(self):
        with self.assertRaises(premium.PremiumError):
            premium.beatportdl_download(
                "https://www.beatport.com/track/x/1", "", "", "out")

    def test_quality_map(self):
        # 显示名 -> 值 -> 订阅档 映射完整
        self.assertEqual(premium.BEATPORT_QUALITIES["FLAC 无损 (44.1kHz)"], ("lossless", "Professional"))
        self.assertEqual(premium.GAMDL_CODECS["AAC 256kbps (推荐, 无需额外配置)"], "aac-web")
        self.assertEqual(premium.GAMDL_CODECS["ALAC 无损 24bit/192kHz (需 wrapper 服务)"], "alac")


class TestBeatportProcess(unittest.TestCase):
    def test_uses_quit_mode_and_unblocks_fatal_pause(self):
        class FakeStdin:
            def __init__(self):
                self.writes = []
                self.flushed = False
                self.closed = False

            def write(self, value):
                self.writes.append(value)

            def flush(self):
                self.flushed = True

            def close(self):
                self.closed = True

        class FakeProcess:
            def __init__(self):
                self.stdin = FakeStdin()
                self.stdout = FakeStdout()

            def wait(self):
                return 1

        class FakeStdout:
            def __iter__(self):
                return iter(())

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as td:
            proc = FakeProcess()
            with patch.object(premium, "_find_beatportdl", return_value="beatportdl.exe"), \
                 patch.object(premium, "_app_data_dir", return_value=td), \
                 patch.object(premium.subprocess, "Popen", return_value=proc) as popen:
                with self.assertRaises(premium.PremiumError):
                    premium.beatportdl_download(
                        "https://www.beatport.com/track/strobe/1696999",
                        "user",
                        "password",
                        os.path.join(td, "downloads"),
                    )

        self.assertEqual(popen.call_args.args[0], ["beatportdl.exe", "-q", "https://www.beatport.com/track/strobe/1696999"])
        self.assertEqual(proc.stdin.writes, ["\n"])
        self.assertTrue(proc.stdin.flushed)
        self.assertTrue(proc.stdin.closed)


if __name__ == "__main__":
    unittest.main()
