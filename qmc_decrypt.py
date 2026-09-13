# -*- coding: utf-8 -*-
"""QMC 解密引擎:将 QQ 音乐加密文件还原为普通音频(FLAC / MP3 / OGG / M4A)。

支持格式:
- QMCv1(整文件静态异或):.qmc0 / .qmc3(→mp3)、.qmcflac(→flac)、.qmcogg / .qmc2(→ogg)
- QMCv2(尾部内嵌密钥):.mflac / .mflac0 / .mflac1(→flac)、.mgg / .mgg0 / .mgg1 / .mggl(→ogg)、.mmp4(→m4a)
  尾部形态支持: 4 字节小端长度前缀 + Base64 密钥串(常见),以及新版 QTag 封装。
  密钥保护层支持: V1(TEA-CBC)与 V2("QQMusic EncV2,Key:" 双层混淆 + TEA-CBC)。
  数据密钥流按密钥长度选择: 256 字节 → Mask128;512 字节 → 强化 RC4;128 字节 → 直接掩码。

算法参考(均为 MIT 开源项目,本文件为独立的纯 Python 实现,并用其测试向量逐字节验证):
- MusicDecrypto(davidxuang)的 StaticCipher / QmcDecrypto / TEA —— 官方测试向量对照通过
- libtakiyasha(nukemiko)的 Mask128 / HardenedRC4 / TarsCppTCTEAWithModeCBC —— 密钥流逐字节对照通过
- qmcdump(MegrezZhu)的静态解密公式
纯本地解密,无任何联网行为。仅用于解密用户自己合法下载的音乐文件。
"""

from __future__ import annotations

import base64
import io
import os
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from math import tan
from typing import Callable, Optional

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

# QMCv1 静态异或表(256 字节,公开常量的十六进制形式)
_STATIC_BOX = bytes.fromhex(
    "77483273def2c0c895ec30b251c3e1a09ee69dcffa7f14d1ceb8dcc34a6793d6"
    "28c29170ca8da2a4f00861907e6fa2e0ebae3eb667c792f491b5f66c5e8440f7"
    "f31b027fd5ab418928f425cc5211ad4368a6418b84b5ff2c924a26d8476a7c95"
    "61cce6cbbb3f47588975c375a1d9afcc087317dcaa9aa21641d8a206c68bfc66"
    "349fcf1823a00a74e72b277092e9af37e68ca7bc62659cc208c988b3f343ac74"
    "2c0fd4afa1c30164954e489ff43578957a39d66aa06d40e84fa8ef111df31b3f"
    "3f07dd6f5b193019fbef0e37f00ecd1649fe5347131abda4f14019600eed6809"
    "065f4dcf3d1afe2077e4d9daf9a42b761c71db00bcfd0c6ca547f7f600794a11"
)

# QMCv2 密钥保护层参数
_V2_MAGIC = b"QQMusic EncV2,Key:"
_GARBLE_KEYS = (b"386ZJY!@#*$%^&)(", b"**#!(#$%&^a1cZ,T")
_TEA_ROUNDS = 32
_TEA_BLOCK = 8
_TEA_SALT_LEN = 2
_TEA_ZERO_LEN = 7

# 核心密钥:由固定公式生成(两个参考实现共用同一公式)
_CORE_KEY = bytes(int(abs(tan(106 + i * 0.1)) * 100) for i in range(8))

# 尾部密钥串的合理长度上限(实际样本约 364~1008 字符)
_MAX_TAIL_KEY_BYTES = 0x1000

CHUNK_SIZE = 1 << 20  # 流式解密分块(1 MiB)

QMC_V1_EXTS = {
    ".qmc0": ".mp3",
    ".qmc3": ".mp3",
    ".qmcflac": ".flac",
    ".qmcogg": ".ogg",
    ".qmc2": ".ogg",
}

QMC_V2_EXTS = {
    ".mflac": ".flac",
    ".mflac0": ".flac",
    ".mflac1": ".flac",
    ".mgg": ".ogg",
    ".mgg0": ".ogg",
    ".mgg1": ".ogg",
    ".mggl": ".ogg",
    ".mmp4": ".m4a",
}

QMC_EXTS = {**QMC_V1_EXTS, **QMC_V2_EXTS}

# 文件对话框用通配符
QMC_PATTERNS = ["*" + ext for ext in sorted(QMC_EXTS)]

# 解密结果的文件头特征(fLaC / OggS / ID3 / MP3 帧同步 / RIFF / MP4)
_ID3_MAGIC = b"ID3"
_FLAC_MAGIC = b"fLaC"
_OGG_MAGIC = b"OggS"
_RIFF_MAGIC = b"RIFF"


class QmcError(Exception):
    pass


@dataclass
class QmcMeta:
    music_name: str = ""
    artist: str = ""
    album: str = ""


@dataclass
class QmcTask:
    src: str
    dst: str
    status: str = "排队中"
    progress: float = 0.0
    error: str = ""
    meta: Optional[QmcMeta] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_progress(self, p: float):
        with self._lock:
            self.progress = p


# --------------------------------------------------------------------------
# 密钥流: QMCv1 / Mask128 / 强化 RC4
# --------------------------------------------------------------------------


def _static_mask128() -> bytes:
    """由 QMCv1 静态表推导 128 字节掩码(两种表述等价,已由数学与向量双重验证)。"""
    return bytes(_STATIC_BOX[(i * i + 0x1B) & 0xFF] for i in range(128))


def _mask128_from_key256(key: bytes) -> bytes:
    """QMCv2(Mask128 族)主密钥 → 128 字节掩码。"""
    mask = bytearray(128)
    for i in range(128):
        idx = (i * i + 71214) % 256
        v = key[idx]
        r = ((idx & 0x07) + 4) % 8
        mask[i] = ((v << r) % 256) | ((v >> r) % 256)
    return bytes(mask)


@lru_cache(maxsize=8)
def _mask128_pattern(mask: bytes) -> tuple[bytes, bytes]:
    """掩码密钥流的两个重复单元: 起始块(前 65534 字节)与普通块(之后每 32767 字节)。"""
    first = mask * 256
    return first + first[1:-1], first[:-1]


def _mask128_keystream(mask: bytes, offset: int, n: int) -> bytes:
    startblk, commonblk = _mask128_pattern(mask)
    out = bytearray()
    pos = offset
    while len(out) < n:
        if pos < len(startblk):
            chunk = startblk[pos:pos + (n - len(out))]
        else:
            rel = (pos - len(startblk)) % len(commonblk)
            chunk = commonblk[rel:rel + (n - len(out))]
        out += chunk
        pos += len(chunk)
    return bytes(out)


class _HardenedRc4:
    """QMCv2(RC4 族)的"强化版 RC4": 512 字节密钥,自有的 KSA/PRGA 与分段跳读。

    各分段都从同一初始 S 盒重启 PRGA,但带不同的初始跳过量;因此任一字节的
    密钥流都是同一条 PRGA 流 F 上的某个下标,可一次性预生成 F 后按段切片。
    """

    _FIRST_SEG = 128
    _SEG_SIZE = 5120

    def __init__(self, key: bytes):
        if not key or 0 in key:
            raise QmcError("RC4 密钥无效")
        self._key = bytes(key)
        key_len = len(self._key)
        box = bytearray(i % 256 for i in range(key_len))
        j = 0
        for i in range(key_len):
            j = (j + box[i] + self._key[i]) % key_len
            box[i], box[j] = box[j], box[i]
        self._box = box

        base = 1
        for v in self._key:
            if v == 0:
                continue
            nxt = (base * v) & 0xFFFFFFFF
            if nxt == 0 or nxt <= base:
                break
            base = nxt
        self._hash_base = base
        self._f = self._build_prga_stream(key_len + self._SEG_SIZE)

    def _build_prga_stream(self, n: int) -> bytes:
        key_len = len(self._key)
        box = bytearray(self._box)
        j = 0
        k = 0
        out = bytearray(n)
        for i in range(n):
            j = (j + 1) % key_len
            k = (box[j] + k) % key_len
            box[j], box[k] = box[k], box[j]
            out[i] = box[(box[j] + box[k]) % key_len]
        return bytes(out)

    def _segment_skip(self, segment: int) -> int:
        key_len = len(self._key)
        seed = self._key[segment % key_len]
        return int(self._hash_base / ((segment + 1) * seed) * 100) % key_len

    def keystream(self, offset: int, n: int) -> bytes:
        out = bytearray()
        pos = offset
        if pos < self._FIRST_SEG:
            cnt = min(n, self._FIRST_SEG - pos)
            for i in range(pos, pos + cnt):
                out.append(self._key[self._segment_skip(i)])
            pos = self._FIRST_SEG
        while len(out) < n:
            seg = pos // self._SEG_SIZE
            in_seg = pos % self._SEG_SIZE
            skip = self._segment_skip(seg)
            cnt = min(n - len(out), self._SEG_SIZE - in_seg)
            out += self._f[skip + in_seg:skip + in_seg + cnt]
            pos += cnt
        return bytes(out)


def _v1_keystream(offset: int, n: int) -> bytes:
    return _mask128_keystream(_static_mask128(), offset, n)


def _make_keystream(key: bytes) -> Callable[[int, int], bytes]:
    """按解密后的主密钥长度选择数据密钥流。"""
    if len(key) == 256:
        mask = _mask128_from_key256(key)
        return lambda offset, n: _mask128_keystream(mask, offset, n)
    if len(key) == 512:
        cipher = _HardenedRc4(key)
        return cipher.keystream
    if len(key) == 128:
        return lambda offset, n: _mask128_keystream(key, offset, n)
    raise QmcError(f"不支持的密钥长度: {len(key)}")


# --------------------------------------------------------------------------
# 密钥保护层: TEA(TarsCpp tc_tea, CBC)与 V1/V2 解密链
# --------------------------------------------------------------------------


def _tea_decrypt_block(block: bytes, key: bytes, rounds: int = _TEA_ROUNDS) -> bytes:
    v0 = int.from_bytes(block[:4], "big")
    v1 = int.from_bytes(block[4:8], "big")
    k0 = int.from_bytes(key[0:4], "big")
    k1 = int.from_bytes(key[4:8], "big")
    k2 = int.from_bytes(key[8:12], "big")
    k3 = int.from_bytes(key[12:16], "big")
    delta = 0x9E3779B9
    total = (delta * (rounds // 2)) & 0xFFFFFFFF
    for _ in range(rounds // 2):
        v1 = (v1 - (((v0 << 4) + k2) ^ (v0 + total) ^ ((v0 >> 5) + k3))) & 0xFFFFFFFF
        v0 = (v0 - (((v1 << 4) + k0) ^ (v1 + total) ^ ((v1 >> 5) + k1))) & 0xFFFFFFFF
        total = (total - delta) & 0xFFFFFFFF
    return v0.to_bytes(4, "big") + v1.to_bytes(4, "big")


def _tars_tea_cbc_decrypt(cipherdata: bytes, tea_key: bytes, rounds: int = _TEA_ROUNDS) -> bytes:
    """TarsCpp tc_tea 的 CBC 模式解密(QQ 音乐密钥保护层使用)。

    密文为 8 字节整数倍: 首块含填充信息与 2 字节盐,尾部按 7 字节零填充校验。
    """
    if len(cipherdata) < _TEA_BLOCK * 2 or len(cipherdata) % _TEA_BLOCK:
        raise QmcError("密钥密文长度非法")
    dest = bytearray(_tea_decrypt_block(cipherdata[:8], tea_key, rounds))
    pad_len = dest[0] & 0x07
    out_len = len(cipherdata) - pad_len - _TEA_SALT_LEN - _TEA_ZERO_LEN - 1
    if out_len < 0:
        raise QmcError("密钥密文填充异常")
    out = bytearray(out_len)
    iv_prev = bytearray(8)
    iv_cur = bytearray(cipherdata[:8])
    pos = 8
    dest_idx = 1 + pad_len

    def crypt_block():
        nonlocal pos, dest
        iv_prev[:] = iv_cur[:]
        iv_cur[:] = cipherdata[pos:pos + 8]
        x = bytes(a ^ b for a, b in zip(dest[:8], iv_cur[:8]))
        dest = bytearray(_tea_decrypt_block(x, tea_key, rounds))
        pos += 8

    i = 1
    while i <= _TEA_SALT_LEN:
        if dest_idx < 8:
            dest_idx += 1
            i += 1
        elif dest_idx == 8:
            crypt_block()
            dest_idx = 0

    o = 0
    while o < out_len:
        if dest_idx < 8:
            out[o] = dest[dest_idx] ^ iv_prev[dest_idx]
            dest_idx += 1
            o += 1
        elif dest_idx == 8:
            crypt_block()
            dest_idx = 0

    for _ in range(1, _TEA_ZERO_LEN):
        if dest_idx < 8:
            if dest[dest_idx] ^ iv_prev[dest_idx] != 0:
                raise QmcError("密钥校验失败(可能是未知的新格式或文件损坏)")
            dest_idx += 1
        elif dest_idx == 8:
            crypt_block()
            dest_idx = 0

    return bytes(out)


def _decrypt_key_blob_v1(blob: bytes) -> bytes:
    """V1 密钥保护: [8 字节配方][TEA-CBC 密文] → 主密钥。"""
    if len(blob) < 16 or len(blob) % 8:
        raise QmcError("密钥密文长度非法")
    recipe = blob[:8]
    tea_key = bytearray(16)
    for i in range(8):
        tea_key[2 * i] = _CORE_KEY[i]
        tea_key[2 * i + 1] = recipe[i]
    return recipe + _tars_tea_cbc_decrypt(blob[8:], bytes(tea_key))


def _decrypt_key_blob(blob: bytes) -> bytes:
    """完整密钥解密链: V2(双重混淆)如有,再走 V1。"""
    if blob.startswith(_V2_MAGIC):
        payload = blob[len(_V2_MAGIC):]
        x = _tars_tea_cbc_decrypt(payload, _GARBLE_KEYS[0])
        x = _tars_tea_cbc_decrypt(x, _GARBLE_KEYS[1])
        x = x.strip().rstrip(b"\x00").strip()
        if len(x) % 4:
            x += b"=" * (4 - len(x) % 4)
        try:
            blob = base64.b64decode(x)
        except Exception as e:  # noqa: BLE001 - 统一转为 QmcError
            raise QmcError(f"V2 密钥内层解码失败: {e}") from e
    return _decrypt_key_blob_v1(blob)


# --------------------------------------------------------------------------
# 尾部解析
# --------------------------------------------------------------------------


def _read_tail(f, size: int) -> tuple[bytes, int]:
    """从文件对象读取尾部密钥串,返回 (密钥串字节, 音频数据区长度)。

    支持两种尾部形态:
    - 常见: [Base64 密钥串][4 字节小端长度]
    - 新版 QTag: [QTag 数据(逗号分隔, 首段为密钥串)][4 字节大端长度]["QTag"]
    """
    if size < 4:
        raise QmcError("文件太小,不是有效的 QMC 文件")
    f.seek(size - 4)
    last4 = f.read(4)

    if last4 == b"QTag":
        f.seek(size - 8)
        raw_len = f.read(4)
        if len(raw_len) != 4:
            raise QmcError("QTag 数据不完整")
        tag_len = struct.unpack(">I", raw_len)[0]
        if tag_len <= 0 or tag_len > size - 8:
            raise QmcError("QTag 数据长度非法")
        f.seek(size - 8 - tag_len)
        chunk = f.read(tag_len)
        return chunk.split(b",", 1)[0], size - 8 - tag_len

    if last4 == b"STag":
        raise QmcError(
            "检测到更新的 STag 加密格式: 该格式不内嵌密钥,无法离线解密"
        )

    if last4 == b"cex\x00":
        f.seek(size - 8)
        if f.read(4) == b"musi":
            raise QmcError("检测到 musicex 加密格式: 该格式暂不受支持")

    (key_len,) = struct.unpack("<I", last4)
    if key_len <= 0 or key_len > size - 4 or key_len > _MAX_TAIL_KEY_BYTES:
        raise QmcError("无法解析文件尾部密钥(可能是未知的新加密格式)")
    f.seek(size - 4 - key_len)
    return f.read(key_len), size - 4 - key_len


def _decode_tail_key(key_str: bytes) -> bytes:
    s = key_str.strip().rstrip(b"\x00").strip()
    if not s:
        raise QmcError("尾部密钥为空")
    if len(s) % 4:
        s += b"=" * (4 - len(s) % 4)
    try:
        blob = base64.b64decode(s)
    except Exception as e:  # noqa: BLE001 - 统一转为 QmcError
        raise QmcError(f"密钥 Base64 解码失败: {e}") from e
    try:
        key = _decrypt_key_blob(blob)
    except QmcError as e:  # 补一层上下文，便于日志定位
        raise QmcError(f"密钥解析失败: {e}") from e
    if len(key) not in (128, 256, 512):
        raise QmcError(f"解密后的密钥长度异常: {len(key)}")
    return key


# --------------------------------------------------------------------------
# 数据解密
# --------------------------------------------------------------------------


def _xor_bytes(a: bytes, b: bytes) -> bytes:
    if len(a) != len(b):
        raise ValueError("xor 长度不一致")
    if not a:
        return b""
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(len(a), "little")


def _looks_like_audio(head: bytes) -> bool:
    if len(head) < 2:
        return False
    if head[:4] in (_FLAC_MAGIC, _OGG_MAGIC, _RIFF_MAGIC):
        return True
    if head[:3] == _ID3_MAGIC:
        return True
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:  # MPEG 帧同步
        return True
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return True
    return False


_BAD_AUDIO_MSG = "解密结果不是有效的音频: 密钥不匹配或该文件使用了更新的加密格式"


def _resolve_ext(ext: str) -> str:
    ext = (ext or "").lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return ext


def decrypt_qmc_bytes(
    data: bytes,
    ext: str,
    on_progress: Optional[Callable[[float], None]] = None,
) -> bytes:
    """在内存中解密一个完整的 QMC 文件(按扩展名判定格式族)。"""
    ext = _resolve_ext(ext)
    if ext in QMC_V1_EXTS:
        ks = _v1_keystream
        data_len = len(data)
    elif ext in QMC_V2_EXTS:
        bio = io.BytesIO(data)
        key_str, data_len = _read_tail(bio, len(data))
        ks = _make_keystream(_decode_tail_key(key_str))
    else:
        raise QmcError(f"不支持的文件类型: {ext or '?'}")
    if data_len <= 0:
        raise QmcError("文件不含音频数据")

    out = bytearray()
    processed = 0
    first = True
    while processed < data_len:
        n = min(CHUNK_SIZE, data_len - processed)
        piece = _xor_bytes(data[processed:processed + n], ks(processed, n))
        if first:
            if not _looks_like_audio(piece[:16]):
                raise QmcError(_BAD_AUDIO_MSG)
            first = False
        out += piece
        processed += n
        if on_progress:
            on_progress(processed / data_len * 100.0)
    return bytes(out)


def decrypt_file(
    src: str,
    dst: str,
    on_progress: Optional[Callable[[float], None]] = None,
) -> tuple[str, QmcMeta]:
    """流式解密单个 QMC 文件,返回 (输出路径, 元数据)。

    输出为原始音频格式(不作转码),扩展名由加密格式决定。
    """
    if not os.path.exists(src):
        raise QmcError(f"文件不存在: {src}")
    ext = os.path.splitext(src)[1].lower()
    if ext not in QMC_EXTS:
        raise QmcError(f"不支持的文件类型: {ext or src}")

    dst = os.path.abspath(os.fspath(dst))
    dst_dir = os.path.dirname(dst) or "."
    os.makedirs(dst_dir, exist_ok=True)
    fd, temp_dst = tempfile.mkstemp(
        prefix=f".{os.path.basename(dst)}.",
        suffix=os.path.splitext(dst)[1] or ".tmp",
        dir=dst_dir,
    )
    os.close(fd)
    try:
        os.remove(temp_dst)
    except OSError:
        pass

    try:
        with open(src, "rb") as fin:
            size = os.fstat(fin.fileno()).st_size
            if ext in QMC_V1_EXTS:
                ks = _v1_keystream
                data_len = size
            else:
                key_str, data_len = _read_tail(fin, size)
                ks = _make_keystream(_decode_tail_key(key_str))
            if data_len <= 0:
                raise QmcError("文件不含音频数据")

            fin.seek(0)
            with open(temp_dst, "wb") as fout:
                processed = 0
                first = True
                while processed < data_len:
                    n = min(CHUNK_SIZE, data_len - processed)
                    chunk = fin.read(n)
                    if len(chunk) != n:
                        raise QmcError("文件读取不完整")
                    out = _xor_bytes(chunk, ks(processed, n))
                    if first:
                        if not _looks_like_audio(out[:16]):
                            raise QmcError(_BAD_AUDIO_MSG)
                        first = False
                    fout.write(out)
                    processed += n
                    if on_progress:
                        on_progress(processed / data_len * 100.0)
                    # 让出 GIL,保持界面响应(对齐 NCM 解密的做法)
                    time.sleep(0)
        os.replace(temp_dst, dst)
    finally:
        if os.path.exists(temp_dst):
            os.remove(temp_dst)

    if on_progress:
        on_progress(100.0)
    return dst, _read_tags_quietly(dst)


def _read_tags_quietly(path: str) -> QmcMeta:
    """解密后尽力读取内嵌标签(失败不影响结果)。"""
    meta = QmcMeta()
    try:
        import mutagen

        mf = mutagen.File(path, easy=True)
        if mf is not None:
            tags = mf.tags or {}

            def first(value) -> str:
                if not value:
                    return ""
                if isinstance(value, (list, tuple)):
                    return str(value[0]) if value else ""
                return str(value)

            meta.music_name = first(tags.get("title"))
            meta.artist = first(tags.get("artist"))
            meta.album = first(tags.get("album"))
    except Exception:  # noqa: BLE001 - 标签读取失败不影响解密结果
        pass
    return meta


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------


def is_qmc_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in QMC_EXTS


def make_output_path(
    src: str, out_dir: str, reserved_paths: Optional[set[str]] = None
) -> str:
    """构造输出路径: 同名 + 对应音频扩展名,避免覆盖或同批次重名。"""
    ext = QMC_EXTS.get(os.path.splitext(src)[1].lower(), ".bin")
    base = os.path.splitext(os.path.basename(src))[0]
    dst = os.path.join(out_dir, base + ext)
    reserved_paths = reserved_paths if reserved_paths is not None else set()
    i = 1
    while os.path.exists(dst) or os.path.normcase(os.path.abspath(dst)) in reserved_paths:
        dst = os.path.join(out_dir, f"{base}_{i}{ext}")
        i += 1
    reserved_paths.add(os.path.normcase(os.path.abspath(dst)))
    return dst
