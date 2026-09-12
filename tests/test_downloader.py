# -*- coding: utf-8 -*-
"""downloader 单元测试:平台识别、URL 校验、格式选项构造、错误处理。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import downloader  # noqa: E402


class TestDetectPlatform(unittest.TestCase):
    def test_platforms(self):
        cases = [
            ("https://soundcloud.com/artist/track", "SoundCloud"),
            ("https://music.apple.com/cn/album/x", "Apple Music"),
            ("https://music.163.com/#/song?id=1", "网易云音乐"),
            ("https://y.qq.com/n/ryqq/songDetail/1", "QQ音乐"),
            ("https://www.youtube.com/watch?v=abc", "YouTube"),
            ("https://www.bilibili.com/video/BV1", "哔哩哔哩"),
            ("https://open.spotify.com/track/x", "Spotify"),
            ("https://example.com/x", "通用"),
        ]
        for url, expect in cases:
            self.assertEqual(downloader.detect_platform(url), expect, url)

    def test_case_insensitive(self):
        self.assertEqual(downloader.detect_platform("HTTP://SOUNDCLOUD.COM/a"), "SoundCloud")


class TestIsValidUrl(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(downloader.is_valid_url("https://a.com"))
        self.assertTrue(downloader.is_valid_url("http://a.com/x"))

    def test_invalid(self):
        self.assertFalse(downloader.is_valid_url("notaurl"))
        self.assertFalse(downloader.is_valid_url(""))
        self.assertFalse(downloader.is_valid_url("  "))


class TestBuildYdlOpts(unittest.TestCase):
    def _task(self, fmt):
        return downloader.DownloadTask(url="https://a.com/x", out_dir="D:/tmp", fmt=fmt)

    def test_flac_postprocessor(self):
        opts = downloader.build_ydl_opts(self._task(downloader.FormatKind.FLAC), lambda d: None)
        pp = opts["postprocessors"][0]
        self.assertEqual(pp["key"], "FFmpegExtractAudio")
        self.assertEqual(pp["preferredcodec"], "flac")

    def test_wav_no_postprocessor(self):
        # WAV 不再使用 yt-dlp 后处理(固定 16-bit 会降位深),下载后自行转码
        opts = downloader.build_ydl_opts(self._task(downloader.FormatKind.WAV), lambda d: None)
        self.assertNotIn("postprocessors", opts)

    def test_original_no_postprocessor(self):
        opts = downloader.build_ydl_opts(self._task(downloader.FormatKind.ORIGINAL), lambda d: None)
        self.assertNotIn("postprocessors", opts)

    def test_bestaudio(self):
        opts = downloader.build_ydl_opts(self._task(downloader.FormatKind.FLAC), lambda d: None)
        self.assertEqual(opts["format"], "bestaudio/best")
        self.assertTrue(opts["noplaylist"])


class TestDownloadErrors(unittest.TestCase):
    def test_invalid_url(self):
        with self.assertRaises(downloader.DownloadError):
            downloader.download_url("notaurl", "_ut_tmp", downloader.FormatKind.FLAC)

    def test_probe_invalid_url(self):
        with self.assertRaises(downloader.DownloadError):
            downloader.probe_url("bad url here")

    def test_find_output_rejects_stale_info_filepath(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "same.flac"
            path.write_bytes(b"old")
            # 用与实现一致的 abspath 归一化：CI 的 %TEMP% 是 8.3 短名(RUNNER~1)，
            # resolve() 会把它展开成长路径，导致 before 与候选路径对不上。
            normalized = os.path.abspath(str(path))
            task = downloader.DownloadTask(
                url="https://a.com/x", out_dir=tmp, fmt=downloader.FormatKind.FLAC
            )
            info = {"filepath": normalized}
            self.assertEqual(downloader._find_output(task, info, before={normalized}), "")


if __name__ == "__main__":
    unittest.main()
