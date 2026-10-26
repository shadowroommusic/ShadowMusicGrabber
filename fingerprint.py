# -*- coding: utf-8 -*-
"""音频指纹识别:为缺失标签的解密结果补全歌名/歌手/专辑/封面。

链路: 本地 fpcalc(Chromaprint) 生成声学指纹 → AcoustID 接口反查 MusicBrainz
录音 ID → MusicBrainz 接口取标题/艺人/专辑 → Cover Art Archive 取封面。

两个硬前提(缺任意一个都会优雅跳过,不影响解密主流程):
- **fpcalc 可执行文件**: Chromaprint 官方 Windows 包内的工具, pip 装不了。
  放到 ``bin\\fpcalc.exe`` 或加入 PATH 即可;也可设环境变量 ``FPCALC`` 指定路径。
- **AcoustID API Key**: 免费, 用 MusicBrainz 账号登录 https://acoustid.org/
  获取。本模块不内置任何 key, 必须由使用者自行提供。

注意: AcoustID 的库挂在 MusicBrainz 上, 由社区维护, 对 QQ 音乐等平台独占的
中文曲库覆盖很有限, 查不到是常态而非故障。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
MUSICBRAINZ_RECORDING_URL = "https://musicbrainz.org/ws/2/recording/{mbid}"
COVER_ART_ARCHIVE_URL = "https://coverartarchive.org/release/{release_id}/front"

USER_AGENT = "ShadowMusicGrabber/1.0 ( https://github.com/shadowroommusic/ShadowMusicGrabber )"
REQUEST_TIMEOUT = 20
FPCALC_TIMEOUT = 180
# MusicBrainz 官方要求至多 1 请求/秒;批量识别缺标签文件时不加节流会吃 503。
MUSICBRAINZ_MIN_INTERVAL = 1.0

_MB_LOCK = threading.Lock()
_MB_LAST_REQUEST = 0.0


def _throttle_musicbrainz() -> None:
    """串行化对 MusicBrainz 的请求并保证间隔 ≥1 秒(可注入时钟,便于测试)。"""
    global _MB_LAST_REQUEST
    with _MB_LOCK:
        now = time.monotonic()
        wait = _MB_LAST_REQUEST + MUSICBRAINZ_MIN_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _MB_LAST_REQUEST = time.monotonic()


class FingerprintError(Exception):
    """指纹识别过程中的可预期错误(缺 fpcalc / 缺 key / 网络失败等)。"""


class MissingBackend(FingerprintError):
    """找不到 fpcalc 可执行文件。"""


class MissingApiKey(FingerprintError):
    """未提供 AcoustID API Key。"""


@dataclass
class FingerprintMatch:
    """一次识别命中(或未命中)的结果。"""

    title: str = ""
    artist: str = ""
    album: str = ""
    cover_url: str = ""
    mbid: str = ""
    score: float = 0.0

    @property
    def found(self) -> bool:
        return bool(self.title or self.artist)


def find_fpcalc() -> Optional[str]:
    """定位 fpcalc: 环境变量 → PATH → 程序目录/bin → cwd/bin。"""
    env = os.environ.get("FPCALC", "").strip()
    if env and os.path.isfile(env):
        return os.path.abspath(env)

    name = "fpcalc.exe" if os.name == "nt" else "fpcalc"
    found = shutil.which("fpcalc")
    if found:
        return os.path.abspath(found)

    here = os.path.dirname(os.path.abspath(__file__))
    meipass = getattr(sys, "_MEIPASS", "")
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if sys.executable else ""
    candidates = [here, meipass, exe_dir, os.getcwd()]
    for base in candidates:
        if not base:
            continue
        for sub in ("", "bin"):
            path = os.path.join(base, sub, name) if sub else os.path.join(base, name)
            if os.path.isfile(path):
                return os.path.abspath(path)
    return None


def compute_fingerprint(path: str, fpcalc: Optional[str] = None) -> tuple[int, str]:
    """调用 fpcalc 生成 (时长秒, Base64 指纹)。"""
    exe = fpcalc or find_fpcalc()
    if not exe:
        raise MissingBackend(
            "未找到 fpcalc。请从 https://github.com/acoustid/chromaprint/releases "
            "下载 chromaprint-fpcalc-*-windows-x86_64.zip,把 fpcalc.exe 放到程序的 "
            "bin 目录或加入 PATH。"
        )
    try:
        proc = subprocess.run(
            # -length 120: AcoustID 官方建议只取前两分钟,长文件(播客/串烧)
            # 不再把整段都算一遍,识别速度大幅提升且命中率不受影响。
            [exe, "-json", "-length", "120", os.path.abspath(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FPCALC_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        raise MissingBackend(f"无法启动 fpcalc: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise FingerprintError("fpcalc 计算指纹超时") from e

    if proc.returncode != 0:
        detail = (proc.stdout or proc.stderr or "").strip()[-300:]
        raise FingerprintError(f"fpcalc 失败(退出码 {proc.returncode}): {detail}")

    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError) as e:
        raise FingerprintError(f"无法解析 fpcalc 输出: {e}") from e

    fingerprint = data.get("fingerprint")
    duration = data.get("duration")
    if not fingerprint or duration is None:
        raise FingerprintError("fpcalc 未返回有效指纹(文件过短或不受支持)")
    return int(float(duration)), str(fingerprint)


def _http_get_json(url: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise FingerprintError(f"接口返回 HTTP {e.code}: {url}") from e
    except urllib.error.URLError as e:
        raise FingerprintError(f"网络不可用: {e.reason}") from e
    except (ValueError, OSError) as e:
        raise FingerprintError(f"请求失败: {e}") from e


def lookup_acoustid(
    fingerprint: str,
    duration: int,
    api_key: str,
    min_score: float = 0.5,
) -> list[dict]:
    """用指纹查 AcoustID,返回候选(已按分数降序)。"""
    if not api_key:
        raise MissingApiKey(
            "未配置 AcoustID API Key。请到 https://acoustid.org/ 用 MusicBrainz "
            "账号登录后获取,并在程序里填入。"
        )
    data = _http_get_json(
        ACOUSTID_LOOKUP_URL,
        {
            "client": api_key,
            "fp": fingerprint,
            "duration": duration,
            "format": "json",
            "meta": "recordings",
        },
    )
    if data.get("status") != "ok":
        message = data.get("error", {}).get("message") or "未知错误"
        raise FingerprintError(f"AcoustID 查询失败: {message}")
    results = []
    for item in data.get("results") or []:
        score = float(item.get("score", 0.0))
        if score < min_score:
            continue
        for rec in item.get("recordings") or []:
            results.append({"score": score, **rec})
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def fetch_recording(mbid: str) -> dict:
    """查 MusicBrainz 录音详情,返回 title / artist / album / release_id。

    带全局节流:MusicBrainz 要求 ≤1 请求/秒,批量识别时背靠背请求会被 503。
    """
    _throttle_musicbrainz()
    data = _http_get_json(
        MUSICBRAINZ_RECORDING_URL.format(mbid=mbid),
        {"fmt": "json", "inc": "artists+releases"},
    )
    title = data.get("title", "") or ""
    artist = ""
    credit_list = data.get("artist-credit") or []
    if credit_list:
        parts = []
        for credit in credit_list:
            name = (credit.get("artist") or {}).get("name", "")
            if name:
                joiner = credit.get("joinphrase", "") or ""
                parts.append(f"{name}{joiner}")
        artist = "".join(parts)
    album = ""
    release_id = ""
    for release in data.get("releases") or []:
        album = release.get("title", "") or album
        release_id = release.get("id", "") or release_id
        if album:
            break
    return {
        "title": title,
        "artist": artist,
        "album": album,
        "release_id": release_id,
    }


def identify(
    path: str,
    api_key: str,
    fpcalc: Optional[str] = None,
    min_score: float = 0.5,
    on_line: Optional[Callable[[str], None]] = None,
) -> FingerprintMatch:
    """对单个音频文件做完整识别;查不到时返回 found=False 的结果(不抛异常)。"""
    duration, fingerprint = compute_fingerprint(path, fpcalc=fpcalc)
    if on_line:
        on_line(f"指纹已生成({duration}s),查询 AcoustID…")
    candidates = lookup_acoustid(fingerprint, duration, api_key, min_score=min_score)
    if not candidates:
        return FingerprintMatch()

    best = candidates[0]
    mbid = str(best.get("id", ""))
    title = str(best.get("title", "") or "")
    artist = ""
    score = float(best.get("score", 0.0))

    if mbid:
        try:
            info = fetch_recording(mbid)
            title = info["title"] or title
            artist = info["artist"]
        except FingerprintError:
            if on_line:
                on_line("MusicBrainz 详情查询失败,使用 AcoustID 返回的信息")

    album = ""
    cover_url = ""
    for release in best.get("releases") or []:
        album = release.get("title", "") or album
        release_id = release.get("id", "")
        if release_id:
            candidate = COVER_ART_ARCHIVE_URL.format(release_id=release_id)
            if on_line:
                on_line(f"封面候选: {candidate}")
            cover_url = candidate
        if album:
            break

    return FingerprintMatch(
        title=title,
        artist=artist,
        album=album,
        cover_url=cover_url,
        mbid=mbid,
        score=score,
    )


def download_cover(url: str) -> bytes:
    """下载封面字节;失败返回空。"""
    if not url:
        return b""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            data = resp.read()
        return data if data else b""
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return b""
