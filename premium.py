# -*- coding: utf-8 -*-
"""Apple Music / Beatport 下载集成。

- Apple Music: 调用 gamdl(需要 Apple Music 订阅 + 浏览器导出的 cookies.txt)
- Beatport:    调用 beatportdl(需要 Beatport 流媒体订阅账号)

两者都需要用户自己的付费订阅凭据;本模块只负责调用与配置。
"""

from __future__ import annotations

import os
import re
import json
import shutil
import subprocess
import sys
from typing import Callable, Optional

# BeatportDL 质量选项: (显示名, 配置值, 需要订阅档)
BEATPORT_QUALITIES = {
    "FLAC 无损 (44.1kHz)": ("lossless", "Professional"),
    "AAC 256kbps": ("high", "Professional"),
    "AAC 128kbps": ("medium", "Advanced"),
    "AAC 128kbps (HLS)": ("medium-hls", "Essential"),
}

# gamdl 编码选项: (显示名, codec 值, 说明)
GAMDL_CODECS = {
    "AAC 256kbps (推荐, 无需额外配置)": "aac-web",
    "ALAC 无损 24bit/192kHz (需 wrapper 服务)": "alac",
}

_OUTPUT_EXTENSIONS = (".m4a", ".mp4", ".flac", ".mp3", ".lrc", ".jpg", ".jpeg", ".png")
_AUDIO_EXTENSIONS = (".m4a", ".mp4", ".flac", ".mp3")


class PremiumError(Exception):
    pass


def is_apple_music_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and (parts.hostname or "").lower() == "music.apple.com"


def is_beatport_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme.lower() in {"http", "https"} and (host == "beatport.com" or host.endswith(".beatport.com"))


def _find_beatportdl() -> Optional[str]:
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        bundled = os.path.join(meipass, "bin", "beatportdl.exe")
        if os.path.isfile(bundled):
            return os.path.abspath(bundled)
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if sys.executable else ""
    if exe_dir:
        for candidate in (
            os.path.join(exe_dir, "bin", "beatportdl.exe"),
            os.path.join(exe_dir, "beatportdl.exe"),
        ):
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
    # Source checkout fallback: program's own bin/ directory.
    here = os.path.dirname(os.path.abspath(__file__))
    bundled = os.path.join(here, "bin", "beatportdl.exe")
    if os.path.exists(bundled):
        return bundled
    return shutil.which("beatportdl")


APP_DIR_NAME = "ShadowMusicGrabber"
_LEGACY_DIR_NAME = "MusicGrabber"


def _app_data_dir() -> str:
    """运行配置（如 BeatportDL 配置）的保存目录。

    应用改名为 Shadow MusicGrabber 后，首次运行会把旧目录的配置迁过来，
    避免用户重新填写账号。
    """
    root = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(root, APP_DIR_NAME)
    legacy = os.path.join(root, _LEGACY_DIR_NAME)
    if not os.path.isdir(path) and os.path.isdir(legacy):
        try:
            shutil.copytree(legacy, path)
            return path
        except OSError:
            pass
    os.makedirs(path, exist_ok=True)
    return path


def _yaml_scalar(value: str) -> str:
    """Encode user input as a YAML double-quoted scalar."""
    return json.dumps(str(value), ensure_ascii=False)


def _snapshot_output_files(out_dir: str) -> dict[str, tuple[int, int]]:
    """Capture output files so a run cannot report stale downloads as new."""
    snapshot: dict[str, tuple[int, int]] = {}
    for root, _dirs, names in os.walk(out_dir):
        for name in names:
            if not name.lower().endswith(_OUTPUT_EXTENSIONS):
                continue
            path = os.path.abspath(os.path.join(root, name))
            try:
                stat = os.stat(path)
            except OSError:
                continue
            snapshot[path] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def _collect_changed_output_files(
    out_dir: str, before: dict[str, tuple[int, int]]
) -> tuple[list[str], list[str]]:
    """Return changed output files and changed audio files separately."""
    changed: list[str] = []
    audio: list[str] = []
    after = _snapshot_output_files(out_dir)
    for path, signature in after.items():
        if before.get(path) == signature:
            continue
        changed.append(path)
        if path.lower().endswith(_AUDIO_EXTENSIONS):
            audio.append(path)
    changed.sort()
    audio.sort()
    return changed, audio


def gamdl_download(
    url: str,
    cookies_path: str,
    out_dir: str,
    codec: str = "aac-web",
    on_log: Optional[Callable[[str], None]] = None,
    ffmpeg_location: Optional[str] = None,
) -> list[str]:
    """调用 gamdl 下载 Apple Music 内容,返回输出文件列表(尽力而为)。

    codec: aac-web(AAC 256) / alac(需 wrapper)
    """
    if not is_apple_music_url(url):
        raise PremiumError("不是 Apple Music 链接")
    if not cookies_path or not os.path.isfile(cookies_path):
        raise PremiumError(
            "未找到 cookies.txt。请先在 Apple Music 网页登录后,"
            "用浏览器扩展(如 Get cookies.txt LOCALLY)导出 Netscape 格式 cookies。"
        )
    os.makedirs(out_dir, exist_ok=True)
    before = _snapshot_output_files(out_dir)

    args = [
        "-n",  # 不用配置文件
        "-c", cookies_path,
        "-o", out_dir,
        "--song-codec-priority", codec,
        "--no-exceptions",
        url,
    ]
    if ffmpeg_location:
        args += ["--ffmpeg-path", ffmpeg_location]

    if getattr(sys, "frozen", False):
        # 打包后的 exe:进程内调用 gamdl(子进程 -m gamdl 不可用)
        rc = _run_gamdl_inline(args, on_log)
    else:
        rc = _run_gamdl_subprocess(args, on_log)
    if rc != 0:
        raise PremiumError(f"gamdl 退出码 {rc},请查看上方日志(常见原因:cookies 过期或订阅无效)")

    # 只报告本次新增/变更的文件，避免旧结果让 GUI 显示“下载成功”。
    files, audio_files = _collect_changed_output_files(out_dir, before)
    if not audio_files:
        raise PremiumError(
            "gamdl 已返回成功,但没有生成新的音频文件;请检查 URL、cookies、订阅和地区权限。"
        )
    return files


def _run_gamdl_subprocess(
    args: list[str], on_log: Optional[Callable[[str], None]] = None
) -> int:
    cmd = [sys.executable, "-m", "gamdl", *args]
    if on_log:
        on_log("gamdl: " + " ".join(cmd))
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
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if on_log:
                on_log(line)
        proc.stdout.close()
        return proc.wait()
    except FileNotFoundError as e:
        raise PremiumError(f"无法启动 gamdl(未安装?): {e}") from e


def _run_gamdl_inline(
    args: list[str], on_log: Optional[Callable[[str], None]] = None
) -> int:
    """在进程内运行 gamdl 的 click CLI(打包 exe 场景)。"""
    import contextlib
    import io

    try:
        from gamdl.cli.cli import main as gamdl_main
    except ImportError as e:
        raise PremiumError(f"gamdl 未随程序打包: {e}") from e

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            gamdl_main.main(args=args, standalone_mode=False)
        rc = 0
    except SystemExit as e:  # click 正常退出
        rc = int(e.code or 0)
    except Exception as e:  # noqa: BLE001
        rc = 1
        if on_log:
            on_log(f"gamdl 错误: {e}")
    for line in buf.getvalue().splitlines():
        if on_log:
            on_log(line)
    return rc


def beatportdl_download(
    url: str,
    username: str,
    password: str,
    out_dir: str,
    quality: str = "lossless",
    on_log: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """调用 beatportdl 下载 Beatport 内容,返回输出文件列表。

    quality: lossless / high / medium / medium-hls
    """
    if not is_beatport_url(url):
        raise PremiumError("不是 Beatport 链接")
    if not username or not password:
        raise PremiumError("请填写 Beatport 账号(用户名/密码)")
    if quality not in {value[0] for value in BEATPORT_QUALITIES.values()}:
        raise PremiumError(f"Unsupported Beatport quality: {quality}")
    exe = _find_beatportdl()
    if not exe:
        raise PremiumError("未找到 beatportdl,请将其放入程序 bin/ 目录")
    os.makedirs(out_dir, exist_ok=True)
    before = _snapshot_output_files(out_dir)

    # 生成配置文件(beatportdl 首次运行会交互询问,直接写配置文件跳过)
    # Keep credentials outside the install/_MEIPASS directory. BeatportDL
    # resolves its config relative to the process working directory.
    workdir = _app_data_dir()
    cfg = os.path.join(workdir, "beatportdl-config.yml")
    try:
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(
                f"username: {_yaml_scalar(username)}\n"
                f"password: {_yaml_scalar(password)}\n"
                f"quality: {_yaml_scalar(quality)}\n"
                f"downloads_directory: {_yaml_scalar(os.path.abspath(out_dir))}\n"
                f"show_progress: false\n"
                f"write_error_log: true\n"
                f"max_download_workers: 4\n"
                f"max_global_workers: 4\n"
            )
    except OSError as e:
        raise PremiumError(f"无法写入 beatportdl 配置: {e}") from e

    # -q makes beatportdl exit after the supplied URL is handled. Without it,
    # the tool returns to its interactive prompt and the GUI worker never ends.
    cmd = [exe, "-q", url]
    if on_log:
        on_log("beatportdl: " + url)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # beatportdl pauses for an acknowledgement after fatal errors. Send
        # an immediate newline so a failed job cannot leave the GUI blocked.
        if proc.stdin is not None:
            try:
                proc.stdin.write("\n")
                proc.stdin.flush()
                proc.stdin.close()
            except OSError:
                pass
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if on_log:
                on_log(line)
        proc.stdout.close()
        rc = proc.wait()
    except FileNotFoundError as e:
        raise PremiumError(f"无法启动 beatportdl: {e}") from e
    if rc != 0:
        raise PremiumError(f"beatportdl 退出码 {rc},请查看上方日志(常见原因:订阅档不支持所选音质或账号错误)")

    files, audio_files = _collect_changed_output_files(out_dir, before)
    if not audio_files:
        raise PremiumError(
            "beatportdl 已返回成功,但没有生成新的音频文件;请检查账号、订阅档、URL 和地区权限。"
        )
    return files
