# -*- coding: utf-8 -*-
"""本地转码引擎:基于 ffmpeg 将本地音频文件转码为 FLAC / WAV(无损容器)。

说明:FLAC/WAV 均为无损格式。从无损源(flac/wav/m4a-alac)转换不损失质量;
从有损源(mp3/aac/opus)转换只是在无损容器中保存有损音质,无法"提升"音质。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

SUPPORTED_INPUT_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif", ".alac", ".ape", ".mp4", ".webm"}


@dataclass
class ConvertTask:
    src: str
    dst: str
    fmt: str  # "flac" | "wav"
    status: str = "排队中"
    progress: float = 0.0
    error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_progress(self, p: float):
        with self._lock:
            self.progress = p


def _app_dirs() -> list[str]:
    """Return directories where bundled executables may be unpacked/installed."""
    dirs: list[str] = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        dirs.extend([meipass, os.path.join(meipass, "bin")])

    here = os.path.dirname(os.path.abspath(__file__))
    dirs.extend([here, os.path.join(here, "bin")])

    executable = getattr(sys, "executable", "")
    if executable:
        exe_dir = os.path.dirname(os.path.abspath(executable))
        dirs.extend([exe_dir, os.path.join(exe_dir, "bin")])

    # Keep ordering stable while avoiding duplicate work when running from a
    # source checkout or a one-file PyInstaller extraction directory.
    return list(dict.fromkeys(os.path.normpath(d) for d in dirs if d))


def _existing_executable(path: str) -> Optional[str]:
    if os.path.isfile(path):
        return os.path.abspath(path)
    return None


def _find_bundled_binary(name: str) -> Optional[str]:
    names = [name]
    if os.name == "nt" and not name.lower().endswith(".exe"):
        names.insert(0, f"{name}.exe")
    for directory in _app_dirs():
        for candidate_name in names:
            found = _existing_executable(os.path.join(directory, candidate_name))
            if found:
                return found
    return None


def find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg on PATH, in packaged resources, or common Windows paths."""
    exe = shutil.which("ffmpeg") or _find_bundled_binary("ffmpeg")
    if exe:
        return exe

    # winget package directories are not necessarily on PATH until a new
    # shell is opened, so search them explicitly as a fallback.
    local_app_data = os.environ.get("LOCALAPPDATA")
    winget_root = (
        os.path.join(local_app_data, "Microsoft", "WinGet", "Packages")
        if local_app_data
        else ""
    )
    if winget_root and os.path.isdir(winget_root):
        for root, _dirs, files in os.walk(winget_root):
            for name in files:
                if name.lower() == "ffmpeg.exe":
                    return os.path.abspath(os.path.join(root, name))

    for cand in (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        os.path.join(local_app_data, "ffmpeg", "bin", "ffmpeg.exe")
        if local_app_data
        else "",
    ):
        found = _existing_executable(cand) if cand else None
        if found:
            return found
    return None


def find_ffprobe(ffmpeg: Optional[str] = None) -> Optional[str]:
    """Locate ffprobe, preferring the executable beside the selected ffmpeg."""
    if ffmpeg:
        ffmpeg_path = _resolve_executable(ffmpeg)
        if ffmpeg_path:
            sibling = os.path.join(
                os.path.dirname(ffmpeg_path),
                "ffprobe.exe" if os.name == "nt" else "ffprobe",
            )
            found = _existing_executable(sibling)
            if found:
                return found

    exe = shutil.which("ffprobe") or _find_bundled_binary("ffprobe")
    return os.path.abspath(exe) if exe else None


def _resolve_executable(executable: Optional[str]) -> Optional[str]:
    """Resolve a supplied command/path without rejecting valid PATH commands."""
    if not executable:
        return None
    found = shutil.which(executable)
    if found:
        return os.path.abspath(found)
    return _existing_executable(executable)


def is_supported(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_INPUT_EXT


def build_output_path(
    src: str,
    out_dir: str,
    fmt: str,
    reserved_paths: Optional[set[str]] = None,
) -> str:
    """构造输出路径,避免覆盖源文件或同一批次的其他输出。"""
    base = os.path.splitext(os.path.basename(src))[0]
    dst = os.path.join(out_dir, f"{base}.{fmt}")
    i = 1
    reserved_paths = reserved_paths if reserved_paths is not None else set()
    src_key = os.path.normcase(os.path.abspath(src))
    while (
        os.path.normcase(os.path.abspath(dst)) == src_key
        or os.path.exists(dst)
        or os.path.normcase(os.path.abspath(dst)) in reserved_paths
    ):
        dst = os.path.join(out_dir, f"{base}_{i}.{fmt}")
        i += 1
    reserved_paths.add(os.path.normcase(os.path.abspath(dst)))
    return dst


class ConvertError(Exception):
    pass


def convert_file(
    src: str,
    dst: str,
    fmt: str,
    on_progress: Optional[Callable[[float], None]] = None,
    on_line: Optional[Callable[[str], None]] = None,
    ffmpeg: Optional[str] = None,
) -> str:
    """转码单个文件,返回输出路径。"""
    src = os.path.abspath(os.fspath(src))
    dst = os.path.abspath(os.fspath(dst))
    if not os.path.isfile(src):
        raise ConvertError(f"文件不存在: {src}")
    if fmt not in ("flac", "wav"):
        raise ConvertError(f"不支持的输出格式: {fmt}")
    if os.path.normcase(src) == os.path.normcase(dst):
        raise ConvertError("输出路径不能与源文件相同,请改用其他文件名或目录")

    ffmpeg_exe = _resolve_executable(ffmpeg) if ffmpeg else find_ffmpeg()
    if not ffmpeg_exe:
        raise ConvertError("未找到 ffmpeg,请先安装并加入 PATH")

    dst_dir = os.path.dirname(dst) or "."
    os.makedirs(dst_dir, exist_ok=True)

    if fmt == "flac":
        codec_args = ["-c:a", "flac", "-compression_level", "8"]
    else:  # wav - 跟随源位深,避免 24/32-bit 源被降为 16-bit
        codec_args = _wav_codec_args(src, ffmpeg_exe)

    # Always render to a sibling temporary file. This prevents a failed ffmpeg
    # invocation from truncating an existing destination and lets us replace it
    # atomically only after a complete output has been produced.
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(dst)}.",
        suffix=f".{fmt}",
        dir=dst_dir,
    )
    os.close(fd)
    try:
        os.remove(temp_path)
    except OSError:
        pass

    cmd = [
        ffmpeg_exe,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        src,
        "-map",
        "0:a:0",
        "-map_metadata",
        "0",
        "-vn",
        *codec_args,
        temp_path,
    ]

    tail: deque[str] = deque(maxlen=20)
    # Probe before starting ffmpeg so its merged stderr/stdout pipe is drained
    # immediately after launch and cannot fill while a probe is running.
    duration = _probe_duration(src, ffmpeg_exe)
    try:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            raise ConvertError(f"无法启动 ffmpeg: {e}") from e

        assert proc.stdout is not None
        import re

        time_re = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

        try:
            for raw in proc.stdout:
                line = raw.rstrip()
                if line:
                    tail.append(line[-500:])
                if on_line:
                    on_line(line)
                m = time_re.search(line)
                if m and duration:
                    secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    pct = min(99.0, secs / duration * 100.0)
                    if on_progress:
                        on_progress(pct)
        finally:
            proc.stdout.close()

        rc = proc.wait()
        if rc != 0:
            detail = " | ".join(tail)
            suffix = f": {detail}" if detail else ""
            raise ConvertError(f"ffmpeg 退出码 {rc}{suffix}")
        if not os.path.isfile(temp_path) or os.path.getsize(temp_path) <= 0:
            raise ConvertError("转码后未生成有效输出文件")

        try:
            os.replace(temp_path, dst)
        except OSError as e:
            raise ConvertError(f"无法写入输出文件 {dst}: {e}") from e
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    if on_progress:
        on_progress(100.0)
    return dst


def _probe_duration(path: str, ffmpeg: str) -> float:
    """用 ffprobe(或 ffmpeg)获取时长,失败返回 0。"""
    import json
    import re

    ffprobe = find_ffprobe(ffmpeg)
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", path],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            return float(json.loads(out)["format"]["duration"])
        except Exception:  # noqa: BLE001
            pass
    # 回退:解析 ffmpeg stderr 里的 Duration
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-i", path],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out.stderr or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _wav_codec_args(path: str, ffmpeg: str) -> list[str]:
    """根据源音频位深选择 WAV PCM 编码器,避免位深降级。

    16-bit -> pcm_s16le, 24-bit -> pcm_s24le, 32-bit int -> pcm_s32le,
    浮点 -> pcm_f32le, 探测失败回退 pcm_s16le。
    """
    import json

    sample_fmt = ""
    bits = 16
    ffprobe = find_ffprobe(ffmpeg)
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", path],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            streams = json.loads(out.stdout).get("streams", [])
            for s in streams:
                if s.get("codec_type") == "audio":
                    sample_fmt = s.get("sample_fmt", "")
                    bits = int(s.get("bits_per_raw_sample") or s.get("bits_per_sample") or 16)
                    break
        except Exception:  # noqa: BLE001
            pass
    if sample_fmt in ("flt", "fltp", "dbl", "dblp"):
        return ["-c:a", "pcm_f32le"]
    if bits >= 32:
        return ["-c:a", "pcm_s32le"]
    if bits >= 24:
        return ["-c:a", "pcm_s24le"]
    return ["-c:a", "pcm_s16le"]
