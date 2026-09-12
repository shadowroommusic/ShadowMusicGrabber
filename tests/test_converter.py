# -*- coding: utf-8 -*-
"""converter 单元测试:ffmpeg 定位、格式识别、路径生成、错误处理。"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import converter  # noqa: E402


class TestFindFfmpeg(unittest.TestCase):
    def test_find_ffmpeg(self):
        exe = converter.find_ffmpeg()
        self.assertTrue(exe, "应能找到 ffmpeg")
        self.assertTrue(os.path.exists(exe), f"路径应存在: {exe}")

    def test_find_ffprobe_next_to_ffmpeg(self):
        with tempfile.TemporaryDirectory() as td:
            ffmpeg = os.path.join(td, "ffmpeg.exe")
            ffprobe = os.path.join(td, "ffprobe.exe")
            open(ffmpeg, "wb").close()
            open(ffprobe, "wb").close()
            self.assertEqual(converter.find_ffprobe(ffmpeg), os.path.abspath(ffprobe))

    def test_find_ffmpeg_in_pyinstaller_meipass(self):
        with tempfile.TemporaryDirectory() as td:
            bundled = os.path.join(td, "bin", "ffmpeg.exe")
            os.makedirs(os.path.dirname(bundled), exist_ok=True)
            open(bundled, "wb").close()
            with mock.patch.object(converter.shutil, "which", return_value=None), \
                    mock.patch.object(converter.sys, "_MEIPASS", td, create=True):
                self.assertEqual(converter.find_ffmpeg(), os.path.abspath(bundled))


class TestIsSupported(unittest.TestCase):
    def test_supported_exts(self):
        for ext in [".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".ape"]:
            self.assertTrue(converter.is_supported("song" + ext), ext)
        self.assertTrue(converter.is_supported("SONG.FLAC"), "大写扩展名")

    def test_unsupported(self):
        self.assertFalse(converter.is_supported("song.txt"))
        self.assertFalse(converter.is_supported("song"))


class TestBuildOutputPath(unittest.TestCase):
    def test_basic(self):
        p = converter.build_output_path(r"C:\music\song.flac", r"C:\out", "wav")
        self.assertEqual(p, os.path.join(r"C:\out", "song.wav"))

    def test_collision(self):
        os.makedirs("_ut_tmp", exist_ok=True)
        open(os.path.join("_ut_tmp", "a.wav"), "w").close()
        p = converter.build_output_path(os.path.join("_ut_tmp", "a.mp3"), "_ut_tmp", "wav")
        self.assertNotEqual(p, os.path.join("_ut_tmp", "a.wav"), "不应覆盖已有文件")
        self.assertTrue(p.endswith("_1.wav"))

    def test_reserved_batch_paths(self):
        os.makedirs("_ut_tmp", exist_ok=True)
        reserved = set()
        p1 = converter.build_output_path("C:/music/song.mp3", "_ut_tmp", "flac", reserved)
        p2 = converter.build_output_path("D:/other/song.wav", "_ut_tmp", "flac", reserved)
        self.assertNotEqual(os.path.normcase(os.path.abspath(p1)), os.path.normcase(os.path.abspath(p2)))


class TestConvertErrors(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaises(converter.ConvertError):
            converter.convert_file("_ut_does_not_exist.mp3", "_ut_tmp/x.flac", "flac")

    def test_bad_fmt(self):
        os.makedirs("_ut_tmp", exist_ok=True)
        src = os.path.join("_ut_tmp", "a.mp3")
        open(src, "w").close()
        with self.assertRaises(converter.ConvertError):
            converter.convert_file(src, os.path.join("_ut_tmp", "a.xyz"), "xyz")

    def test_same_source_and_destination_is_rejected(self):
        os.makedirs("_ut_tmp", exist_ok=True)
        src = os.path.join("_ut_tmp", "same.wav")
        open(src, "wb").close()
        with self.assertRaisesRegex(converter.ConvertError, "不能与源文件相同"):
            converter.convert_file(src, src, "wav", ffmpeg=converter.find_ffmpeg())

    def test_failed_conversion_keeps_existing_destination(self):
        os.makedirs("_ut_tmp", exist_ok=True)
        src = os.path.join("_ut_tmp", "invalid.mp3")
        dst = os.path.join("_ut_tmp", "preserved.flac")
        with open(src, "wb") as f:
            f.write(b"not an audio file")
        sentinel = b"keep this output"
        with open(dst, "wb") as f:
            f.write(sentinel)
        with self.assertRaises(converter.ConvertError) as ctx:
            converter.convert_file(src, dst, "flac", ffmpeg=converter.find_ffmpeg())
        self.assertIn("ffmpeg", str(ctx.exception))
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), sentinel)


class TestRealConvert(unittest.TestCase):
    """真实转码往返:ffmpeg 生成 wav → flac → wav,校验输出存在且大小合理。"""

    @classmethod
    def setUpClass(cls):
        os.makedirs("_ut_tmp", exist_ok=True)
        cls.ff = converter.find_ffmpeg()
        cls.src_wav = os.path.join("_ut_tmp", "src.wav")
        import subprocess
        subprocess.run(
            [cls.ff, "-hide_banner", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=2", "-c:a", "pcm_s16le", cls.src_wav],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_wav_to_flac(self):
        dst = os.path.join("_ut_tmp", "out.flac")
        out = converter.convert_file(self.src_wav, dst, "flac", ffmpeg=self.ff)
        self.assertTrue(os.path.exists(out))
        self.assertGreater(os.path.getsize(out), 0)

    def test_flac_to_wav(self):
        flac = os.path.join("_ut_tmp", "mid.flac")
        converter.convert_file(self.src_wav, flac, "flac", ffmpeg=self.ff)
        wav = os.path.join("_ut_tmp", "back.wav")
        out = converter.convert_file(flac, wav, "wav", ffmpeg=self.ff)
        self.assertTrue(os.path.exists(out))
        self.assertGreater(os.path.getsize(out), 0)

    def test_progress_callback(self):
        seen = []
        converter.convert_file(
            self.src_wav, os.path.join("_ut_tmp", "cb.flac"), "flac",
            on_progress=seen.append, ffmpeg=self.ff,
        )
        self.assertTrue(any(p > 0 for p in seen), "进度回调应产生正值")


class TestBitDepthPreservation(unittest.TestCase):
    """位深保真:24-bit 源转 WAV 不应降为 16-bit。"""

    @classmethod
    def setUpClass(cls):
        os.makedirs("_ut_tmp", exist_ok=True)
        cls.ff = converter.find_ffmpeg()
        import subprocess
        cls.src24 = os.path.join("_ut_tmp", "src24.wav")
        subprocess.run(
            [cls.ff, "-hide_banner", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1", "-c:a", "pcm_s24le", cls.src24],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _bits(self, path: str) -> int:
        import json
        import subprocess
        ffprobe = os.path.join(os.path.dirname(self.ff), "ffprobe.exe")
        out = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", path],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        streams = json.loads(out.stdout)["streams"]
        for s in streams:
            if s.get("codec_type") == "audio":
                return int(s.get("bits_per_raw_sample") or s.get("bits_per_sample") or 0)
        return 0

    def test_24bit_wav_stays_24bit(self):
        out = converter.convert_file(self.src24, os.path.join("_ut_tmp", "o24.wav"), "wav", ffmpeg=self.ff)
        self.assertEqual(self._bits(out), 24, "24-bit 源转 WAV 应保持 24-bit")

    def test_24bit_flac_stays_24bit(self):
        out = converter.convert_file(self.src24, os.path.join("_ut_tmp", "o24.flac"), "flac", ffmpeg=self.ff)
        self.assertEqual(self._bits(out), 24, "24-bit 源转 FLAC 应保持 24-bit")

    def test_wav_codec_args_selection(self):
        args24 = converter._wav_codec_args(self.src24, self.ff)
        self.assertIn("pcm_s24le", args24)
        import subprocess
        src16 = os.path.join("_ut_tmp", "src16.wav")
        subprocess.run(
            [self.ff, "-hide_banner", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1", "-c:a", "pcm_s16le", src16],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        args16 = converter._wav_codec_args(src16, self.ff)
        self.assertIn("pcm_s16le", args16)


if __name__ == "__main__":
    unittest.main()
