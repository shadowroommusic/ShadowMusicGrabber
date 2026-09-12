# -*- coding: utf-8 -*-
"""下载引擎:基于 yt-dlp 抓取 SoundCloud / YouTube / Apple Music / 网易云 / QQ音乐等链接。

不包含任何 DRM 解密或加密壳破解逻辑,仅使用 yt-dlp 官方公开的提取器能力。
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional
from urllib.parse import urlsplit

import yt_dlp


class FormatKind(str, Enum):
    """输出格式选择。flac/wav 为无损容器。"""

    ORIGINAL = "original"   # 保持源格式(可能为 m4a/opus 等)
    FLAC = "flac"
    WAV = "wav"
    MP3 = "mp3"


@dataclass
class DownloadTask:
    url: str
    out_dir: str
    fmt: FormatKind = FormatKind.FLAC
    title: str = ""
    status: str = "排队中"
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    file_path: str = ""
    error: str = ""
    allow_playlist: bool = False
    _hook_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update_progress(self, percent: float, speed: str = "", eta: str = ""):
        with self._hook_lock:
            self.progress = percent
            self.speed = speed
            self.eta = eta


PLATFORM_PATTERNS = [
    ("SoundCloud", r"soundcloud\.com"),
    ("YouTube", r"(youtube\.com|youtu\.be)"),
    ("Apple Music", r"music\.apple\.com"),
    ("网易云音乐", r"music\.163\.com"),
    ("QQ音乐", r"(y\.qq\.com|i\.y\.qq\.com|c6\.y\.qq\.com)"),
    ("哔哩哔哩", r"bilibili\.com"),
    ("Spotify", r"open\.spotify\.com"),
    ("Bandcamp", r"bandcamp\.com"),
    ("Vimeo", r"vimeo\.com"),
]


def detect_platform(url: str) -> str:
    """从链接中识别平台,未知返回 '通用'。"""
    for name, pattern in PLATFORM_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return name
    return "通用"


def is_valid_url(url: str) -> bool:
    candidate = url.strip()
    if not candidate or any(ch.isspace() for ch in candidate):
        return False
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return False
    return (
        parts.scheme.lower() in {"http", "https"}
        and bool(parts.netloc)
        and bool(parts.hostname)
    )


def build_ydl_opts(
    task: DownloadTask,
    progress_hook: Callable[[dict], None],
    ffmpeg_location: Optional[str] = None,
) -> dict:
    """构造 yt-dlp 选项。无损策略:优先下载源最高音质,再用 ffmpeg 无损转封装。"""
    postprocessors: list[dict] = []
    if task.fmt == FormatKind.FLAC:
        postprocessors.append(
            {"key": "FFmpegExtractAudio", "preferredcodec": "flac", "preferredquality": "0"}
        )
    elif task.fmt == FormatKind.MP3:
        postprocessors.append(
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}
        )
    # WAV: 不用 yt-dlp 的后处理(它固定转 pcm_s16le 会降位深),
    # 下载原始格式后用 converter 的位深感知转码

    opts: dict = {
        "outtmpl": os.path.join(task.out_dir, "%(title)s.%(ext)s"),
        "format": "bestaudio/best",
        "noplaylist": not task.allow_playlist,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [progress_hook],
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "socket_timeout": 30,
        "windowsfilenames": True,
        "restrictfilenames": False,
        "writethumbnail": False,
        "embedthumbnail": False,
        "continuedl": True,
        "overwrites": False,
        "ignoreerrors": False,
    }
    if postprocessors:
        opts["postprocessors"] = postprocessors
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location
    return opts


class DownloadError(Exception):
    pass


def download_url(
    url: str,
    out_dir: str,
    fmt: FormatKind = FormatKind.FLAC,
    on_status: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float, str, str], None]] = None,
    ffmpeg_location: Optional[str] = None,
    allow_playlist: bool = False,
) -> str:
    """下载单个链接并返回最终文件路径。"""
    if not is_valid_url(url):
        raise DownloadError(f"无效链接: {url!r}")

    out_dir = os.path.abspath(os.fspath(out_dir))
    os.makedirs(out_dir, exist_ok=True)
    task = DownloadTask(url=url, out_dir=out_dir, fmt=fmt, allow_playlist=allow_playlist)
    if not ffmpeg_location and fmt != FormatKind.ORIGINAL:
        ffmpeg_location = _locate_ffmpeg()
    if fmt != FormatKind.ORIGINAL and not ffmpeg_location:
        raise DownloadError("ffmpeg not found; install ffmpeg or place it beside the application")

    started_at = time.time()
    before = _snapshot_output_files(out_dir)

    def hook(d: dict):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total else 0.0
            speed = d.get("_speed_str", "") or ""
            eta = d.get("_eta_str", "") or ""
            task.update_progress(percent, speed, eta)
            if on_progress:
                on_progress(percent, speed, eta)
        elif d.get("status") == "finished":
            task.update_progress(100.0)
            if on_status:
                on_status("下载完成,转码中…")

    opts = build_ydl_opts(task, hook, ffmpeg_location)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            task.title = info.get("title", task.title)
            if on_status:
                on_status("处理完成")
        # 定位最终输出文件
        final = _find_output(task, info, before=before, started_at=started_at)
        if not final:
            raise DownloadError("download finished but no output file was found")
        # WAV: 用位深感知转码(yt-dlp 的 wav 后处理固定 16-bit,会降位深)
        if task.fmt == FormatKind.WAV and final:
            from converter import convert_file

            wav_path = os.path.splitext(final)[0] + ".wav"
            if os.path.abspath(wav_path) != os.path.abspath(final):
                if on_status:
                    on_status("转码为 WAV…")
                convert_file(final, wav_path, "wav", ffmpeg=ffmpeg_location)
                try:
                    if os.path.exists(final):
                        os.remove(final)
                except OSError:
                    pass
                final = wav_path
        return final
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(f"下载失败: {e}") from e
    except Exception as e:  # noqa: BLE001 - 统一包装给 GUI
        raise DownloadError(f"发生错误: {e}") from e


def _snapshot_output_files(out_dir: str) -> set[str]:
    """Return regular files present before a download starts."""
    try:
        return {
            os.path.abspath(os.path.join(out_dir, name))
            for name in os.listdir(out_dir)
            if os.path.isfile(os.path.join(out_dir, name))
        }
    except OSError:
        return set()


def _expected_extensions(task: DownloadTask, info: dict) -> set[str]:
    if task.fmt == FormatKind.FLAC:
        return {".flac"}
    if task.fmt == FormatKind.MP3:
        return {".mp3"}
    if task.fmt == FormatKind.WAV:
        # WAV is rendered by converter after yt-dlp downloads the best source.
        return {".wav", ".m4a", ".mp4", ".webm", ".opus", ".ogg", ".aac", ".mp3", ".flac"}
    ext = str((info or {}).get("ext") or "").lower().strip()
    return {f".{ext}"} if ext else set()


def _find_output(
    task: DownloadTask,
    info: dict,
    before: Optional[set[str]] = None,
    started_at: Optional[float] = None,
) -> str:
    """Resolve the file produced by this invocation, never a stale directory entry."""
    expected = _expected_extensions(task, info)
    if info:
        for key in ("filepath", "_filename"):
            fp = info.get(key)
            if fp and os.path.isfile(fp) and _is_new_output(fp, before, started_at) and (
                not expected or os.path.splitext(fp)[1].lower() in expected
            ):
                return os.path.abspath(fp)
    if info and info.get("requested_downloads"):
        for dl in info["requested_downloads"]:
            fp = dl.get("filepath")
            if fp and os.path.isfile(fp) and _is_new_output(fp, before, started_at) and (
                not expected or os.path.splitext(fp)[1].lower() in expected
            ):
                return os.path.abspath(fp)

    before = before or set()
    candidates: list[str] = []
    try:
        names = os.listdir(task.out_dir)
    except OSError:
        names = []
    for name in names:
        path = os.path.abspath(os.path.join(task.out_dir, name))
        if not os.path.isfile(path) or name.lower().endswith((".part", ".ytdl", ".temp")):
            continue
        if expected and os.path.splitext(name)[1].lower() not in expected:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if path in before and (started_at is None or mtime < started_at - 1.0):
            continue
        candidates.append(path)
    if candidates:
        return max(candidates, key=os.path.getmtime)
    return ""


def _is_new_output(path: str, before: Optional[set[str]], started_at: Optional[float]) -> bool:
    """Reject a pre-existing path when yt-dlp skipped a same-name download."""
    absolute = os.path.abspath(path)
    if absolute not in (before or set()):
        return True
    # yt-dlp is configured with overwrites=False, so an existing destination
    # is normally stale. Allow it only when a tool explicitly replaced it.
    try:
        return started_at is not None and os.path.getmtime(absolute) >= started_at - 1.0
    except OSError:
        return False


def probe_url(url: str, ffmpeg_location: Optional[str] = None) -> dict:
    """只解析链接信息(标题/时长/平台),不下载。"""
    if not is_valid_url(url):
        raise DownloadError(f"无效链接: {url!r}")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", ""),
                "duration": info.get("duration"),
                "uploader": info.get("uploader") or info.get("channel") or "",
                "platform": detect_platform(url),
                "formats": len(info.get("formats") or []),
            }
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(f"解析失败: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise DownloadError(f"解析失败: {e}") from e


def _locate_ffmpeg() -> Optional[str]:
    """复用 converter 的 ffmpeg 定位逻辑(含 winget 路径)。"""
    from converter import find_ffmpeg

    return find_ffmpeg()
