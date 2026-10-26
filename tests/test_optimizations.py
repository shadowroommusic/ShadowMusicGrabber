# -*- coding: utf-8 -*-
"""v1.14 优化项的验收测试。

覆盖:版本号单一来源、更新包 SHA256 校验、NCM original 模式保留 MP3、
fpcalc 120 秒截断与 MusicBrainz 节流、ffprobe 单次探测、下载分片并发、
yt-dlp 懒加载、暂存目录分配的线程安全。
"""

import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import converter  # noqa: E402
import downloader  # noqa: E402
import fingerprint  # noqa: E402
import mcp_server  # noqa: E402
import ncm_decrypt  # noqa: E402
import qmc_decrypt  # noqa: E402
import updater  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _strip_id3(data: bytes) -> bytes:
    """去掉 MP3 开头的 ID3v2 标签块,返回裸 MPEG 帧流。"""
    if data[:3] == b"ID3":
        size = (
            (data[6] & 0x7F) << 21
            | (data[7] & 0x7F) << 14
            | (data[8] & 0x7F) << 7
            | (data[9] & 0x7F)
        )
        return data[10 + size:]
    return data


class VersionSingleSourceTests(unittest.TestCase):
    """版本号只允许写在 _version.py 一处。"""

    def test_mcp_server_matches_version_module(self):
        import _version

        self.assertEqual(mcp_server.SERVER_VERSION, _version.__version__)

    def test_main_imports_version_module(self):
        source = open(os.path.join(PROJECT_ROOT, "main.py"), encoding="utf-8").read()
        self.assertIn("from _version import __version__ as APP_VERSION", source)
        self.assertNotIn('APP_VERSION = "1.', source)

    def test_pyproject_uses_dynamic_attr(self):
        text = open(os.path.join(PROJECT_ROOT, "pyproject.toml"), encoding="utf-8").read()
        self.assertIn('dynamic = ["version"]', text)
        self.assertIn('_version.__version__', text)
        self.assertNotIn('\nversion = "1.', text)


class _FakeDownload:
    """模拟 download_update 需要的响应对象(read/headers/上下文协议)。"""

    def __init__(self, data: bytes):
        self._data = data
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n):
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class UpdateDigestTests(unittest.TestCase):
    """更新包完整性:SHA256 优先,无 digest 时回退大小校验。"""

    PAYLOAD = b"fake-exe-payload" * 64

    def _download(self, digest: str, asset_name: str) -> str:
        info = updater.UpdateInfo(
            tag="v9.9.9",
            asset_name=asset_name,
            asset_url=f"https://example.com/{asset_name}",
            asset_size=len(self.PAYLOAD),
            asset_digest=digest,
        )
        with mock.patch.object(
            updater.urllib.request, "urlopen", return_value=_FakeDownload(self.PAYLOAD)
        ):
            return updater.download_update(info)

    def _update_dir(self) -> str:
        return os.path.join(tempfile.gettempdir(), "MusicGrabber-update")

    def test_matching_digest_installs(self):
        good = "sha256:" + hashlib.sha256(self.PAYLOAD).hexdigest()
        path = self._download(good, "ShadowMusicGrabber-9.9.9-good.exe")
        self.assertTrue(os.path.isfile(path))

    def test_mismatching_digest_raises_and_deletes(self):
        bad = "sha256:" + "0" * 64
        with self.assertRaises(updater.UpdateError):
            self._download(bad, "ShadowMusicGrabber-9.9.9-bad.exe")
        self.assertFalse(
            os.path.exists(os.path.join(self._update_dir(), "ShadowMusicGrabber-9.9.9-bad.exe")),
            "校验失败的坏包必须被删除",
        )

    def test_unknown_digest_algorithm_falls_back_to_size(self):
        path = self._download("blake3:abcdef", "ShadowMusicGrabber-9.9.9-fb.exe")
        self.assertTrue(os.path.isfile(path))

    def test_api_payload_carries_digest(self):
        payload = {
            "tag_name": "v9.9.9",
            "name": "r",
            "body": "",
            "assets": [{
                "name": "ShadowMusicGrabber-9.9.9-windows-x64.exe",
                "browser_download_url": "https://example.com/a.exe",
                "size": 10,
                "digest": "sha256:" + "a" * 64,
            }],
        }
        with mock.patch.object(updater, "_get_json", return_value=payload), \
                mock.patch.object(updater, "platform_tag", return_value="windows-x64"), \
                mock.patch.object(updater, "_platform_exts", return_value=(".exe", ".zip")):
            info = updater.check_for_update("1.0.0")
        self.assertEqual(info.asset_digest, "sha256:" + "a" * 64)


class NcmOriginalModeTests(unittest.TestCase):
    """output_fmt="original":内嵌 MP3 原样保留,不再强制转 FLAC。"""

    @classmethod
    def setUpClass(cls):
        os.makedirs("_ut_ncm", exist_ok=True)
        cls.ff = converter.find_ffmpeg()
        if not cls.ff:
            raise unittest.SkipTest("需要 ffmpeg")

    def _make_mp3(self) -> bytes:
        subprocess.run(
            [self.ff, "-hide_banner", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1", "-c:a", "libmp3lame", "-b:a", "128k",
             os.path.join("_ut_ncm", "opt_src.mp3")],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with open(os.path.join("_ut_ncm", "opt_src.mp3"), "rb") as f:
            return f.read()

    def test_original_keeps_mp3_bytes_and_extension(self):
        from test_ncm import make_ncm

        audio = self._make_mp3()
        ncm = make_ncm(audio, {"musicName": "原样保留", "artist": [["歌手"]], "format": "mp3"})
        src = os.path.join("_ut_ncm", "orig_song.ncm")
        with open(src, "wb") as f:
            f.write(ncm)
        dst = os.path.join("_ut_ncm", "orig_song.flac")
        # original 模式不应依赖 ffmpeg:把定位结果打空也能完成
        with mock.patch.object(converter, "find_ffmpeg", return_value=None):
            out, meta = ncm_decrypt.decrypt_file(src, dst, output_fmt="original")
        self.assertTrue(out.endswith(".mp3"), out)
        with open(out, "rb") as f:
            produced = f.read()
        self.assertEqual(meta.music_name, "原样保留")

        from mutagen.mp3 import MP3

        tags = MP3(out).tags
        self.assertEqual(str(tags.getall("TIT2")[0]), "原样保留")
        self.assertEqual(str(tags.getall("TPE1")[0]), "歌手")

        # 标签是"外加"的 ID3 头,MPEG 音频帧本身必须逐字节一致(未被重编码)。
        self.assertEqual(_strip_id3(produced), _strip_id3(audio),
                         "MPEG 帧流被改动,疑似经过了重编码")

    def test_flac_output_still_transcodes_mp3(self):
        from test_ncm import make_ncm

        audio = self._make_mp3()
        ncm = make_ncm(audio, {"musicName": "转出FLAC", "format": "mp3"})
        src = os.path.join("_ut_ncm", "to_flac.ncm")
        with open(src, "wb") as f:
            f.write(ncm)
        out, _ = ncm_decrypt.decrypt_file(
            src, os.path.join("_ut_ncm", "to_flac.flac"), ffmpeg=self.ff
        )
        self.assertTrue(out.endswith(".flac"))
        with open(out, "rb") as f:
            self.assertTrue(f.read().startswith(b"fLaC"))


class FingerprintOptimizationTests(unittest.TestCase):
    """fpcalc 只取前 120 秒;MusicBrainz 请求全局节流。"""

    def test_fpcalc_caps_length_at_120(self):
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return mock.Mock(returncode=0, stdout=json.dumps(
                {"duration": 180, "fingerprint": "AQAB"}), stderr="")

        with mock.patch.object(fingerprint, "find_fpcalc", return_value="fpcalc"), \
                mock.patch.object(fingerprint.subprocess, "run", side_effect=fake_run):
            duration, fp = fingerprint.compute_fingerprint("long.flac")
        self.assertEqual((duration, fp), (180, "AQAB"))
        args = captured["args"]
        self.assertIn("-length", args)
        self.assertEqual(args[args.index("-length") + 1], "120")

    def test_musicbrainz_throttle_enforces_one_per_second(self):
        with mock.patch.object(fingerprint, "_MB_LAST_REQUEST", 0.0), \
                mock.patch.object(fingerprint.time, "monotonic", return_value=100.0), \
                mock.patch.object(fingerprint.time, "sleep") as fake_sleep:
            fingerprint._throttle_musicbrainz()
            fake_sleep.assert_not_called()  # 距上次超过 1s,立即放行
            fingerprint._MB_LAST_REQUEST = 99.5
            fingerprint._throttle_musicbrainz()
            fake_sleep.assert_called_once_with(0.5)  # 补足到 1s 间隔


class ProbeMediaTests(unittest.TestCase):
    """一次 ffprobe 拿全时长/采样率/编码,替代逐字段探测。"""

    @classmethod
    def setUpClass(cls):
        os.makedirs("_ut_tmp", exist_ok=True)
        cls.ff = converter.find_ffmpeg()
        if not cls.ff:
            raise unittest.SkipTest("需要 ffmpeg")
        cls.wav = os.path.join("_ut_tmp", "probe_src.wav")
        subprocess.run(
            [cls.ff, "-hide_banner", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=2", "-ar", "44100",
             "-c:a", "pcm_s16le", cls.wav],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_returns_all_fields_in_one_call(self):
        info = converter.probe_media(self.wav, self.ff)
        self.assertAlmostEqual(info.duration, 2.0, delta=0.2)
        self.assertEqual(info.sample_rate, 44100)
        self.assertEqual(info.codec_name, "pcm_s16le")
        self.assertEqual(info.bits_per_sample, 16)

    def test_wav_codec_args_uses_precomputed_media(self):
        info = converter.MediaInfo(sample_fmt="s32", bits_per_sample=24)
        self.assertEqual(converter._wav_codec_args("x.wav", self.ff, info),
                         ["-c:a", "pcm_s24le"])


class DownloaderOptsTests(unittest.TestCase):
    """下载提速与启动加速。"""

    def _task(self):
        return downloader.DownloadTask(
            url="https://example.com/a", out_dir=".", fmt=downloader.FormatKind.FLAC
        )

    def test_fragment_downloads_are_concurrent(self):
        opts = downloader.build_ydl_opts(self._task(), lambda d: None)
        self.assertEqual(opts["concurrent_fragment_downloads"], 4)

    def test_importing_downloader_does_not_load_yt_dlp(self):
        """yt-dlp 导入要 1 秒上下,只有真正下载时才允许加载。"""
        code = (
            "import sys; sys.path.insert(0, %r); "
            "import downloader; "
            "assert 'yt_dlp' not in sys.modules, 'yt_dlp should be lazy'; print('ok')"
            % PROJECT_ROOT
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)


class ThreadSafetyTests(unittest.TestCase):
    """并行批次依赖的路径分配必须无冲突。"""

    def test_staging_path_unique_across_threads(self):
        results = []
        lock = threading.Lock()

        def worker():
            path = converter.staging_path("same-name.flac")
            with lock:
                results.append(os.path.normcase(path))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), len(set(results)), "并行分配到了重复的暂存路径")


class QmcTitleHeuristicTests(unittest.TestCase):
    """QTag 歌名:无 CJK 无空格的英文单词歌名也能被选中。"""

    @staticmethod
    def _qtag(parts: bytes) -> bytes:
        import io

        data = b"AUDIO" + parts + struct.pack(">I", len(parts)) + b"QTag"
        return qmc_decrypt._read_qtag_title(io.BytesIO(data), len(data))

    def test_single_english_word_title(self):
        self.assertEqual(self._qtag(b"QUJDRA==,Hello"), "Hello")

    def test_pure_digits_still_rejected(self):
        self.assertEqual(self._qtag(b"QUJDRA==,123456"), "")


if __name__ == "__main__":
    unittest.main()
