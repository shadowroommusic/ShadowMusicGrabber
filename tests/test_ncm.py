# -*- coding: utf-8 -*-
"""NCM 解密引擎测试:构造标准 NCM 文件(按官方算法加密),验证解密往返。"""

import base64
import json
import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Crypto.Cipher import AES  # noqa: E402
from Crypto.Util.Padding import pad  # noqa: E402

import ncm_decrypt  # noqa: E402
from ncm_decrypt import CORE_KEY, META_KEY, NCM_MAGIC  # noqa: E402

# 1x1 红色 PNG
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def build_key_box(key: bytes) -> list:
    """与 ncm_decrypt 相同的变体 KSA(加密侧)。"""
    box = list(range(256))
    c = 0
    last_byte = 0
    key_offset = 0
    key_len = len(key)
    for i in range(256):
        swap = box[i]
        c = (swap + last_byte + key[key_offset]) & 0xFF
        key_offset += 1
        if key_offset >= key_len:
            key_offset = 0
        box[i] = box[c]
        box[c] = swap
        last_byte = c
    return box


def encrypt_stream(chunk: bytes, box: list) -> bytes:
    out = bytearray(len(chunk))
    for i, b in enumerate(chunk):
        j = (i + 1) & 0xFF
        out[i] = b ^ box[(box[j] + box[(box[j] + j) & 0xFF]) & 0xFF]
    return bytes(out)


def make_ncm(audio: bytes, meta_json: dict, cover: bytes = TINY_PNG,
             box_seed: bytes = None) -> bytes:
    """按官方格式构造 .ncm 文件字节。"""
    box_seed = box_seed or b"0123456789abcdef"
    key_material = b"\x80" + b"\x11" * 16 + box_seed  # 17 字节 key + box 种子
    # key 段: AES(CORE_KEY) -> XOR 0x64
    enc_key = AES.new(CORE_KEY, AES.MODE_ECB).encrypt(pad(key_material, 16))
    enc_key = bytes(b ^ 0x64 for b in enc_key)

    # 元数据段
    payload = b"music:" + json.dumps(meta_json, ensure_ascii=False).encode("utf-8")
    enc_meta = AES.new(META_KEY, AES.MODE_ECB).encrypt(pad(payload, 16))
    b64 = base64.b64encode(enc_meta)
    meta_block = b"163 key(Don't modify):" + b64
    meta_block = bytes(b ^ 0x63 for b in meta_block)

    cover_block = cover + b"\x00" * 10  # image_space > image_size 的 padding

    out = bytearray()
    out += NCM_MAGIC
    out += b"\x02\x00"  # version
    out += struct.pack("<I", len(enc_key)) + enc_key
    out += struct.pack("<I", len(meta_block)) + meta_block
    out += b"\x00\x00\x00\x00" + b"\x00"  # crc32 + image version
    out += struct.pack("<I", len(cover_block)) + struct.pack("<I", len(cover))
    out += cover_block

    box = build_key_box(box_seed)
    # 分块加密音频
    for start in range(0, len(audio), ncm_decrypt.CHUNK_SIZE):
        out += encrypt_stream(audio[start:start + ncm_decrypt.CHUNK_SIZE], box)
    return bytes(out)


class TestBuildKeyBox(unittest.TestCase):
    def test_deterministic(self):
        b1 = ncm_decrypt.build_key_box(b"abc")
        b2 = ncm_decrypt.build_key_box(b"abc")
        self.assertEqual(b1, b2)
        self.assertEqual(len(b1), 256)
        self.assertEqual(sorted(b1), list(range(256)), "box 应为 0-255 的排列")

    def test_different_keys(self):
        self.assertNotEqual(
            ncm_decrypt.build_key_box(b"abc"), ncm_decrypt.build_key_box(b"def"))

    def test_stream_decrypt_matches_reference(self):
        """The optimized C-level XOR must remain byte-for-byte compatible."""
        import os

        box = ncm_decrypt.build_key_box(b"0123456789abcdef")
        encrypted = os.urandom(ncm_decrypt.CHUNK_SIZE + 37)
        expected = bytearray()
        for i, value in enumerate(encrypted):
            j = (i + 1) & 0xFF
            expected.append(value ^ box[(box[j] + box[(box[j] + j) & 0xFF]) & 0xFF])
        actual = bytearray()
        for start in range(0, len(encrypted), ncm_decrypt.CHUNK_SIZE):
            actual += ncm_decrypt._decrypt_stream_block(
                encrypted[start:start + ncm_decrypt.CHUNK_SIZE], box
            )
        self.assertEqual(actual, expected)


class TestDecryptNcm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs("_ut_ncm", exist_ok=True)

    def _flac_audio(self) -> bytes:
        import subprocess
        from converter import find_ffmpeg
        ff = find_ffmpeg()
        src = os.path.join("_ut_ncm", "s.wav")
        subprocess.run(
            [ff, "-hide_banner", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1", "-c:a", "pcm_s16le", src],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        flac = os.path.join("_ut_ncm", "s.flac")
        subprocess.run(
            [ff, "-hide_banner", "-y", "-i", src, "-c:a", "flac", flac],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with open(flac, "rb") as f:
            return f.read()

    def test_roundtrip_flac(self):
        audio = self._flac_audio()
        meta = {"musicName": "测试歌曲", "artist": [["测试歌手"]],
                "album": "测试专辑", "bitrate": 1411200,
                "duration": 1000, "format": "flac"}
        ncm = make_ncm(audio, meta)
        out_audio, out_meta, out_cover = ncm_decrypt.decrypt_ncm_bytes(ncm)
        self.assertTrue(out_audio.startswith(b"fLaC"), "应识别为 FLAC")
        self.assertEqual(out_audio, audio, "音频应无损还原")
        self.assertEqual(out_meta.music_name, "测试歌曲")
        self.assertEqual(out_meta.artist, "测试歌手")
        self.assertEqual(out_meta.album, "测试专辑")
        self.assertEqual(out_meta.format, "flac")
        self.assertEqual(out_cover, TINY_PNG, "封面应还原")

    def test_roundtrip_mp3(self):
        # 构造内嵌 MP3(ID3v2 头)
        import subprocess
        from converter import find_ffmpeg
        ff = find_ffmpeg()
        src = os.path.join("_ut_ncm", "s.wav")
        mp3 = os.path.join("_ut_ncm", "s.mp3")
        subprocess.run(
            [ff, "-hide_banner", "-y", "-i", src, "-c:a", "libmp3lame", "-b:a", "128k", mp3],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with open(mp3, "rb") as f:
            audio = f.read()
        ncm = make_ncm(audio, {"musicName": "MP3歌", "artist": [["A"]], "format": "mp3"})
        out_audio, out_meta, _ = ncm_decrypt.decrypt_ncm_bytes(ncm)
        self.assertEqual(out_audio, audio)
        self.assertEqual(ncm_decrypt.detect_audio_format(out_audio), "mp3")

    def test_cover_absent(self):
        audio = b"fLaC" + b"\x00" * 100
        ncm = make_ncm(audio, {"musicName": "x", "format": "flac"}, cover=b"")
        out_audio, meta, cover = ncm_decrypt.decrypt_ncm_bytes(ncm)
        self.assertEqual(cover, b"")
        self.assertEqual(out_audio, audio)

    def test_invalid_magic(self):
        with self.assertRaises(ncm_decrypt.NcmError):
            ncm_decrypt.decrypt_ncm_bytes(b"NOTNCMFILE" + b"\x00" * 64)


class TestDecryptFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs("_ut_ncm", exist_ok=True)

    def test_full_file_flow(self):
        audio = b"fLaC" + b"\x00" * 4096 + b"\xff\xf8\x00\x00"
        meta = {"musicName": "文件测试", "artist": [["歌手1"], ["歌手2"]],
                "album": "专辑", "format": "flac"}
        ncm = make_ncm(audio, meta)
        src = os.path.join("_ut_ncm", "test_song.ncm")
        with open(src, "wb") as f:
            f.write(ncm)
        dst = os.path.join("_ut_ncm", "out.flac")
        out, out_meta = ncm_decrypt.decrypt_file(src, dst)
        self.assertTrue(os.path.exists(out))
        with open(out, "rb") as f:
            self.assertTrue(f.read().startswith(b"fLaC"))
        self.assertEqual(out_meta.artist, "歌手1/歌手2")

    def test_large_audio_is_decrypted_in_bounded_chunks(self):
        """The file API must not load the encrypted audio payload at once."""
        audio = b"fLaC" + os.urandom(ncm_decrypt.CHUNK_SIZE * 2 + 137)
        ncm = make_ncm(audio, {"musicName": "分块测试", "format": "flac"})
        src = os.path.join("_ut_ncm", "chunked_song.ncm")
        dst = os.path.join("_ut_ncm", "chunked_song.flac")
        with open(src, "wb") as f:
            f.write(ncm)

        original_read_exact = ncm_decrypt._read_exact
        audio_read_sizes = []

        def track_audio_reads(stream, size, context):
            if context == "in audio data":
                audio_read_sizes.append(size)
            return original_read_exact(stream, size, context)

        with (
            mock.patch.object(ncm_decrypt, "_read_exact", side_effect=track_audio_reads),
            mock.patch.object(ncm_decrypt, "write_flac_meta"),
        ):
            out, _ = ncm_decrypt.decrypt_file(src, dst)

        with open(out, "rb") as f:
            self.assertEqual(f.read(), audio)
        self.assertEqual(
            audio_read_sizes,
            [ncm_decrypt.CHUNK_SIZE, ncm_decrypt.CHUNK_SIZE, 141],
        )

    def test_mp3_file_flow_converts_to_flac(self):
        """实际走过 .ncm 解密、临时 MP3 和 ffmpeg 转 FLAC 路径。"""
        import subprocess
        from converter import find_ffmpeg

        ff = find_ffmpeg()
        self.assertTrue(ff, "测试需要 ffmpeg")
        wav = os.path.join("_ut_ncm", "mp3_source.wav")
        mp3 = os.path.join("_ut_ncm", "mp3_source.mp3")
        subprocess.run(
            [ff, "-hide_banner", "-nostdin", "-y", "-f", "lavfi", "-i",
             "sine=frequency=523:duration=1", "-c:a", "pcm_s16le", wav],
            check=True, capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        subprocess.run(
            [ff, "-hide_banner", "-nostdin", "-y", "-i", wav,
             "-c:a", "libmp3lame", "-b:a", "128k", mp3],
            check=True, capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with open(mp3, "rb") as f:
            ncm = make_ncm(
                f.read(),
                {"musicName": "MP3 文件", "artist": [["测试"]], "format": "mp3"},
            )
        src = os.path.join("_ut_ncm", "mp3_song.ncm")
        dst = os.path.join("_ut_ncm", "mp3_song.flac")
        with open(src, "wb") as f:
            f.write(ncm)
        out, meta = ncm_decrypt.decrypt_file(src, dst, ffmpeg=ff)
        self.assertEqual(out, os.path.abspath(dst))
        self.assertEqual(meta.music_name, "MP3 文件")
        with open(out, "rb") as f:
            self.assertEqual(f.read(4), b"fLaC")

    def test_missing_file(self):
        with self.assertRaises(ncm_decrypt.NcmError):
            ncm_decrypt.decrypt_file("_ut_ncm/nope.ncm", "_ut_ncm/x.flac")

    def test_zero_filled_file_has_actionable_error(self):
        """A same-sized zero placeholder should not look like a crypto bug."""
        src = os.path.join("_ut_ncm", "zero_placeholder.ncm")
        dst = os.path.join("_ut_ncm", "zero_placeholder.flac")
        with open(src, "wb") as f:
            f.write(b"\x00" * (ncm_decrypt.CHUNK_SIZE * 2 + 17))
        with self.assertRaisesRegex(ncm_decrypt.NcmError, "文件内容全为 0"):
            ncm_decrypt.decrypt_file(src, dst)
        self.assertFalse(os.path.exists(dst))

    def test_wrong_ext(self):
        with self.assertRaises(ncm_decrypt.NcmError):
            ncm_decrypt.decrypt_file("_ut_ncm/not_ncm.txt", "_ut_ncm/x.flac")

    def test_make_output_path(self):
        os.makedirs("_ut_ncm", exist_ok=True)
        for f in os.listdir("_ut_ncm"):
            if f.startswith("song") and f.endswith(".flac"):
                os.remove(os.path.join("_ut_ncm", f))
        p1 = ncm_decrypt.make_output_path("_ut_ncm/song.ncm", "_ut_ncm")
        self.assertTrue(p1.endswith("song.flac"))
        open(p1, "w").close()
        p2 = ncm_decrypt.make_output_path("_ut_ncm/song.ncm", "_ut_ncm")
        self.assertTrue(p2.endswith("song_1.flac"), p2)

    def test_reserved_batch_paths(self):
        reserved = set()
        p1 = ncm_decrypt.make_output_path("C:/music/song.ncm", "_ut_ncm", reserved)
        p2 = ncm_decrypt.make_output_path("D:/other/song.ncm", "_ut_ncm", reserved)
        self.assertNotEqual(os.path.normcase(os.path.abspath(p1)), os.path.normcase(os.path.abspath(p2)))


class NcmMetaFieldsTests(unittest.TestCase):
    """NCM 元数据里可用但此前未写入的字段(专辑艺人/日期/曲号/碟号/流派)。"""

    def _meta(self, **json_fields):
        payload = {"musicName": "歌名", "artist": [["歌手A"]], "album": "专辑"}
        payload.update(json_fields)
        return ncm_decrypt.NcmMeta(
            music_name=payload["musicName"],
            artist="歌手A",
            album=payload["album"],
            raw_json=payload,
        )

    def test_album_artist_falls_back_to_artist(self):
        self.assertEqual(self._meta().album_artist, "歌手A")

    def test_album_artist_from_field(self):
        self.assertEqual(self._meta(albumArtist="专辑歌手").album_artist, "专辑歌手")

    def test_album_artist_from_list(self):
        self.assertEqual(self._meta(albumArtist=["甲", "乙"]).album_artist, "甲")

    def test_publish_date_from_millis(self):
        # 2021-01-01T00:00:00Z
        self.assertEqual(self._meta(publishTime=1609459200000).publish_date, "2021-01-01")

    def test_publish_date_from_string(self):
        self.assertEqual(self._meta(publishTimeText="2019-05-06").publish_date, "2019-05-06")

    def test_publish_date_absent(self):
        self.assertEqual(self._meta().publish_date, "")

    def test_publish_date_zero_is_ignored(self):
        self.assertEqual(self._meta(publishTime=0).publish_date, "")

    def test_track_number(self):
        self.assertEqual(self._meta(no=7).track_number, "7")
        self.assertEqual(self._meta(no="3").track_number, "3")

    def test_track_number_zero_is_ignored(self):
        """网易云用 no=0 表示未知曲号,不该写进标签。"""
        self.assertEqual(self._meta(no=0).track_number, "")

    def test_disc_number(self):
        self.assertEqual(self._meta(discNumber=2).disc_number, "2")
        self.assertEqual(self._meta(discNumber=0).disc_number, "")

    def test_genre_list_is_joined(self):
        self.assertEqual(self._meta(genre=["流行", "摇滚"]).genre, "流行/摇滚")

    def test_empty_meta_is_safe(self):
        empty = ncm_decrypt.NcmMeta()
        self.assertEqual(empty.album_artist, "")
        self.assertEqual(empty.publish_date, "")
        self.assertEqual(empty.track_number, "")
        self.assertEqual(empty.disc_number, "")
        self.assertEqual(empty.genre, "")


class NcmFlacTagTests(unittest.TestCase):
    """write_flac_meta 应把上述字段与封面一起写进 FLAC。"""

    def test_writes_all_fields_and_cover(self):
        import subprocess
        from converter import find_ffmpeg

        os.makedirs("_ut_ncm", exist_ok=True)
        ff = find_ffmpeg()
        src = os.path.join("_ut_ncm", "tag.wav")
        flac = os.path.join("_ut_ncm", "tag_out.flac")
        subprocess.run(
            [ff, "-hide_banner", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1", "-c:a", "flac", flac],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        payload = {
            "musicName": "完整标签", "artist": [["主唱"]],
            "album": "某专辑", "albumArtist": "某组合",
            "publishTime": 1609459200000, "no": 5, "discNumber": 1,
            "genre": ["流行"],
        }
        meta = ncm_decrypt.NcmMeta(
            music_name="完整标签", artist="主唱", album="某专辑", raw_json=payload
        )
        ncm_decrypt.write_flac_meta(flac, meta, TINY_PNG, "完整标签 - 主唱")

        from mutagen.flac import FLAC
        audio = FLAC(flac)
        # mutagen 的 Vorbis 标签读取出来是单元素列表
        self.assertEqual(audio["title"], ["完整标签"])
        self.assertEqual(audio["artist"], ["主唱"])
        self.assertEqual(audio["album"], ["某专辑"])
        self.assertEqual(audio["albumartist"], ["某组合"])
        self.assertEqual(audio["date"], ["2021-01-01"])
        self.assertEqual(audio["tracknumber"], ["5"])
        self.assertEqual(audio["discnumber"], ["1"])
        self.assertEqual(audio["genre"], ["流行"])
        self.assertEqual(len(audio.pictures), 1, "封面应写入")


if __name__ == "__main__":
    unittest.main()
