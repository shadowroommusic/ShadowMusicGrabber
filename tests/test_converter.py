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
        ext = ".exe" if os.name == "nt" else ""
        with tempfile.TemporaryDirectory() as td:
            ffmpeg = os.path.join(td, "ffmpeg" + ext)
            ffprobe = os.path.join(td, "ffprobe" + ext)
            open(ffmpeg, "wb").close()
            open(ffprobe, "wb").close()
            self.assertEqual(converter.find_ffprobe(ffmpeg), os.path.abspath(ffprobe))

    def test_find_ffmpeg_in_pyinstaller_meipass(self):
        ext = ".exe" if os.name == "nt" else ""
        with tempfile.TemporaryDirectory() as td:
            bundled = os.path.join(td, "bin", "ffmpeg" + ext)
            os.makedirs(os.path.dirname(bundled), exist_ok=True)
            open(bundled, "wb").close()
            # 把候选目录收敛到 MEIPASS,避免本机 bin/ 里恰好放了 ffmpeg 时干扰。
            with mock.patch.object(converter.shutil, "which", return_value=None), \
                    mock.patch.object(converter.sys, "_MEIPASS", td, create=True), \
                    mock.patch.object(
                        converter, "_app_dirs",
                        return_value=[td, os.path.join(td, "bin")],
                    ):
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
    @unittest.skipUnless(os.name == "nt", "Windows 反斜杠路径语义")
    def test_basic(self):
        p = converter.build_output_path(r"C:\music\song.flac", r"C:\out", "wav")
        self.assertEqual(p, os.path.join(r"C:\out", "song.wav"))

    def test_basic_portable(self):
        src_dir = "C:/music" if os.name == "nt" else "/music"
        out_dir = "C:/out" if os.name == "nt" else "/out"
        p = converter.build_output_path(
            os.path.join(src_dir, "song.flac"), out_dir, "wav"
        )
        self.assertEqual(p, os.path.join(out_dir, "song.wav"))

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
        ffprobe = converter.find_ffprobe(self.ff)
        self.assertTrue(ffprobe, "测试需要 ffprobe")
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


class TestBatchNameCollision(unittest.TestCase):
    """回归:同一批次里不同来源产出同名文件时不得互相覆盖。

    历史 bug: GUI 的「添加文件」在调用 build_output_path 时没传 reserved,
    导致 A/song.mp3 与 B/song.mp3 都排到 song.flac,后一个任务静默覆盖
    前一个的结果(真实数据丢失)。
    """

    def test_same_basename_different_dirs_get_distinct_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "out")
            os.makedirs(out)
            src_a = os.path.join(td, "A", "song.mp3")
            src_b = os.path.join(td, "B", "song.mp3")
            for path in (src_a, src_b):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "wb").close()

            reserved: set[str] = set()
            first = converter.build_output_path(src_a, out, "flac", reserved)
            second = converter.build_output_path(src_b, out, "flac", reserved)
            self.assertNotEqual(
                os.path.normcase(first), os.path.normcase(second),
                "不同来源的同名文件必须排到不同输出路径",
            )
            self.assertTrue(first.endswith("song.flac"))
            self.assertTrue(second.endswith("song_1.flac"))

    def test_reserved_seeded_from_existing_tasks(self):
        """GUI 用已有任务的 dst 预填 reserved,新任务必须避开。"""
        with tempfile.TemporaryDirectory() as td:
            existing = os.path.join(td, "song.flac")
            open(existing, "wb").close()
            reserved = {os.path.normcase(os.path.abspath(existing))}
            nxt = converter.build_output_path(
                os.path.join(td, "song.mp3"), td, "flac", reserved
            )
            self.assertTrue(nxt.endswith("song_1.flac"))
            self.assertNotEqual(
                os.path.normcase(os.path.abspath(nxt)),
                os.path.normcase(os.path.abspath(existing)),
            )

    def test_many_colliding_sources_all_unique(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "out")
            os.makedirs(out)
            reserved: set[str] = set()
            names = []
            for folder in ("A", "B", "C", "D"):
                src = os.path.join(td, folder, "song.mp3")
                os.makedirs(os.path.dirname(src), exist_ok=True)
                names.append(
                    os.path.basename(
                        converter.build_output_path(src, out, "flac", reserved)
                    )
                )
            self.assertEqual(len(set(names)), 4, f"输出名应互不相同: {names}")

    def test_without_reserved_conflict_exists(self):
        """反向证明:不传 reserved 确实会冲突(说明为什么必须传)。"""
        with tempfile.TemporaryDirectory() as td:
            src_a = os.path.join(td, "A", "song.mp3")
            src_b = os.path.join(td, "B", "song.mp3")
            for path in (src_a, src_b):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "wb").close()
            first = converter.build_output_path(src_a, td, "flac")
            second = converter.build_output_path(src_b, td, "flac")
            self.assertEqual(
                os.path.normcase(first), os.path.normcase(second),
                "不传 reserved 时确实冲突,这正是本测试要防的回归",
            )


class TestAtomicWriteProtection(unittest.TestCase):
    """原子写:转换失败绝不能破坏已有目标文件,也不应泄漏临时文件。"""

    def test_failed_conversion_preserves_destination(self):
        with tempfile.TemporaryDirectory() as td:
            bad = os.path.join(td, "bad.mp3")
            with open(bad, "wb") as f:
                f.write(b"not audio at all")
            keep = os.path.join(td, "keep.flac")
            with open(keep, "wb") as f:
                f.write(b"ORIGINAL DATA")
            with self.assertRaises(converter.ConvertError):
                converter.convert_file(
                    bad, keep, "flac", ffmpeg=converter.find_ffmpeg()
                )
            with open(keep, "rb") as f:
                self.assertEqual(f.read(), b"ORIGINAL DATA")

    def test_repeated_failures_leave_no_temp_files(self):
        import glob
        with tempfile.TemporaryDirectory() as td:
            bad = os.path.join(td, "x.mp3")
            with open(bad, "wb") as f:
                f.write(b"garbage")
            for i in range(4):
                try:
                    converter.convert_file(
                        bad, os.path.join(td, f"o{i}.flac"),
                        "flac", ffmpeg=converter.find_ffmpeg(),
                    )
                except converter.ConvertError:
                    pass
            leftovers = [
                p for p in glob.glob(os.path.join(td, ".*"))
                if os.path.isfile(p)
            ]
            self.assertEqual(leftovers, [], f"临时文件泄漏: {leftovers}")


# 1x1 透明 PNG,足够验证封面字节是否往返。
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63fcffff3f0300050001a5f645bf0000"
    "000049454e44ae426082"
)


class TestPicturePreservation(unittest.TestCase):
    """转码/重采样必须保留内嵌封面。

    ffmpeg 把 FLAC/MP3 封面当作 attached_pic 视频流,而转码命令使用
    `-map 0:a:0 -vn`,因此需要靠 mutagen 抠出再写回。
    """

    cover_ready = False

    @classmethod
    def setUpClass(cls):
        os.makedirs("_ut_tmp", exist_ok=True)
        cls.ff = converter.find_ffmpeg()
        cls.src_flac = os.path.join("_ut_tmp", "cover_src.flac")
        import subprocess
        subprocess.run(
            [cls.ff, "-hide_banner", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1", "-c:a", "flac", cls.src_flac],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        cls.cover_ready = converter.attach_picture(cls.src_flac, TINY_PNG, "flac")

    def setUp(self):
        if not self.cover_ready:
            self.skipTest("mutagen 无法写入封面,跳过")

    def _fresh_copy(self, name):
        import shutil
        target = os.path.join("_ut_tmp", name)
        shutil.copyfile(self.src_flac, target)
        return target

    def test_attach_and_extract_roundtrip(self):
        target = self._fresh_copy("rt.flac")
        self.assertEqual(converter.extract_picture(target), TINY_PNG, "封面应原样取出")

    def test_flac_to_flac_keeps_cover(self):
        dst = os.path.join("_ut_tmp", "keep_cover.flac")
        out = converter.convert_file(self.src_flac, dst, "flac", ffmpeg=self.ff)
        self.assertEqual(converter.extract_picture(out), TINY_PNG, "FLAC 转 FLAC 必须保留封面")

    def test_resample_keeps_cover(self):
        target = self._fresh_copy("resample_cover.flac")
        converter.resample_in_place(target, 48000, ffmpeg=self.ff)
        self.assertEqual(converter.extract_picture(target), TINY_PNG, "重采样必须保留封面")

    def test_flac_to_mp3_keeps_cover(self):
        dst = os.path.join("_ut_tmp", "cover_out.mp3")
        out = converter.convert_file(self.src_flac, dst, "mp3", ffmpeg=self.ff)
        self.assertEqual(converter.extract_picture(out), TINY_PNG, "转 MP3 也应带上封面")

    def test_attach_replaces_instead_of_duplicating(self):
        target = self._fresh_copy("once.flac")
        converter.attach_picture(target, TINY_PNG, "flac")
        converter.attach_picture(target, TINY_PNG, "flac")
        from mutagen.flac import FLAC
        self.assertEqual(len(FLAC(target).pictures), 1, "重复写入不应产生多张封面")

    def test_wav_output_skips_cover_silently(self):
        """WAV 无标签容器,不应因此报错。"""
        dst = os.path.join("_ut_tmp", "nowav.wav")
        out = converter.convert_file(self.src_flac, dst, "wav", ffmpeg=self.ff)
        self.assertTrue(os.path.exists(out))
        self.assertEqual(converter.extract_picture(out), b"")

    def test_extract_on_missing_file_returns_empty(self):
        self.assertEqual(converter.extract_picture("_ut_tmp/does-not-exist.flac"), b"")

    def test_extract_on_file_without_cover_returns_empty(self):
        plain = os.path.join("_ut_tmp", "plain.wav")
        import subprocess
        subprocess.run(
            [self.ff, "-hide_banner", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1", plain],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(converter.extract_picture(plain), b"")


if __name__ == "__main__":
    unittest.main()
