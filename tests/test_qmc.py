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


if __name__ == "__main__":
    unittest.main()
