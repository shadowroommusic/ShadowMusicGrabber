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


class TestNormalizeSampleRate(unittest.TestCase):
    """采样率规范化:UI 传入的字符串/None/异常值都应安全回落到"跟随原始"。"""

    def test_valid_rates(self):
        self.assertEqual(converter.normalize_sample_rate("44100"), 44100)
        self.assertEqual(converter.normalize_sample_rate(48000), 48000)
        self.assertEqual(converter.normalize_sample_rate("96000"), 96000)
        self.assertEqual(converter.normalize_sample_rate("192000"), 192000)
        self.assertEqual(converter.normalize_sample_rate(" 44100 "), 44100)

    def test_keep_original(self):
        self.assertIsNone(converter.normalize_sample_rate(None))
        self.assertIsNone(converter.normalize_sample_rate(""))
        self.assertIsNone(converter.normalize_sample_rate(0))
        self.assertIsNone(converter.normalize_sample_rate("0"))

    def test_invalid_values_fall_back_to_none(self):
        for bad in ("abc", "44.1", None, -44100, "1", 999999, []):
            self.assertIsNone(
                converter.normalize_sample_rate(bad), f"异常输入应回落到 None: {bad!r}"
            )

    def test_choices_are_covered(self):
        for rate in converter.SAMPLE_RATE_CHOICES:
            self.assertEqual(converter.normalize_sample_rate(str(rate)), rate)


class TestSampleRateConversion(unittest.TestCase):
    """真实转码:验证输出文件的实际采样率,并确认默认不重采样。"""

    @classmethod
    def setUpClass(cls):
        os.makedirs("_ut_tmp", exist_ok=True)
        cls.ff = converter.find_ffmpeg()
        cls.ffprobe = converter.find_ffprobe(cls.ff)
        cls.src = os.path.join("_ut_tmp", "sr_src.wav")
        import subprocess
        subprocess.run(
            [cls.ff, "-hide_banner", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=2", "-ar", "44100",
             "-c:a", "pcm_s16le", cls.src],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _rate(self, path):
        import json
        import subprocess
        out = subprocess.run(
            [self.ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_streams", path],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        for s in json.loads(out).get("streams", []):
            if s.get("codec_type") == "audio":
                return int(s.get("sample_rate", 0))
        return 0

    def test_upsample_to_48k(self):
        dst = os.path.join("_ut_tmp", "sr48.flac")
        out = converter.convert_file(
            self.src, dst, "flac", ffmpeg=self.ff, sample_rate=48000
        )
        self.assertEqual(self._rate(out), 48000)

    def test_downsample_to_44k(self):
        dst = os.path.join("_ut_tmp", "sr44.wav")
        out = converter.convert_file(
            self.src, dst, "wav", ffmpeg=self.ff, sample_rate=44100
        )
        self.assertEqual(self._rate(out), 44100)

    def test_hires_96k(self):
        dst = os.path.join("_ut_tmp", "sr96.wav")
        out = converter.convert_file(
            self.src, dst, "wav", ffmpeg=self.ff, sample_rate=96000
        )
        self.assertEqual(self._rate(out), 96000)

    def test_hires_192k(self):
        dst = os.path.join("_ut_tmp", "sr192.wav")
        out = converter.convert_file(
            self.src, dst, "wav", ffmpeg=self.ff, sample_rate=192000
        )
        self.assertEqual(self._rate(out), 192000)

    def test_default_keeps_source_rate(self):
        """不传 sample_rate 时输出应保持源文件的 44100,不做任何重采样。"""
        dst = os.path.join("_ut_tmp", "sr_keep.flac")
        out = converter.convert_file(self.src, dst, "flac", ffmpeg=self.ff)
        self.assertEqual(self._rate(out), 44100)


class TestResampleInPlace(unittest.TestCase):
    """原地重采样:保持容器与文件名,只改采样率。"""

    @classmethod
    def setUpClass(cls):
        os.makedirs("_ut_tmp", exist_ok=True)
        cls.ff = converter.find_ffmpeg()
        cls.ffprobe = converter.find_ffprobe(cls.ff)
        cls.src = os.path.join("_ut_tmp", "rip_src.wav")
        import subprocess
        subprocess.run(
            [cls.ff, "-hide_banner", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=2", "-ar", "44100",
             "-c:a", "pcm_s16le", cls.src],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _rate(self, path):
        import json
        import subprocess
        out = subprocess.run(
            [self.ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_streams", path],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        for s in json.loads(out).get("streams", []):
            if s.get("codec_type") == "audio":
                return int(s.get("sample_rate", 0))
        return 0

    def test_resamples_and_keeps_path(self):
        import shutil
        target = os.path.join("_ut_tmp", "rip_target.wav")
        shutil.copyfile(self.src, target)
        before_size = os.path.getsize(target)
        out = converter.resample_in_place(target, 48000, ffmpeg=self.ff)
        self.assertEqual(out, os.path.abspath(target), "应原地替换,路径不变")
        self.assertTrue(os.path.exists(target))
        self.assertEqual(self._rate(target), 48000)
        self.assertGreater(os.path.getsize(target), 0)
        self.assertNotEqual(before_size, -1)

    def test_no_temp_files_left_behind(self):
        import glob
        import shutil
        target = os.path.join("_ut_tmp", "rip_clean.wav")
        shutil.copyfile(self.src, target)
        converter.resample_in_place(target, 96000, ffmpeg=self.ff)
        leftovers = [
            p for p in glob.glob(os.path.join("_ut_tmp", ".rip_clean.wav*"))
            if os.path.isfile(p)
        ]
        self.assertEqual(leftovers, [], f"不应残留临时文件: {leftovers}")

    def test_zero_rate_is_a_noop(self):
        import shutil
        target = os.path.join("_ut_tmp", "rip_noop.wav")
        shutil.copyfile(self.src, target)
        out = converter.resample_in_place(target, 0, ffmpeg=self.ff)
        self.assertEqual(out, target)
        self.assertEqual(self._rate(target), 44100, "0 应表示不重采样")


if __name__ == "__main__":
    unittest.main()
