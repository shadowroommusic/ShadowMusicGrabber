# -*- coding: utf-8 -*-
"""GitHub Releases 自动更新。

更新源固定为本项目仓库，只从该仓库的 Release 下载资产，避免任意 URL 被当作
更新包安装。默认走 GitHub API；当 API 被限流（未登录时每小时 60 次）或不可用时，
退回网页跳转方式读取最新 tag，并使用仓库固定的 latest 下载地址。
仅打包后的 exe 支持就地替换；源码运行时改为打开下载页。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

REPO = "shadowroommusic/MusicGrabber"
API_LATEST_RELEASE = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
LATEST_PAGE = f"https://github.com/{REPO}/releases/latest"
LATEST_ASSET_URL = f"https://github.com/{REPO}/releases/latest/download/MusicGrabber.exe"
ASSET_NAME = "MusicGrabber.exe"
USER_AGENT = "MusicGrabber-Updater"
TIMEOUT = 20
_CHUNK = 256 * 1024


class UpdateError(RuntimeError):
    """更新检查或下载失败。"""


class UpdateInfo:
    """一个可用的新版本。"""

    def __init__(
        self,
        *,
        tag: str,
        name: str = "",
        notes: str = "",
        asset_name: str = "",
        asset_url: str = "",
        asset_size: int = 0,
    ):
        self.tag = tag
        self.name = name
        self.notes = notes
        self.asset_name = asset_name
        self.asset_url = asset_url
        self.asset_size = asset_size

    @property
    def title(self) -> str:
        return self.name or self.tag


def parse_version(text: str) -> tuple[int, ...]:
    """把 v1.2.3 / 1.2.3-beta 之类解析成可比较的数字元组。"""
    numbers = re.findall(r"\d+", str(text or ""))
    return tuple(int(part) for part in numbers[:4]) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    left, right = parse_version(candidate), parse_version(current)
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (width - len(right))


def can_self_update() -> bool:
    """只有在打包后的 Windows exe 中运行才允许就地替换。"""
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def _http_error_message(code: int) -> str:
    if code == 404:
        return "更新仓库还没有发布任何 Release。"
    if code in (403, 429):
        return "更新服务暂时限制了访问频率，请稍后再试。"
    return f"更新服务返回 HTTP {code}。"


def _get_json(url: str) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise UpdateError(_http_error_message(exc.code)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"无法连接更新服务: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UpdateError("更新服务返回了无法解析的数据。") from exc


def _select_asset(assets: list) -> dict | None:
    """优先选取 exe，其次 zip。"""
    ranked = []
    for asset in assets or []:
        name = str(asset.get("name", ""))
        lower = name.lower()
        if lower.endswith(".exe"):
            rank = 0
        elif lower.endswith(".zip"):
            rank = 1
        else:
            continue
        ranked.append((rank, lower, asset))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]))
    return ranked[0][2]


def _check_via_api(current_version: str) -> UpdateInfo | None:
    data = _get_json(API_LATEST_RELEASE)
    tag = str(data.get("tag_name") or data.get("name") or "").strip()
    if not tag or not is_newer(tag, current_version):
        return None
    asset = _select_asset(data.get("assets") or [])
    return UpdateInfo(
        tag=tag,
        name=str(data.get("name") or "").strip(),
        notes=str(data.get("body") or "").strip(),
        asset_name=str(asset.get("name") or "") if asset else "",
        asset_url=str(asset.get("browser_download_url") or "") if asset else "",
        asset_size=int(asset.get("size") or 0) if asset else 0,
    )


def _latest_tag_via_web() -> str:
    """读取 releases/latest 的最终跳转地址，得到最新 tag（不受 API 限额影响）。"""
    request = urllib.request.Request(
        LATEST_PAGE, headers={"User-Agent": USER_AGENT}, method="HEAD"
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            final = str(response.geturl() or "")
    except urllib.error.HTTPError as exc:
        raise UpdateError(_http_error_message(exc.code)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"无法连接更新服务: {exc}") from exc
    tail = final.rstrip("/").rsplit("/", 1)[-1]
    return tail if tail and tail != "latest" else ""


def check_for_update(current_version: str) -> UpdateInfo | None:
    """返回可下载的新版本；已是最新时返回 None。"""
    api_error: UpdateError | None = None
    try:
        return _check_via_api(current_version)
    except UpdateError as exc:
        api_error = exc

    # API 限流或异常时退回网页方式；仍失败则汇报最初的错误。
    try:
        tag = _latest_tag_via_web()
    except UpdateError:
        if api_error is not None:
            raise api_error
        raise
    if not tag or not is_newer(tag, current_version):
        return None
    return UpdateInfo(tag=tag, asset_name=ASSET_NAME, asset_url=LATEST_ASSET_URL)


def download_update(
    info: UpdateInfo, on_progress=None, dest_dir: str | None = None
) -> str:
    """下载更新资产到临时目录，返回文件路径。"""
    if not info.asset_url:
        raise UpdateError("这个 Release 没有可下载的 exe/zip 资产。")
    target_dir = dest_dir or os.path.join(tempfile.gettempdir(), "MusicGrabber-update")
    os.makedirs(target_dir, exist_ok=True)
    filename = info.asset_name or os.path.basename(info.asset_url) or ASSET_NAME
    target = os.path.join(target_dir, filename)
    request = urllib.request.Request(info.asset_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response, open(
            target, "wb"
        ) as handle:
            total = int(response.headers.get("Content-Length") or info.asset_size or 0)
            done = 0
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done / total if total else 0.0)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"下载更新失败: {exc}") from exc
    if info.asset_size and os.path.getsize(target) != info.asset_size:
        raise UpdateError("下载的文件大小与 Release 不一致，已取消安装。")
    return target


_APPLY_SCRIPT = r"""@echo off
chcp 65001 >nul
setlocal
set "TARGET={target}"
set "SOURCE={source}"
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if not errorlevel 1 (
  ping -n 2 127.0.0.1 >nul
  goto wait
)
for /L %%i in (1,1,30) do (
  copy /Y "%SOURCE%" "%TARGET%" >nul 2>nul && goto done
  ping -n 2 127.0.0.1 >nul
)
goto cleanup
:done
start "" "%TARGET%"
:cleanup
del "%SOURCE%" >nul 2>nul
del "%~f0" >nul 2>nul
"""


def build_apply_script(target_exe: str, source_exe: str, pid: int) -> str:
    """生成「等待退出 → 替换文件 → 重新启动」的批处理脚本内容。"""
    return _APPLY_SCRIPT.format(target=target_exe, source=source_exe, pid=pid)


def install_and_restart(downloaded: str, target_exe: str | None = None) -> str:
    """启动替换脚本（调用方随后应立即退出程序），返回脚本路径。"""
    if not can_self_update():
        raise UpdateError("当前以源码方式运行，不能自动替换程序文件。")
    target = target_exe or sys.executable
    script_dir = os.path.dirname(os.path.abspath(downloaded))
    script = os.path.join(script_dir, "apply_update.cmd")
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(build_apply_script(target, os.path.abspath(downloaded), os.getpid()))
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    subprocess.Popen(["cmd.exe", "/c", script], creationflags=flags, close_fds=True)
    return script


def open_releases_page() -> None:
    """在浏览器里打开本项目的 Releases 页面。"""
    import webbrowser

    webbrowser.open(RELEASES_PAGE)
