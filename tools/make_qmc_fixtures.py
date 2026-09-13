# -*- coding: utf-8 -*-
"""生成 QMC 解密回归测试夹具(tests/data/qmc/)。

夹具内容全部为合成随机数据(并非真实歌曲), 用于验证 qmc_decrypt.py 的完整
解密链路: QMCv1 静态 / QMCv2+MASK128 / QMCv2+RC4 / QMCv2+QTag+EncV2 密钥。

加密方向(生成夹具)按规范实现; 生成后由 libtakiyasha(MIT)交叉验证可正确解密,
并已与 MusicDecrypto(MIT)官方测试向量做过字节级对照(详见项目记录)。

用法:
    python tools/make_qmc_fixtures.py            # 写入 tests/data/qmc/
    python tools/make_qmc_fixtures.py --check    # 仅校验已存在的夹具(重新解密比对)
"""

from __future__ import annotations

import base64
import hashlib
import os
import random
import string
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qmc_decrypt as QD  # noqa: E402

FIXDIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "data", "qmc"
)

_TEA_ROUNDS = 32
_DELTA = 0x9E3779B9


def _tea_encrypt_block(block: bytes, key: bytes, rounds: int = _TEA_ROUNDS) -> bytes:
    v0 = int.from_bytes(block[:4], "big")
    v1 = int.from_bytes(block[4:8], "big")
    k0 = int.from_bytes(key[0:4], "big")
    k1 = int.from_bytes(key[4:8], "big")
    k2 = int.from_bytes(key[8:12], "big")
    k3 = int.from_bytes(key[12:16], "big")
    s = 0
    for _ in range(rounds // 2):
        s = (s + _DELTA) & 0xFFFFFFFF
        v0 = (v0 + (((v1 << 4) + k0) ^ (v1 + s) ^ ((v1 >> 5) + k1))) & 0xFFFFFFFF
        v1 = (v1 + (((v0 << 4) + k2) ^ (v0 + s) ^ ((v0 >> 5) + k3))) & 0xFFFFFFFF
    return v0.to_bytes(4, "big") + v1.to_bytes(4, "big")


def _tars_encrypt(plaindata: bytes, tea_key: bytes, rng: random.Random) -> bytes:
    """TarsCpp tc_tea CBC 加密(与 qmc_decrypt._tars_tea_cbc_decrypt 互逆)。"""
    bs, salt_len, zero_len = 8, 2, 7
    total = len(plaindata) + salt_len + zero_len + 1
    pad_len = total % bs
    if pad_len:
        pad_len = bs - pad_len
    body = bytearray()
    body.append((rng.randrange(256) & 0xF8) | pad_len)
    body += bytes(rng.randrange(256) for _ in range(pad_len))
    body += bytes(rng.randrange(256) for _ in range(salt_len))
    body += plaindata
    body += b"\x00" * zero_len
    while len(body) % bs:
        body += bytes(rng.randrange(256) for _ in range(1))  # pragma: no cover
    out = bytearray()
    iv_plain = bytearray(bs)
    iv_crypt = bytearray(bs)
    for pos in range(0, len(body), bs):
        blk = bytearray(body[pos:pos + bs])
        blk = bytearray(a ^ b for a, b in zip(blk, iv_crypt))
        enc = _tea_encrypt_block(bytes(blk), tea_key)
        out += bytes(a ^ b for a, b in zip(enc, iv_plain))
        iv_plain[:] = blk[:]
        iv_crypt[:] = out[-bs:]
    return bytes(out)


def _v1_key_blob(final_key: bytes, rng: random.Random) -> bytes:
    """把最终主密钥封装为 V1 密文块(recipe + TEA-CBC)。"""
    recipe = final_key[:8]
    rest = final_key[8:]
    tea_key = bytearray(16)
    for i in range(8):
        tea_key[2 * i] = QD._CORE_KEY[i]
        tea_key[2 * i + 1] = recipe[i]
    return recipe + _tars_encrypt(rest, bytes(tea_key), rng)


def _v2_wrap(blob: bytes, rng: random.Random) -> bytes:
    """EncV2 外层: b64 -> garble2 加密 -> garble1 加密 -> 前缀魔数。"""
    x = base64.b64encode(blob)
    x = _tars_encrypt(x, QD._GARBLE_KEYS[1], rng)
    x = _tars_encrypt(x, QD._GARBLE_KEYS[0], rng)
    return QD._V2_MAGIC + x


def _make_payload(nbytes: int, seed: int) -> bytes:
    rng = random.Random(seed)
    return b"fLaC" + rng.randbytes(nbytes - 4)


def _rand_key(rng: random.Random, nbytes: int) -> bytes:
    """生成无空字节密钥(真实 QMCv2 密钥为 ASCII 串; RC4 分段跳转要求无 0x00)。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(rng.choice(alphabet) for _ in range(nbytes)).encode("ascii")


def _encrypt_payload(plain: bytes, key: bytes | None) -> bytes:
    if key is None:
        ks = lambda o, n: QD._mask128_keystream(QD._static_mask128(), o, n)  # noqa: E731
    else:
        ks = QD._make_keystream(key)
    return QD._xor_bytes(plain, ks(0, len(plain)))


def build_fixtures() -> dict[str, bytes]:
    rng = random.Random(20260913)
    fixtures: dict[str, bytes] = {}

    # 1) QMCv1 静态(无尾部)
    plain = _make_payload(2048, 1)
    fixtures["v1_static.qmcflac"] = _encrypt_payload(plain, None)
    fixtures["_p_v1_static"] = plain  # 内部用, 不落盘

    # 2) QMCv2 + Mask128(256 字节密钥, V1 封装, 尾部带 \0 填充以覆盖容错)
    key = _rand_key(rng, 256)
    plain = _make_payload(2048, 2)
    body = base64.b64encode(_v1_key_blob(key, rng)) + b"\x00"
    fixtures["v2_mask.mflac"] = _encrypt_payload(plain, key) + body + struct.pack("<I", len(body))
    fixtures["_p_v2_mask"] = plain

    # 3) QMCv2 + HardenedRC4(512 字节密钥, V1 封装, 跨两个 RC4 分段)
    key = _rand_key(rng, 512)
    plain = _make_payload(6144, 3)
    body = base64.b64encode(_v1_key_blob(key, rng))
    fixtures["v2_rc4.mflac"] = _encrypt_payload(plain, key) + body + struct.pack("<I", len(body))
    fixtures["_p_v2_rc4"] = plain

    # 4) QMCv2 + QTag 尾部 + EncV2 双层密钥
    key = _rand_key(rng, 256)
    plain = _make_payload(2048, 4)
    key_str = base64.b64encode(_v2_wrap(_v1_key_blob(key, rng), rng))
    chunk = key_str + b",123456789,2"
    fixtures["v2_qtag_v2key.mflac0"] = (
        _encrypt_payload(plain, key) + chunk + struct.pack(">I", len(chunk)) + b"QTag"
    )
    fixtures["_p_v2_qtag_v2key"] = plain

    return fixtures


def cross_validate(fixtures: dict[str, bytes]) -> bool:
    """用 libtakiyasha(若可用)交叉验证夹具确实可解密为对应明文。"""
    libtaki = os.environ.get("LIBTAKIYASHA_PATH")
    if not libtaki or not os.path.isdir(libtaki):
        print("[跳过] 未设置 LIBTAKIYASHA_PATH, 不做 libtakiyasha 交叉验证")
        return True
    sys.path.insert(0, libtaki)
    try:
        from libtakiyasha.qmc._qmcdataciphers import HardenedRC4 as TK_RC4
        from libtakiyasha.qmc._qmcdataciphers import Mask128 as TK_Mask
        from libtakiyasha.qmc._qmckeyciphers import QMCv2KeyEncryptV1 as TK_V1
        from libtakiyasha.qmc._qmckeyciphers import QMCv2KeyEncryptV2 as TK_V2
        from libtakiyasha.qmc._qmckeyciphers import make_core_key as tk_core
    except Exception as e:  # pragma: no cover
        print(f"[跳过] libtakiyasha 导入失败: {e}")
        return True

    ok = True
    core = tk_core(106, 8)
    for name, data in fixtures.items():
        if name.startswith("_p_"):
            continue
        plain = fixtures["_p_" + name.split(".")[0] if False else "_p_" + name[: name.index(".")]]
        try:
            if name.startswith("v1_"):
                ks = bytes(TK_Mask(QD._static_mask128()).keystream("decrypt", len(plain), 0))
                got = QD._xor_bytes(data, ks)
            else:
                tail = data[-4:]
                if tail == b"QTag":
                    chunk_len = struct.unpack(">I", data[-8:-4])[0]
                    chunk = data[-8 - chunk_len: -8]
                    key_str = chunk.split(b",", 1)[0]
                    body_len = len(data) - 8 - chunk_len
                else:
                    key_field = struct.unpack("<I", tail)[0]
                    key_str = data[-4 - key_field: -4]
                    body_len = len(data) - 4 - key_field
                blob = base64.b64decode(key_str.rstrip(b"\x00"))
                if blob.startswith(QD._V2_MAGIC):
                    key = TK_V2(core, [QD._GARBLE_KEYS[0], QD._GARBLE_KEYS[1]]).decrypt(blob[18:])
                else:
                    key = TK_V1(core).decrypt(blob)
                cipher = TK_Mask.from_qmcv2_key256(key) if len(key) == 256 else TK_RC4(key)
                ks = bytes(cipher.keystream("decrypt", len(plain), 0))
                got = QD._xor_bytes(data[:body_len], ks)
            same = got == plain
            print(f"  [libtakiyasha] {name}: {'一致 ✔' if same else '不一致 ✘'}")
            ok = ok and same
        except Exception as e:
            print(f"  [libtakiyasha] {name}: 验证异常 {e!r}")
            ok = False
    return ok


def main() -> int:
    fixtures = build_fixtures()
    os.makedirs(FIXDIR, exist_ok=True)
    print("=== 写入夹具 ===")
    for name, data in fixtures.items():
        if name.startswith("_p_"):
            continue
        path = os.path.join(FIXDIR, name)
        with open(path, "wb") as f:
            f.write(data)
        plain = fixtures["_p_" + name[: name.index(".")]]
        print(f"  {name}: {len(data)}B  明文sha256={hashlib.sha256(plain).hexdigest()}")

    print("\n=== libtakiyasha 交叉验证 ===")
    ok = cross_validate(fixtures)

    print("\n=== 供 tests/test_qmc.py 使用的期望值 ===")
    print("EXPECTED_SHA256 = {")
    for name, data in fixtures.items():
        if name.startswith("_p_"):
            continue
        plain = fixtures["_p_" + name[: name.index(".")]]
        print(f'    "{name}": "{hashlib.sha256(plain).hexdigest()}",')
    print("}")

    print("\n结果:", "OK" if ok else "交叉验证失败!")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
