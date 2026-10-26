# -*- coding: utf-8 -*-
"""本地转码引擎:基于 ffmpeg 将本地音频文件转码为 FLAC / WAV(无损容器)。

说明:FLAC/WAV 均为无损格式。从无损源(flac/wav/m4a-alac)转换不损失质量;
从有损源(mp3/aac/opus)转换只是在无损容器中保存有损音质,无法"提升"音质。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional

# 音频 + 视频容器: 视频会被 ffmpeg 提取音轨后转码(视频转音频页面与本地转码页共用)。
SUPPORTED_INPUT_EXT = {
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma",
    ".aiff", ".aif", ".alac", ".ape",
    ".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv", ".m4v", ".wmv",
    ".ts", ".mpg", ".mpeg", ".3gp", ".vob", ".rmvb",
}


@dataclass
class ConvertTask:
    src: str
    dst: str
    fmt: str  # "flac" | "wav" | "mp3"
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


@lru_cache(maxsize=2)
def _scan_winget_for_ffmpeg(winget_root: str) -> Optional[str]:
    """在 WinGet 包目录里找 ffmpeg.exe。

    整棵目录树可能非常大(数百个包 × 每包数层),按根路径缓存,
    避免批量任务里每个文件都重扫一遍。
    """
    for root, _dirs, files in os.walk(winget_root):
        for name in files:
            if name.lower() == "ffmpeg.exe":
                return os.path.abspath(os.path.join(root, name))
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
        found = _scan_winget_for_ffmpeg(winget_root)
        if found:
            return found

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


def unique_path(path: str) -> str:
    """返回不与现有文件冲突的路径：冲突时追加 _1/_2。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{stem}_{i}{ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def is_removable_path(path: str) -> bool:
    """路径是否位于可移动盘 / 网络盘 / 光驱。

    这类盘的随机写入很慢，解密时把中间文件直接写上去会把整机 I/O 打满。
    调用方应改用本地暂存目录处理，最后只把成品复制一次。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if not drive:
            return False
        # DRIVE_REMOVABLE=2, DRIVE_REMOTE=4, DRIVE_CDROM=5
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") in (2, 4, 5)
    except Exception:  # noqa: BLE001 - 探测失败按本地盘处理
        return False


def staging_dir() -> str:
    """本地暂存目录（默认在系统盘）。"""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = os.path.join(base, "ShadowMusicGrabber", "staging")
    os.makedirs(path, exist_ok=True)
    return path


_STAGING_LOCK = threading.Lock()
# 已分配出去但文件还没落盘/还没被消费的暂存名(小写路径键)。
# 只靠 exists() 判重在并行批次下不够:名字分配后到文件真正写出之间,
# 其他线程会拿到同一个名字。
_STAGING_LEASED: set[str] = set()


def staging_path(name: str) -> str:
    """在本地暂存目录里分配一个不冲突的路径(线程安全,供并行批次使用)。

    分配即"租约":即使文件尚未写出,其他线程也不会拿到同名路径;
    消费完毕由 :func:`place_file` 释放租约。
    """
    with _STAGING_LOCK:
        base = os.path.join(staging_dir(), name)
        stem, ext = os.path.splitext(base)
        candidate = base
        index = 1
        while (
            os.path.exists(candidate)
            or os.path.normcase(candidate) in _STAGING_LEASED
        ):
            candidate = f"{stem}_{index}{ext}"
            index += 1
        _STAGING_LEASED.add(os.path.normcase(candidate))
        return candidate


def _release_staging(path: str) -> None:
    with _STAGING_LOCK:
        _STAGING_LEASED.discard(os.path.normcase(path))


_PLACE_LOCK = threading.Lock()


def place_file(src: str, dest_dir: str, overwrite: bool = False) -> str:
    """把处理好的文件放到目标目录，返回最终路径。

    先写 ``.part`` 再原子替换，避免目标盘上留下半截文件；
    overwrite=False 时自动避开同名文件。目标名的分配加了锁,
    并行批次里同名成品不会互相覆盖。
    """
    os.makedirs(dest_dir, exist_ok=True)
    with _PLACE_LOCK:
        target = os.path.join(dest_dir, os.path.basename(src))
        if not overwrite:
            target = unique_path(target)
    tmp = f"{target}.part"
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        try:
            os.remove(src)
        except OSError:
            pass
        # 暂存租约随消费结束而释放;非暂存来源这里是空操作。
        _release_staging(src)
    return target


class ConvertError(Exception):
    pass


# FLAC 压缩级别。6 与 8 的体积差距约 0.5%,编码却快约一倍;
# FLAC 是无损编码,级别不影响解码结果与音质。
FLAC_COMPRESSION_LEVEL = 6


# 常用输出采样率 (Hz)。None / 0 表示跟随源文件,不重采样。
SAMPLE_RATE_CHOICES: tuple[int, ...] = (44100, 48000, 96000, 192000)

# 支持内嵌封面的输出格式。WAV/AIFF 这类无标签容器除外。
PICTURE_CAPABLE_FORMATS = {"flac", "mp3", "m4a", "aac", "ogg", "opus", "wma", "ape", "alac"}


def _sniff_image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def extract_picture(path: str) -> bytes:
    """从音频文件读出内嵌封面的原始字节;没有封面或读取失败时返回空。

    ffmpeg 会把 FLAC/MP3 的封面当作 attached_pic 视频流,我们的转码命令用
    `-map 0:a:0 -vn` 只保留音频,封面必然丢失。因此这里先用 mutagen 把封面
    抠出来,转码完成后再写回去。
    """
    if not path or not os.path.isfile(path):
        return b""
    try:
        import mutagen

        audio = mutagen.File(path)
        if audio is None:
            return b""

        # FLAC / 其他提供 .pictures 的格式
        pictures = getattr(audio, "pictures", None)
        if pictures:
            return bytes(pictures[0].data)

        tags = getattr(audio, "tags", None)
        if not tags:
            return b""
        try:
            keys = list(tags.keys())
        except Exception:  # noqa: BLE001 - 某些 tags 实现不支持 keys()
            return b""

        for key in keys:
            name = str(key).upper()
            value = tags[key]
            # MP4/M4A: covr
            if name == "COVR" and value:
                entry = value[0] if isinstance(value, (list, tuple)) else value
                return bytes(entry)
            # ID3: APIC:description
            if name.startswith("APIC"):
                data = getattr(value, "data", None)
                if data:
                    return bytes(data)
            # APEv2 / VorbisComment: COVERART (裸字节或 base64)
            if name in ("COVERART", "COVERARTFRONT"):
                if isinstance(value, (bytes, bytearray)):
                    return bytes(value)
                if isinstance(value, str):
                    import base64 as _b64
                    try:
                        return _b64.b64decode(value, validate=True)
                    except Exception:  # noqa: BLE001
                        continue
            # VorbisComment: METADATA_BLOCK_PICTURE (base64 FLAC Picture 块)
            if name == "METADATA_BLOCK_PICTURE" and isinstance(value, str):
                import base64 as _b64
                try:
                    from mutagen.flac import Picture
                    pic = Picture(_b64.b64decode(value, validate=True))
                    return bytes(pic.data)
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001 - 取封面失败绝不影响转码本身
        return b""
    return b""


def attach_picture(path: str, data: bytes, fmt: str = "") -> bool:
    """把封面字节写回音频文件;成功返回 True。

    FLAC 走 PICTURE 块,MP3 走 ID3 APIC,其余格式尽力而为,失败一律不影响主流程。
    """
    if not data:
        return False
    target = (fmt or os.path.splitext(path)[1].lstrip(".")).lower()
    mime = _sniff_image_mime(data)
    try:
        if target == "flac":
            from mutagen.flac import FLAC, Picture

            audio = FLAC(path)
            # 避免重复添加同一张封面
            if audio.pictures:
                audio.clear_pictures()
            pic = Picture()
            pic.type = 3  # front cover
            pic.mime = mime
            pic.data = data
            audio.add_picture(pic)
            audio.save()
            return True
        if target == "mp3":
            from mutagen.id3 import APIC, ID3
            from mutagen.mp3 import MP3

            audio = MP3(path)
            if audio.tags is None:
                audio.add_tags()
            try:
                audio.tags.delall("APIC")
            except Exception:  # noqa: BLE001 - 没有旧封面可删
                pass
            audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            audio.save()
            return True
    except Exception:  # noqa: BLE001 - 写封面失败不致命
        return False
    return False


def normalize_sample_rate(value: Optional[object]) -> Optional[int]:
    """把 UI/配置里的采样率值规范成 ffmpeg 可用的整数 Hz,无效输入返回 None。"""
    if value is None:
        return None
    try:
        rate = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if rate <= 0:
        return None
    # 拒绝明显异常的取值,避免用户输入导致 ffmpeg 产出超大文件。
    if rate < 8000 or rate > 384000:
        return None
    return rate


def convert_file(
    src: str,
    dst: str,
    fmt: str,
    on_progress: Optional[Callable[[float], None]] = None,
    on_line: Optional[Callable[[str], None]] = None,
    ffmpeg: Optional[str] = None,
    sample_rate: Optional[int] = None,
) -> str:
    """转码单个文件,返回输出路径。

    sample_rate 指定输出采样率 (Hz);None 表示保持源文件采样率不做重采样。
    重采样只改变采样规格,不会提升原始音质;从有损源重采样也不还原细节。
    """
    src = os.path.abspath(os.fspath(src))
    dst = os.path.abspath(os.fspath(dst))
    if not os.path.isfile(src):
        raise ConvertError(f"文件不存在: {src}")
    if fmt not in ("flac", "wav", "mp3"):
        raise ConvertError(f"不支持的输出格式: {fmt}")
    if os.path.normcase(src) == os.path.normcase(dst):
        raise ConvertError("输出路径不能与源文件相同,请改用其他文件名或目录")

    target_rate = normalize_sample_rate(sample_rate)

    ffmpeg_exe = _resolve_executable(ffmpeg) if ffmpeg else find_ffmpeg()
    if not ffmpeg_exe:
        raise ConvertError("未找到 ffmpeg,请先安装并加入 PATH")

    dst_dir = os.path.dirname(dst) or "."
    os.makedirs(dst_dir, exist_ok=True)

    # 一次探测同时拿到时长(进度条用)与 WAV 位深(编码器选择用),
    # 不再为每个字段各拉一个 ffprobe 子进程。
    media = probe_media(src, ffmpeg_exe)
    if fmt == "flac":
        codec_args = ["-c:a", "flac", "-compression_level", str(FLAC_COMPRESSION_LEVEL)]
    elif fmt == "mp3":
        codec_args = ["-c:a", "libmp3lame", "-b:a", "320k"]
    else:  # wav - 跟随源位深,避免 24/32-bit 源被降为 16-bit
        codec_args = _wav_codec_args(src, ffmpeg_exe, media)

    # 采样率:不指定时完全跟随源文件,保证无损链路不引入任何重采样。
    rate_args: list[str] = []
    if target_rate:
        rate_args = ["-ar", str(target_rate)]

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
        *rate_args,
        temp_path,
    ]

    tail: deque[str] = deque(maxlen=20)
    # `-map 0:a:0 -vn` 只保留音频,内嵌封面会被丢掉。目标格式能存封面时,
    # 先把源封面抠出来,转码完成后再写回去。
    cover = b""
    if fmt in PICTURE_CAPABLE_FORMATS:
        cover = extract_picture(src)

    # Probe before starting ffmpeg so its merged stderr/stdout pipe is drained
    # immediately after launch and cannot fill while a probe is running.
    duration = media.duration
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

        if cover:
            # 在 os.replace 之前回写封面,失败也不影响音频输出。
            # 注意不能把 on_line 写进条件里:多数调用方不传日志回调。
            if not attach_picture(temp_path, cover, fmt) and on_line:
                on_line("提示: 封面未能写入输出文件")

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


def _resample_encoder_args(codec_name: str, bitrate: str = "") -> list[str]:
    """按源编码选择重采样后的目标编码。

    注意:ffmpeg 不允许在流拷贝(-c copy)的同时改变采样率,必须重新编码。
    这里尽量沿用源编码族,并给它足够高的码率,避免二次有损压缩叠加。
    """
    codec = (codec_name or "").lower()
    if codec == "flac":
        return ["-c:a", "flac", "-compression_level", str(FLAC_COMPRESSION_LEVEL)]
    if codec.startswith("pcm") or codec == "wav":
        return ["-c:a", codec if codec.startswith("pcm") else "pcm_s16le"]
    if codec == "aac":
        return ["-c:a", "aac", "-b:a", bitrate or "320k"]
    if codec in ("libopus", "opus"):
        return ["-c:a", "libopus", "-b:a", bitrate or "192k"]
    if codec in ("libmp3lame", "mp3"):
        return ["-c:a", "libmp3lame", "-b:a", bitrate or "320k"]
    if codec in ("libvorbis", "vorbis"):
        return ["-c:a", "libvorbis", "-q:a", "6"]
    # 未知编码:退回到稳妥的高码率 AAC。
    return ["-c:a", "aac", "-b:a", bitrate or "320k"]


@dataclass
class MediaInfo:
    """一次探测拿到的音频流信息;探测失败的字段保持默认值,由调用方兜底。"""

    duration: float = 0.0
    sample_rate: int = 0
    codec_name: str = ""
    bit_rate: str = ""
    sample_fmt: str = ""
    bits_per_sample: int = 0


def probe_media(path: str, ffmpeg: Optional[str] = None) -> MediaInfo:
    """用一次 ffprobe 调用取回时长/采样率/编码/位深等全部信息。

    旧实现对每个字段分别拉起一个 ffprobe 子进程(每个约 50-100ms),
    大批量任务下纯属浪费;这里合并为一次调用,ffprobe 缺失时回退到
    解析 ffmpeg stderr 的 Duration。
    """
    info = MediaInfo()
    ffmpeg_exe = _resolve_executable(ffmpeg) if ffmpeg else find_ffmpeg()
    ffprobe = find_ffprobe(ffmpeg_exe)
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-show_format", path],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            data = json.loads(out)
            for stream in data.get("streams", []):
                if stream.get("codec_type") != "audio":
                    continue
                info.codec_name = str(stream.get("codec_name", "") or "")
                info.bit_rate = str(stream.get("bit_rate", "") or "")
                info.sample_fmt = str(stream.get("sample_fmt", "") or "")
                try:
                    info.bits_per_sample = int(
                        stream.get("bits_per_raw_sample")
                        or stream.get("bits_per_sample")
                        or 0
                    )
                except (TypeError, ValueError):
                    info.bits_per_sample = 0
                rate = str(stream.get("sample_rate", "") or "")
                if rate.isdigit():
                    info.sample_rate = int(rate)
                break
            try:
                info.duration = float((data.get("format") or {}).get("duration") or 0.0)
            except (TypeError, ValueError):
                info.duration = 0.0
            return info
        except Exception:  # noqa: BLE001 - 探测失败走 ffmpeg 兜底
            pass
    if ffmpeg_exe:
        info.duration = _duration_from_ffmpeg(path, ffmpeg_exe)
    return info


def _duration_from_ffmpeg(path: str, ffmpeg_exe: str) -> float:
    try:
        out = subprocess.run(
            [ffmpeg_exe, "-hide_banner", "-nostdin", "-i", path],
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


def _probe_audio_codec(path: str, ffmpeg: str) -> tuple[str, str]:
    """返回 (codec_name, bit_rate);探测失败时返回空串,由调用方走兜底编码器。"""
    info = probe_media(path, ffmpeg)
    return info.codec_name, info.bit_rate


def _probe_sample_rate(path: str, ffmpeg: str) -> int:
    """读取音频流的采样率；失败返回 0。"""
    return probe_media(path, ffmpeg).sample_rate


def resample_in_place(
    path: str,
    sample_rate: int,
    ffmpeg: Optional[str] = None,
    metadata: Optional[dict[str, str]] = None,
    cover: Optional[bytes] = None,
) -> str:
    """保持原音频容器与编码族,只把采样率重采样到 sample_rate,原子替换原文件。

    用于"原格式 + 指定采样率"的场景:例如下载到的 m4a/opus 保持原编码,
    仅重采样。返回最终路径(成功时与入参相同)。

    metadata / cover 会在同一次 ffmpeg 里写入,避免再跑一次 mutagen 全文件
    重写——对 USB / 机械盘来说这是成倍的 I/O 节省。
    """
    rate = normalize_sample_rate(sample_rate)
    if not rate:
        return path
    path = os.path.abspath(os.fspath(path))
    if not os.path.isfile(path):
        raise ConvertError(f"文件不存在: {path}")

    ffmpeg_exe = _resolve_executable(ffmpeg) if ffmpeg else find_ffmpeg()
    if not ffmpeg_exe:
        raise ConvertError("未找到 ffmpeg,请先安装并加入 PATH")

    # 一次探测同时拿到当前采样率(是否需要重写)与源编码族(重编码器选择)。
    media = probe_media(path, ffmpeg_exe)
    current_rate = media.sample_rate
    if current_rate == rate and not metadata and not cover:
        # 已经是目标采样率: 不重写文件, 省掉一次整文件 I/O。
        return path

    ext = os.path.splitext(path)[1] or ".tmp"
    dst_dir = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=ext, dir=dst_dir
    )
    os.close(fd)
    try:
        os.remove(temp_path)
    except OSError:
        pass

    if current_rate == rate:
        # 采样率不变时只写标签/封面, 流拷贝即可, 不重新编码。
        codec_args = ["-c:a", "copy"]
    else:
        # 改变采样率必须重新编码:-c copy 流拷贝时 ffmpeg 会拒绝重采样。
        codec_args = _resample_encoder_args(media.codec_name, media.bit_rate)

    out_fmt = os.path.splitext(path)[1].lstrip(".").lower()
    cover_path: Optional[str] = None
    cmd = [ffmpeg_exe, "-hide_banner", "-nostdin", "-y", "-i", path]
    if cover:
        suffix = ".png" if cover[:4] == b"\x89PNG" else ".jpg"
        fd, cover_path = tempfile.mkstemp(prefix=".cover.", suffix=suffix)
        os.close(fd)
        with open(cover_path, "wb") as fh:
            fh.write(cover)
        cmd += ["-i", cover_path]
        cmd += ["-map", "0:a:0", "-map", "1:v?"]
    else:
        # 保留源文件里已有的 attached_pic(封面), 不丢封面。
        cmd += ["-map", "0:a:0", "-map", "0:v?"]
    cmd += [
        "-c:v", "copy", "-disposition:v", "attached_pic",
        "-map_metadata", "0",
        *codec_args,
    ]
    if current_rate != rate:
        cmd += ["-ar", str(rate)]
    for key, value in (metadata or {}).items():
        if value:
            cmd += ["-metadata", f"{key}={value}"]
    cmd.append(temp_path)

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            # 不设超时:慢机器上长音频的高级别重采样可能远超 10 分钟,
            # 超时会让本可成功的任务整体失败(与 convert_file 的行为对齐)。
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            detail = (proc.stdout or "")[-500:]
            raise ConvertError(f"重采样失败 (ffmpeg {proc.returncode}): {detail}")
        if not os.path.isfile(temp_path) or os.path.getsize(temp_path) <= 0:
            raise ConvertError("重采样后未生成有效输出文件")
        if cover and out_fmt == "flac":
            # 极端情况下 ffmpeg 没把封面写进 FLAC, 再用 mutagen 补一次。
            try:
                from mutagen.flac import FLAC

                if not FLAC(temp_path).pictures:
                    attach_picture(temp_path, cover, out_fmt)
            except Exception:  # noqa: BLE001 - 封面失败不影响音频
                pass
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        if cover_path and os.path.exists(cover_path):
            try:
                os.remove(cover_path)
            except OSError:
                pass
    return path


def finalize_decrypted(
    raw: str,
    fmt: str,
    sample_rate: Optional[int] = None,
    ffmpeg: Optional[str] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    metadata: Optional[dict[str, str]] = None,
    cover: Optional[bytes] = None,
) -> str:
    """把解密产物落到目标格式。

    ``fmt == "original"`` 表示保持解密得到的原始格式：只按需重采样，
    绝不转码——"original" 不是 ffmpeg 的输出格式，不能往下传。

    其他格式会转码到同目录下的新文件，成功后才删除原始解密产物；
    转码失败时保留原始文件，避免用户拿不到已经解密出来的音频。
    """
    raw = os.path.abspath(os.fspath(raw))
    if fmt == "original":
        if sample_rate or metadata or cover:
            resample_in_place(
                raw, sample_rate, ffmpeg=ffmpeg, metadata=metadata, cover=cover
            )
        return raw

    current = os.path.splitext(raw)[1].lstrip(".").lower()
    if current == fmt:
        if sample_rate or metadata or cover:
            resample_in_place(
                raw, sample_rate, ffmpeg=ffmpeg, metadata=metadata, cover=cover
            )
        return raw

    out_dir = os.path.dirname(raw) or "."
    final = build_output_path(raw, out_dir, fmt)
    if os.path.normcase(os.path.abspath(final)) == os.path.normcase(raw):
        i = 1
        while True:
            candidate = os.path.join(
                out_dir, f"{os.path.splitext(os.path.basename(raw))[0]}_{i}.{fmt}"
            )
            if not os.path.exists(candidate):
                final = candidate
                break
            i += 1
    try:
        convert_file(
            raw,
            final,
            fmt,
            on_progress=(
                (lambda p: on_progress(50.0 + p * 0.5)) if on_progress else None
            ),
            ffmpeg=ffmpeg,
            sample_rate=sample_rate,
        )
    except Exception:
        if os.path.exists(final):
            try:
                os.remove(final)
            except OSError:
                pass
        raise
    try:
        os.remove(raw)
    except OSError:
        pass
    return final


def _probe_duration(path: str, ffmpeg: str) -> float:
    """用 ffprobe(或 ffmpeg)获取时长,失败返回 0。"""
    return probe_media(path, ffmpeg).duration


def _wav_codec_args(
    path: str,
    ffmpeg: str,
    media: Optional[MediaInfo] = None,
) -> list[str]:
    """根据源音频位深选择 WAV PCM 编码器,避免位深降级。

    16-bit -> pcm_s16le, 24-bit -> pcm_s24le, 32-bit int -> pcm_s32le,
    浮点 -> pcm_f32le, 探测失败回退 pcm_s16le。
    ``media`` 可传入已探测的 :class:`MediaInfo`,避免重复拉起 ffprobe。
    """
    media = media or probe_media(path, ffmpeg)
    if media.sample_fmt in ("flt", "fltp", "dbl", "dblp"):
        return ["-c:a", "pcm_f32le"]
    if media.bits_per_sample >= 32:
        return ["-c:a", "pcm_s32le"]
    if media.bits_per_sample >= 24:
        return ["-c:a", "pcm_s24le"]
    return ["-c:a", "pcm_s16le"]
