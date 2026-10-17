# -*- coding: utf-8 -*-
"""解密产物落地：original 必须只按需重采样，绝不能传给 ffmpeg 当输出格式。"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import converter  # noqa: E402


def _run(cmd):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _make_wav(path: str, rate: int = 44100) -> None:
    ffmpeg = converter.find_ffmpeg()
    if not ffmpeg:
        raise unittest.SkipTest("ffmpeg not available")
    proc = _run(
        [
            ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration=1:sample_rate={rate}",
            "-c:a", "pcm_s16le",
            path,
        ]
    )
    if proc.returncode != 0:
        raise unittest.SkipTest(f"cannot generate wav: {proc.stderr[-200:]}")


def _sample_rate(path: str) -> int:
    ffmpeg = converter.find_ffmpeg()
    ffprobe = converter.find_ffprobe(ffmpeg) if ffmpeg else None
    if not ffprobe:
        raise unittest.SkipTest("ffprobe not available")
    proc = _run(
        [
            ffprobe, "-v", "quiet", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate", "-of", "csv=p=0", path,
        ]
    )
    return int(proc.stdout.strip())


class FinalizeDecryptedTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name
        self.ffmpeg = converter.find_ffmpeg()
        if not self.ffmpeg:
            self.skipTest("ffmpeg not available")

    def tearDown(self):
        self._td.cleanup()

    def test_original_with_sample_rate_resamples_in_place(self):
        src = os.path.join(self.dir, "song.wav")
        _make_wav(src, 44100)

        out = converter.finalize_decrypted(
            src, "original", sample_rate=48000, ffmpeg=self.ffmpeg
        )

        self.assertEqual(os.path.normcase(out), os.path.normcase(src))
        self.assertTrue(os.path.exists(src))
        self.assertEqual(_sample_rate(src), 48000)

    def test_original_without_rate_is_noop(self):
        src = os.path.join(self.dir, "song.wav")
        _make_wav(src, 44100)
        before = os.path.getmtime(src)

        out = converter.finalize_decrypted(src, "original", ffmpeg=self.ffmpeg)

        self.assertEqual(out, src)
        self.assertEqual(os.path.getmtime(src), before)

    def test_other_format_transcodes_and_removes_raw(self):
        src = os.path.join(self.dir, "song.wav")
        _make_wav(src, 44100)

        out = converter.finalize_decrypted(src, "flac", ffmpeg=self.ffmpeg)

        self.assertTrue(out.endswith(".flac"))
        self.assertTrue(os.path.exists(out))
        self.assertFalse(os.path.exists(src))
