# -*- coding: utf-8 -*-
"""NCM 解密引擎:将网易云音乐 .ncm 加密文件解密为 FLAC(无损)。

算法来自开源项目 taurusxin/ncmdump 与 unlock-music(纯本地解密,无联网)。
仅用于解密用户自己合法下载的音乐文件。
"""

from __future__ import annotations

import base64
import json
import os
import struct
import threading
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from Crypto.Util.strxor import strxor

# NCM 文件头魔数 "CTENFDAM"
NCM_MAGIC = b"CTENFDAM"

# 固定密钥(公开于 ncmdump 项目)
CORE_KEY = bytes.fromhex("687A4852416D736F356B496E62617857")  # hzHRAmso5kInbaxW
META_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")  # #14ljk_!\]&0U<'(
CHUNK_SIZE = 0x8000

AUDIO_EXT_MAP = {b"fLaC": "flac", b"ID3": "mp3", b"\xff\xfb": "mp3", b"\xff\xf3": "mp3", b"\xff\xf2": "mp3"}


class NcmError(Exception):
    pass


@dataclass
class NcmMeta:
    music_name: str = ""
    artist: str = ""
    album: str = ""
    bitrate: int = 0
    duration: int = 0
    format: str = ""  # 原始内嵌格式: flac / mp3
    raw_json: dict = field(default_factory=dict)


@dataclass
class NcmTask:
    src: str
    dst: str
    status: str = "排队中"
    progress: float = 0.0
    error: str = ""
    meta: Optional[NcmMeta] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_progress(self, p: float):
        with self._lock:
            self.progress = p


def build_key_box(key: bytes) -> list[int]:
    """变体 KSA(taurusxin/unlock-music 同款)。"""
    box = list(range(256))
    c = 0
    last_byte = 0
    key_offset = 0
    key_len = len(key)
    if not key_len:
        raise NcmError("NCM key data is empty")
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


def _build_stream_key(box: list[int]) -> bytes:
    """Build the repeating 256-byte NCM keystream once.

    The stream index is ``(i + 1) & 0xff``, so every 256 bytes use the same
    key sequence.  Precomputing it avoids a Python loop for every audio byte.
    The actual XOR is then performed by PyCryptodome's C implementation,
    which keeps the Tk event loop responsive while large files are decoded.
    """
    return bytes(
        box[(box[j] + box[(box[j] + j) & 0xFF]) & 0xFF]
        for i in range(256)
        for j in ((i + 1) & 0xFF,)
    )


def _decrypt_stream_block(
    chunk: bytes, box: list[int], stream_key: Optional[bytes] = None
) -> bytearray:
    """Decrypt one chunk using the repeating NCM keystream."""
    if not chunk:
        return bytearray()
    stream_key = stream_key or _build_stream_key(box)
    repeats = (len(chunk) + len(stream_key) - 1) // len(stream_key)
    keystream = (stream_key * repeats)[: len(chunk)]
    return bytearray(strxor(chunk, keystream))


def _parse_metadata(meta_raw: bytes) -> NcmMeta:
    """Decode the optional encrypted metadata block.

    Bad metadata is deliberately non-fatal: valid NCM audio can still be
    recovered when its tag block is absent or malformed.
    """
    try:
        b64 = meta_raw[22:]  # Skip "163 key(Don't modify):"
        encrypted_meta = base64.b64decode(b64, validate=True)
        if not encrypted_meta or len(encrypted_meta) % 16:
            raise ValueError("invalid metadata AES block length")
        dec = unpad(AES.new(META_KEY, AES.MODE_ECB).decrypt(encrypted_meta), 16)
        payload = dec[6:]  # Skip "music:"
        obj = json.loads(payload.decode("utf-8"))
        return NcmMeta(
            music_name=obj.get("musicName", ""),
            artist="/".join(a[0] for a in obj.get("artist", []) if a),
            album=obj.get("album", ""),
            bitrate=int(obj.get("bitrate", 0) or 0),
            duration=int(obj.get("duration", 0) or 0),
            format=obj.get("format", ""),
            raw_json=obj,
        )
    except Exception:  # noqa: BLE001 - malformed metadata is non-fatal
        return NcmMeta()


def _read_exact(stream, size: int, context: str) -> bytes:
    """Read exactly *size* bytes or report a truncated NCM file."""
    data = stream.read(size)
    if len(data) != size:
        raise NcmError(f"NCM file is truncated {context}")
    return data


def _is_zero_filled(stream, file_size: int) -> bool:
    """Return whether the complete file consists only of zero bytes.

    A failed cloud/download copy can leave a correctly sized, zero-filled
    placeholder on disk.  Detect it only after the normal magic check fails so
    valid NCM files keep the same bounded streaming path and error behavior.
    """
    position = stream.tell()
    try:
        stream.seek(0)
        remaining = file_size
        while remaining > 0:
            chunk = stream.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                return False
            if any(chunk):
                return False
            remaining -= len(chunk)
        return remaining == 0
    finally:
        stream.seek(position)


def _read_ncm_header(stream, file_size: int) -> tuple[list[int], NcmMeta, bytes, int]:
    """Read the small NCM header and leave *stream* at encrypted audio.

    Audio payloads are intentionally excluded from this parser so callers can
    decrypt large files with bounded memory.
    """
    if file_size < 14:
        raise NcmError("NCM file is truncated")
    magic = _read_exact(stream, 8, "before magic")
    if magic != NCM_MAGIC:
        if magic == b"\x00" * 8 and _is_zero_filled(stream, file_size):
            raise NcmError(
                "文件内容全为 0，疑似未完成下载或云盘占位文件；"
                "请重新下载/同步后再试"
            )
        raise NcmError("不是有效的 NCM 文件(魔数错误)")
    _read_exact(stream, 2, "before version")

    key_len = struct.unpack("<I", _read_exact(stream, 4, "before key length"))[0]
    remaining = file_size - stream.tell()
    if key_len <= 0 or key_len > remaining:
        raise NcmError("NCM 文件损坏(key 长度非法)")
    key_data = bytes(b ^ 0x64 for b in _read_exact(stream, key_len, "in key data"))
    if len(key_data) % 16:
        raise NcmError("NCM key data has an invalid AES block length")
    try:
        key_data = unpad(AES.new(CORE_KEY, AES.MODE_ECB).decrypt(key_data), 16)[17:]
    except (ValueError, IndexError) as e:
        raise NcmError(f"key 解密失败: {e}") from e
    if not key_data:
        raise NcmError("key 数据为空")
    box = build_key_box(key_data)

    meta_len = struct.unpack("<I", _read_exact(stream, 4, "before metadata"))[0]
    remaining = file_size - stream.tell()
    if meta_len > remaining:
        raise NcmError("NCM 文件损坏(元数据长度非法)")
    meta = NcmMeta()
    if meta_len:
        meta_raw = bytes(b ^ 0x63 for b in _read_exact(stream, meta_len, "in metadata"))
        meta = _parse_metadata(meta_raw)

    _read_exact(stream, 5, "before cover data")  # CRC32 + image version

    # A few old/partial files have no cover block.  Match decrypt_ncm_bytes:
    # in that case all remaining bytes are audio.
    if file_size - stream.tell() < 4:
        return box, meta, b"", stream.tell()

    image_space = struct.unpack("<I", _read_exact(stream, 4, "before cover size"))[0]
    image_size = struct.unpack("<I", _read_exact(stream, 4, "before cover size"))[0]
    if image_size > image_space:
        raise NcmError("NCM cover size is invalid")
    if image_space > file_size - stream.tell():
        raise NcmError("NCM cover block length is invalid")
    cover = _read_exact(stream, image_size, "in cover data") if image_size else b""
    stream.seek(image_space - image_size, os.SEEK_CUR)
    return box, meta, cover, stream.tell()


def _decrypt_audio_to_file(
    src_stream,
    output_path: str,
    audio_size: int,
    box: list[int],
    on_progress: Optional[Callable[[float], None]] = None,
) -> str:
    """Decrypt NCM audio directly to disk and return its detected format."""
    if audio_size <= 0:
        raise NcmError("NCM file contains no audio data")

    stream_key = _build_stream_key(box)
    processed = 0
    fmt = "flac"
    with open(output_path, "wb") as output:
        while processed < audio_size:
            chunk = _read_exact(
                src_stream,
                min(CHUNK_SIZE, audio_size - processed),
                "in audio data",
            )
            decrypted = _decrypt_stream_block(chunk, box, stream_key)
            if processed == 0:
                fmt = detect_audio_format(decrypted)
            output.write(decrypted)
            processed += len(chunk)
            if on_progress:
                on_progress(processed / audio_size * 100.0)
            # CHUNK_SIZE is divisible by the 256-byte key period, so each
            # block starts at the same key position as the in-memory path.
            if (processed // CHUNK_SIZE) % 32 == 0:
                time.sleep(0)
    return fmt


def decrypt_ncm_bytes(
    data: bytes,
    on_progress: Optional[Callable[[float], None]] = None,
) -> tuple[bytes, NcmMeta, bytes]:
    """解密整个 .ncm 文件内容,返回 (音频字节, 元数据, 封面图片字节)。"""
    if len(data) < 8 or data[:8] != NCM_MAGIC:
        raise NcmError("不是有效的 NCM 文件(魔数错误)")

    if len(data) < 14:
        raise NcmError("NCM file is truncated")
    pos = 10  # 8 magic + 2 version

    # ---- key 数据(XOR 0x64 -> AES-128-ECB 解密 -> 去掉填充 -> 跳过前 17 字节)----
    if pos + 4 > len(data):
        raise NcmError("NCM file is truncated before key length")
    key_len = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    if key_len <= 0 or pos + key_len > len(data):
        raise NcmError("NCM 文件损坏(key 长度非法)")
    key_data = bytes(b ^ 0x64 for b in data[pos:pos + key_len])
    pos += key_len

    if len(key_data) % 16:
        raise NcmError("NCM key data has an invalid AES block length")
    try:
        key_data = unpad(AES.new(CORE_KEY, AES.MODE_ECB).decrypt(key_data), 16)[17:]
    except (ValueError, IndexError) as e:
        raise NcmError(f"key 解密失败: {e}") from e
    if not key_data:
        raise NcmError("key 数据为空")

    box = build_key_box(key_data)

    # ---- 元数据(XOR 0x63 -> 跳过 22 字节标识 -> base64 -> AES 解密 -> 跳过 music:)----
    if pos + 4 > len(data):
        raise NcmError("NCM file is truncated before metadata")
    meta_len = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    meta_obj = NcmMeta()
    if meta_len > 0:
        if pos + meta_len > len(data):
            raise NcmError("NCM 文件损坏(元数据长度非法)")
        meta_raw = bytes(b ^ 0x63 for b in data[pos:pos + meta_len])
        pos += meta_len
        meta_obj = _parse_metadata(meta_raw)
    else:
        pos += 0

    # ---- skip crc32(4) + image version(1) ----
    if pos + 5 > len(data):
        raise NcmError("NCM file is truncated before cover data")
    pos += 5

    # ---- 封面 ----
    cover = b""
    if pos + 4 <= len(data):
        image_space = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if pos + 4 > len(data):
            raise NcmError("NCM file is truncated before cover size")
        image_size = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if image_size > image_space:
            raise NcmError("NCM cover size is invalid")
        if image_space > len(data) - pos:
            raise NcmError("NCM cover block length is invalid")
        if image_size > 0 and pos + image_size <= len(data):
            cover = data[pos:pos + image_size]
            pos += image_size
        pos += image_space - image_size
        pos = min(pos, len(data))

    # ---- 音频流解密 ----
    audio = data[pos:]
    if not audio:
        raise NcmError("NCM file contains no audio data")
    stream_key = _build_stream_key(box)
    out = bytearray()
    total_audio = len(audio)
    for chunk_index, start in enumerate(range(0, total_audio, CHUNK_SIZE)):
        out += _decrypt_stream_block(
            audio[start:start + CHUNK_SIZE], box, stream_key
        )
        if on_progress:
            on_progress(min(100.0, (start + CHUNK_SIZE) / total_audio * 100.0))
        # The XOR itself runs in C, but yielding periodically keeps the GUI
        # responsive even on systems where extension calls hold the GIL.
        if chunk_index % 32 == 31:
            time.sleep(0)
    return bytes(out), meta_obj, cover


def detect_audio_format(audio: bytes) -> str:
    """按文件头识别解密后的音频格式。"""
    for magic, ext in AUDIO_EXT_MAP.items():
        if audio.startswith(magic):
            return ext
    return "flac"  # 默认 flac


def write_flac_meta(path: str, meta: NcmMeta, cover: bytes, identifier: str = "") -> None:
    """向 FLAC 写入标题/艺术家/专辑/封面。"""
    from mutagen.flac import FLAC, Picture

    audio = FLAC(path)
    if meta.music_name:
        audio["title"] = meta.music_name
    if meta.artist:
        audio["artist"] = meta.artist
    if meta.album:
        audio["album"] = meta.album
    if identifier:
        audio["comment"] = identifier
    if cover:
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = "image/png" if cover[:4] == b"\x89PNG" else "image/jpeg"
        pic.data = cover
        audio.add_picture(pic)
    audio.save()


def decrypt_file(
    src: str,
    dst: str,
    on_progress: Optional[Callable[[float], None]] = None,
    on_line: Optional[Callable[[str], None]] = None,
    ffmpeg: Optional[str] = None,
) -> tuple[str, NcmMeta]:
    """解密单个 .ncm 文件,输出 FLAC(若内嵌为 MP3 则用 ffmpeg 转 FLAC)。

    返回 (输出路径, 元数据)。
    """
    if not os.path.exists(src):
        raise NcmError(f"文件不存在: {src}")
    if not src.lower().endswith(".ncm"):
        raise NcmError("只支持 .ncm 文件")

    dst = os.path.abspath(os.fspath(dst))
    dst_dir = os.path.dirname(dst) or "."
    os.makedirs(dst_dir, exist_ok=True)
    fd, temp_dst = tempfile.mkstemp(
        # Keep the real output extension on the temporary path. FFmpeg infers
        # the muxer from the destination suffix; a bare partial suffix makes
        # the MP3 -> FLAC branch fail before it can write anything.
        prefix=f".{os.path.basename(dst)}.", suffix=".flac", dir=dst_dir
    )
    os.close(fd)
    try:
        os.remove(temp_dst)
    except OSError:
        pass

    try:
        with open(src, "rb") as source:
            file_size = os.fstat(source.fileno()).st_size
            box, meta, cover, audio_offset = _read_ncm_header(source, file_size)
            if on_progress:
                on_progress(5.0)
            audio_size = file_size - audio_offset
            fmt = _decrypt_audio_to_file(
                source,
                temp_dst,
                audio_size,
                box,
                on_progress=(
                    (lambda p: on_progress(5.0 + p * 0.45))
                    if on_progress
                    else None
                ),
            )

        if on_progress:
            on_progress(50.0)

        if fmt != "flac":
            # 内嵌 mp3 -> 用 ffmpeg 转 flac(无损容器)
            tmp_mp3 = temp_dst + ".mp3"
            os.replace(temp_dst, tmp_mp3)
            try:
                _transcode_to_flac(tmp_mp3, temp_dst, ffmpeg, on_progress)
            finally:
                if os.path.exists(tmp_mp3):
                    os.remove(tmp_mp3)

        if on_progress:
            on_progress(80.0)

        # 写元数据与封面
        identifier = ""
        if meta.music_name:
            identifier = f"{meta.music_name} - {meta.artist}".strip(" -")
        try:
            write_flac_meta(temp_dst, meta, cover, identifier)
        except Exception as e:  # noqa: BLE001 - 元数据写入失败不致命
            if on_line:
                on_line(f"警告: 元数据写入失败: {e}")

        os.replace(temp_dst, dst)
    finally:
        if os.path.exists(temp_dst):
            os.remove(temp_dst)

    if on_progress:
        on_progress(100.0)
    return dst, meta


def _transcode_to_flac(
    src: str, dst: str, ffmpeg: Optional[str], on_progress: Optional[Callable[[float], None]]
) -> None:
    import subprocess

    from converter import find_ffmpeg

    ffmpeg_exe = ffmpeg or find_ffmpeg()
    if not ffmpeg_exe:
        raise NcmError("未找到 ffmpeg,无法将内嵌 MP3 转码为 FLAC")
    cmd = [
        ffmpeg_exe,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        src,
        "-map",
        "0:a:0",
        "-vn",
        "-c:a",
        "flac",
        "-compression_level",
        "8",
        "-f",
        "flac",
        dst,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.stdout is not None
    for _ in proc.stdout:
        pass
    proc.stdout.close()
    rc = proc.wait()
    if rc != 0 or not os.path.exists(dst):
        raise NcmError(f"ffmpeg 转码失败(退出码 {rc})")


def make_output_path(
    src: str, out_dir: str, reserved_paths: Optional[set[str]] = None
) -> str:
    """构造输出路径: 同名 .flac,避免覆盖或同批次重名。"""
    base = os.path.splitext(os.path.basename(src))[0]
    dst = os.path.join(out_dir, base + ".flac")
    reserved_paths = reserved_paths if reserved_paths is not None else set()
    i = 1
    while os.path.exists(dst) or os.path.normcase(os.path.abspath(dst)) in reserved_paths:
        dst = os.path.join(out_dir, f"{base}_{i}.flac")
        i += 1
    reserved_paths.add(os.path.normcase(os.path.abspath(dst)))
    return dst
