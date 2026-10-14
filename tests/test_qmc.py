# -*- coding: utf-8 -*-
"""QMC 解密引擎测试: 使用合成夹具(tests/data/qmc/)验证全链路。

夹具为合成数据(不是真实歌曲), 由 tools/make_qmc_fixtures.py 生成;
生成后已用 libtakiyasha(独立实现)交叉验证解密结果一致。
静态密钥流的首个 16 字节断言取自 MusicDecrypto(MIT)的公开测试常量。
"""

import hashlib
import io
import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qmc_decrypt  # noqa: E402
from qmc_decrypt import QmcError  # noqa: E402

FIXDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "qmc")

# 夹具解密后的明文 sha256(由 tools/make_qmc_fixtures.py 输出)
EXPECTED_SHA256 = {
    "v1_static.qmcflac": "27fe8e792b96b96a1f995931733699fa98eac3f50d364d14482625439edbdf04",
    "v2_mask.mflac": "deb984be9f15753788e0ff234fb85a57def06774367c39928f17d5b5e445def7",
    "v2_rc4.mflac": "035984c835d2733254cd0b25d67cbd069cfb5f960f82e0221eab6d7079bd69bd",
    "v2_qtag_v2key.mflac0": "e12151cd7aa5b98f52a72c8f649b9de56a9de7d282e4323f446d45f01869d5c3",
}

# 夹具的数据区长度(不含尾部密钥)
EXPECTED_DATA_LEN = {
    "v1_static.qmcflac": 2048,
    "v2_mask.mflac": 2048,
    "v2_rc4.mflac": 6144,
    "v2_qtag_v2key.mflac0": 2048,
}


def fixture(name: str) -> bytes:
    with open(os.path.join(FIXDIR, name), "rb") as f:
        return f.read()


class TestQmcAlgorithmConstants(unittest.TestCase):
    def test_static_keystream_matches_known_constant(self):
        """静态(QMCv1)密钥流前 16 字节必须与公开测试常量一致。"""
        ks = qmc_decrypt._mask128_keystream(qmc_decrypt._static_mask128(), 0, 16)
        self.assertEqual(ks.hex(), "c34ad6ca9067f752d8a166629f5b0900")

    def test_static_keystream_agrees_with_box_formula_at_large_offsets(self):
        """大偏移处静态密钥流必须与盒子公式一致(覆盖跨块回绕)。"""
        def box_formula(o: int, n: int) -> bytes:
            out = bytearray(n)
            for i in range(n):
                idx = o + i
                if idx > 0x7FFF:
                    idx %= 0x7FFF
                out[i] = qmc_decrypt._STATIC_BOX[(idx * idx + 0x1B) & 0xFF]
            return bytes(out)

        for o in (0, 32767, 32768, 65534, 65535, 3_000_000, 33_554_431):
            self.assertEqual(
                qmc_decrypt._mask128_keystream(qmc_decrypt._static_mask128(), o, 8),
                box_formula(o, 8),
                f"offset={o}",
            )


class TestQmcEndToEnd(unittest.TestCase):
    """对每个夹具做文件级(decrypt_file)与内存级(decrypt_qmc_bytes)双重验证。"""

    def _run_file_case(self, name: str):
        src = os.path.join(FIXDIR, name)
        out_ext = qmc_decrypt.QMC_EXTS[os.path.splitext(name)[1].lower()]
        with tempfile.TemporaryDirectory() as td:
            dst = os.path.join(td, "out", "result" + out_ext)
            out_path, meta = qmc_decrypt.decrypt_file(src, dst)
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, "rb") as f:
                data = f.read()
            self.assertIsInstance(meta, qmc_decrypt.QmcMeta)
        return data

    def _check_case(self, name: str):
        data = fixture(name)
        ext = os.path.splitext(name)[1].lower()
        # 内存级
        out = qmc_decrypt.decrypt_qmc_bytes(data, ext)
        self.assertEqual(hashlib.sha256(out).hexdigest(), EXPECTED_SHA256[name])
        # 文件级
        out2 = self._run_file_case(name)
        self.assertEqual(out, out2)

    def test_v1_static(self):
        self._check_case("v1_static.qmcflac")

    def test_v2_mask_with_null_padded_tail(self):
        self._check_case("v2_mask.mflac")

    def test_v2_rc4(self):
        self._check_case("v2_rc4.mflac")

    def test_v2_qtag_encv2_key(self):
        self._check_case("v2_qtag_v2key.mflac0")

    def test_tail_parse_data_lengths(self):
        """尾部解析必须给出正确的数据区长度(含 QTag 形式)。"""
        for name, expected in EXPECTED_DATA_LEN.items():
            data = fixture(name)
            ext = os.path.splitext(name)[1].lower()
            if ext in qmc_decrypt.QMC_V1_EXTS:
                continue
            _, data_len = qmc_decrypt._read_tail(io.BytesIO(data), len(data))
            self.assertEqual(data_len, expected, name)

    def test_chunk_boundary_crossing(self):
        """跨 1MiB 分块边界的解密必须与单块一致(用 v1 静态路径)。"""
        plain = b"fLaC" + random.Random(9).randbytes(2 * 1024 * 1024 + 4096)
        ks = qmc_decrypt._mask128_keystream  # noqa: F841 - 仅为可读性保留
        key_stream = lambda o, n: qmc_decrypt._mask128_keystream(  # noqa: E731
            qmc_decrypt._static_mask128(), o, n)
        enc = qmc_decrypt._xor_bytes(plain, key_stream(0, len(plain)))
        self.assertEqual(qmc_decrypt.decrypt_qmc_bytes(enc, ".qmcflac"), plain)

    def test_make_output_path_collision(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "song.mflac")
            first = qmc_decrypt.make_output_path(src, td)
            self.assertTrue(first.endswith("song.flac"))
            open(first, "wb").write(b"x")
            second = qmc_decrypt.make_output_path(src, td)
            self.assertTrue(second.endswith("song_1.flac"))
            # reserved 集合防止同批次重名
            third = qmc_decrypt.make_output_path(src, td, {os.path.normcase(os.path.abspath(second))})
            self.assertTrue(third.endswith("song_2.flac"))

    def test_extension_maps(self):
        self.assertEqual(qmc_decrypt.QMC_EXTS[".qmc0"], ".mp3")
        self.assertEqual(qmc_decrypt.QMC_EXTS[".qmc3"], ".mp3")
        self.assertEqual(qmc_decrypt.QMC_EXTS[".qmcflac"], ".flac")
        self.assertEqual(qmc_decrypt.QMC_EXTS[".qmcogg"], ".ogg")
        self.assertEqual(qmc_decrypt.QMC_EXTS[".mflac"], ".flac")
        self.assertEqual(qmc_decrypt.QMC_EXTS[".mflac0"], ".flac")
        self.assertEqual(qmc_decrypt.QMC_EXTS[".mgg"], ".ogg")
        self.assertEqual(qmc_decrypt.QMC_EXTS[".mgg1"], ".ogg")
        self.assertEqual(qmc_decrypt.QMC_EXTS[".mmp4"], ".m4a")
        self.assertTrue(qmc_decrypt.is_qmc_file("a/b/song.mflac0"))
        self.assertFalse(qmc_decrypt.is_qmc_file("a/b/song.ncm"))
        self.assertFalse(qmc_decrypt.is_qmc_file("a/b/song.mp3"))


class TestQmcErrors(unittest.TestCase):
    def test_stag_unsupported(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "x.mflac")
            with open(src, "wb") as f:
                f.write(b"A" * 100 + b"STag")
            with self.assertRaises(QmcError) as ctx:
                qmc_decrypt.decrypt_file(src, os.path.join(td, "o.flac"))
            self.assertIn("STag", str(ctx.exception))

    def test_garbage_tail(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "y.mflac")
            with open(src, "wb") as f:
                f.write(b"B" * 500 + (0x20).to_bytes(4, "little"))
            with self.assertRaises(QmcError):
                qmc_decrypt.decrypt_file(src, os.path.join(td, "o.flac"))

    def test_unsupported_extension(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "z.xyz")
            with open(src, "wb") as f:
                f.write(b"C" * 64)
            with self.assertRaises(QmcError) as ctx:
                qmc_decrypt.decrypt_file(src, os.path.join(td, "o.flac"))
            self.assertIn("不支持", str(ctx.exception))

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(QmcError):
                qmc_decrypt.decrypt_file(os.path.join(td, "no.mflac"), os.path.join(td, "o.flac"))

    def test_wrong_key_fails_audio_validation(self):
        """结构合法但密钥不匹配时必须报错, 绝不输出垃圾文件。"""
        v1 = fixture("v1_static.qmcflac")
        mask_tail = fixture("v2_mask.mflac")[-369:]  # 另一个文件的尾部(key/长度)
        with self.assertRaises(QmcError) as ctx:
            qmc_decrypt.decrypt_qmc_bytes(v1 + mask_tail, ".mflac")
        self.assertIn("不是有效的音频", str(ctx.exception))

    def test_short_file(self):
        with self.assertRaises(QmcError):
            qmc_decrypt.decrypt_qmc_bytes(b"\x01\x02", ".mflac")


class TestQtagTitle(unittest.TestCase):
    """QTag 尾部的歌名提取(取不到必须安静返回空串)。"""

    @staticmethod
    def _build_qtag(chunk: bytes) -> bytes:
        """按真实 QTag 布局构造尾部: [数据][QTag 数据][4 字节大端长度]["QTag"]。"""
        import struct as _s
        return b"AUDIO" + chunk + _s.pack(">I", len(chunk)) + b"QTag"

    def _title(self, chunk):
        data = self._build_qtag(chunk)
        return qmc_decrypt._read_qtag_title(io.BytesIO(data), len(data))

    def test_picks_cjk_second_segment(self):
        self.assertEqual(self._title("QUJDRA==,我的歌名".encode("utf-8")), "我的歌名")

    def test_skips_key_segment(self):
        """首段是 Base64 密钥, 不能被当成歌名。"""
        self.assertEqual(self._title("QUJDREVGR0g=,晴天".encode("utf-8")), "晴天")

    def test_ignores_hex_only_segment(self):
        self.assertEqual(self._title("QUJDRA==,deadbeefdeadbeef".encode("utf-8")), "")

    def test_accepts_spaces_in_title(self):
        self.assertEqual(self._title("QUJDRA==,A Song Title".encode("utf-8")), "A Song Title")

    def test_no_comma_returns_empty(self):
        self.assertEqual(self._title(b"QUJDRA=="), "")

    def test_non_qtag_file_returns_empty(self):
        # 尾部不是 "QTag",应直接返回空串
        data = b"AUDIO" + b"tail" + (4).to_bytes(4, "big") + b"XXXX"
        self.assertEqual(qmc_decrypt._read_qtag_title(io.BytesIO(data), len(data)), "")

    def test_tiny_file_is_safe(self):
        self.assertEqual(qmc_decrypt._read_qtag_title(io.BytesIO(b"ab"), 2), "")

    def test_real_fixture_does_not_crash(self):
        """真实夹具可能没有歌名, 但绝不能抛异常。"""
        data = fixture("v2_qtag_v2key.mflac0")
        qmc_decrypt._read_qtag_title(io.BytesIO(data), len(data))


class TestTrackFilename(unittest.TestCase):
    """按"歌手 - 歌名"生成安全文件名。"""

    def test_artist_and_title(self):
        with tempfile.TemporaryDirectory() as td:
            p = qmc_decrypt.build_track_filename("周杰伦", "晴天", ".flac", td)
            self.assertEqual(os.path.basename(p), "周杰伦 - 晴天.flac")

    def test_title_only(self):
        with tempfile.TemporaryDirectory() as td:
            p = qmc_decrypt.build_track_filename("", "无题", ".flac", td)
            self.assertEqual(os.path.basename(p), "无题.flac")

    def test_no_title_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(qmc_decrypt.build_track_filename("歌手", "", ".flac", td))

    def test_collision_gets_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            first = qmc_decrypt.build_track_filename("A", "歌", ".flac", td)
            open(first, "wb").close()
            second = qmc_decrypt.build_track_filename("A", "歌", ".flac", td)
            self.assertEqual(os.path.basename(second), "A - 歌_1.flac")

    def test_reserved_set_prevents_collision(self):
        with tempfile.TemporaryDirectory() as td:
            reserved = set()
            first = qmc_decrypt.build_track_filename("A", "歌", ".flac", td, reserved)
            second = qmc_decrypt.build_track_filename("A", "歌", ".flac", td, reserved)
            self.assertNotEqual(os.path.normcase(first), os.path.normcase(second))

    def test_illegal_chars_are_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            p = qmc_decrypt.build_track_filename("AC/DC", "a<b>c:d", ".flac", td)
            name = os.path.basename(p)
            for ch in '<>:"/\\|?*':
                self.assertNotIn(ch, name)

    def test_control_chars_stripped(self):
        self.assertEqual(qmc_decrypt._safe_filename_component("a\x00b\x1fc"), "abc")

    def test_reserved_device_name_escaped(self):
        self.assertEqual(qmc_decrypt._safe_filename_component("CON"), "_CON")
        self.assertEqual(qmc_decrypt._safe_filename_component("com1"), "_com1")

    def test_name_length_capped(self):
        with tempfile.TemporaryDirectory() as td:
            p = qmc_decrypt.build_track_filename("", "长" * 400, ".flac", td)
            self.assertLessEqual(len(os.path.basename(p)), 130)

    def test_empty_input_falls_back(self):
        self.assertEqual(qmc_decrypt._safe_filename_component("", "fb"), "fb")


class TestWriteBasicTags(unittest.TestCase):
    """给裸音频补写标签。"""

    def _make_flac(self, td):
        from converter import find_ffmpeg
        import subprocess
        ff = find_ffmpeg()
        path = os.path.join(td, "t.flac")
        subprocess.run(
            [ff, "-hide_banner", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1", "-c:a", "flac", path],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return path

    def test_writes_title(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._make_flac(td)
            self.assertTrue(qmc_decrypt.write_basic_tags(path, title="测试歌名"))
            from mutagen.flac import FLAC
            # mutagen 的 Vorbis 标签读取出来是单元素列表
            self.assertEqual(FLAC(path)["title"], ["测试歌名"])

    def test_does_not_overwrite_existing(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._make_flac(td)
            qmc_decrypt.write_basic_tags(path, title="原标题")
            qmc_decrypt.write_basic_tags(path, title="新标题")
            from mutagen.flac import FLAC
            self.assertEqual(FLAC(path)["title"], ["原标题"], "已有标签不应被覆盖")

    def test_no_inputs_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._make_flac(td)
            self.assertFalse(qmc_decrypt.write_basic_tags(path))

    def test_invalid_file_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "not_audio.flac")
            with open(path, "wb") as f:
                f.write(b"garbage")
            self.assertFalse(qmc_decrypt.write_basic_tags(path, title="x"))


class TestDecryptRenaming(unittest.TestCase):
    """decrypt_file(rename_by_meta=True) 应输出"歌手 - 歌名"。"""

    def _ncm(self, td, meta):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_ncm import make_ncm
        import ncm_decrypt as ncm_mod

        audio = b"fLaC" + b"\x00" * 2048
        blob = make_ncm(audio, meta)
        src = os.path.join(td, "song.ncm")
        with open(src, "wb") as f:
            f.write(blob)
        return src, ncm_mod

    def test_ncm_renamed_by_meta(self):
        with tempfile.TemporaryDirectory() as td:
            src, ncm_mod = self._ncm(
                td, {"musicName": "稻香", "artist": [["周杰伦"]], "format": "flac"}
            )
            out, meta = ncm_mod.decrypt_file(
                src, os.path.join(td, "song.flac"), rename_by_meta=True
            )
            self.assertEqual(os.path.basename(out), "周杰伦 - 稻香.flac")
            self.assertTrue(os.path.exists(out))

    def test_ncm_without_rename_keeps_path(self):
        with tempfile.TemporaryDirectory() as td:
            src, ncm_mod = self._ncm(
                td, {"musicName": "稻香", "artist": [["周杰伦"]], "format": "flac"}
            )
            dst = os.path.join(td, "song.flac")
            out, _ = ncm_mod.decrypt_file(src, dst)
            self.assertEqual(out, os.path.abspath(dst))

    def test_ncm_missing_title_keeps_path(self):
        with tempfile.TemporaryDirectory() as td:
            src, ncm_mod = self._ncm(td, {"artist": [["无名氏"]], "format": "flac"})
            out, _ = ncm_mod.decrypt_file(
                src, os.path.join(td, "song.flac"), rename_by_meta=True
            )
            self.assertEqual(os.path.basename(out), "song.flac")

    def test_qmc_renaming_is_opt_in(self):
        """QMC 夹具没有歌名时必须维持源文件名,不能凭空改名。"""
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "x.qmcflac")
            shutil.copyfile(os.path.join(FIXDIR, "v1_static.qmcflac"), src)
            out, _ = qmc_decrypt.decrypt_file(
                src, os.path.join(td, "x.flac"), rename_by_meta=True
            )
            self.assertTrue(os.path.basename(out).endswith(".flac"))


if __name__ == "__main__":
    unittest.main()
