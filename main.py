# -*- coding: utf-8 -*-
"""音乐抓取与无损转码工具 - Windows 桌面版。

提供公开链接下载、本地转码,以及用户自有网易云 .ncm 与 QQ 音乐加密文件(.qmc/.mflac/.mgg 等)的本地还原。
不包含 DRM、订阅、地区或访问限制绕过能力。
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from logging.handlers import RotatingFileHandler
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk

import appsettings
import converter
import downloader
import dragdrop
import fingerprint
import i18n
import ncm_decrypt
import premium
import qmc_decrypt
import updater
from _version import __version__ as APP_VERSION
from batching import BatchCancelled, BatchExecutor

# 并行批次的线程数:XOR 解密与 ffmpeg 子进程都释放 GIL,
# 2-4 个并发对大批量接近线性提速;太高会互相抢磁盘,得不偿失。
BATCH_WORKERS = min(4, os.cpu_count() or 1)
# 同时进行的下载任务数(每个下载还可能再起 ffmpeg 后处理)。
DOWNLOAD_CONCURRENCY = 3

APP_NAME = "Shadow MusicGrabber"

# 日志文件: 用户遇到卡顿/失败时可以直接回传这个文件定位问题。
LOG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "ShadowMusicGrabber",
    "logs",
)
LOG_PATH = os.path.join(LOG_DIR, "app.log")


def _setup_logging() -> logging.Logger:
    """把界面日志与未捕获异常写入可回传的日志文件。"""
    logger = logging.getLogger("shadowmusicgrabber")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
        )
        logger.addHandler(handler)
    except OSError:
        pass
    return logger


log = _setup_logging()

# 界面语言：先读已保存的设置，否则按系统语言；控件在构造时由 i18n 统一翻译。
i18n.install()
i18n.load_language()

# Shadowroom-inspired visual language: near-black canvas, quiet panels, and a
# single acid-lime action color for controls that start work.
APP_BG = "#0a0a0b"
PANEL = "#151517"
PANEL_ALT = "#1c1c1f"
BORDER = "#2a2a2e"
TEXT = "#f4f4f5"
MUTED = "#9b9ba3"
ACCENT = "#c4f04c"
ACCENT_HOVER = "#a8d63c"
BUTTON = "#29292d"
BUTTON_HOVER = "#3a3a40"

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")

FORMAT_LABELS = {
    downloader.FormatKind.FLAC: "FLAC (无损)",
    downloader.FormatKind.WAV: "WAV (无损)",
    downloader.FormatKind.MP3: "MP3 (320kbps)",
    downloader.FormatKind.ORIGINAL: "原格式 (不转码)",
}
FORMAT_ORDER = [
    downloader.FormatKind.FLAC,
    downloader.FormatKind.WAV,
    downloader.FormatKind.MP3,
    downloader.FormatKind.ORIGINAL,
]

# 采样率选项。"" 表示跟随源文件,不触发重采样。四个页面共用同一份定义。
CV_SAMPLE_RATE_CHOICES: tuple[str, ...] = ("", "44100", "48000", "96000", "192000")
CV_SAMPLE_RATE_LABELS = {
    "": "保持原始",
    "44100": "44100 Hz (CD)",
    "48000": "48000 Hz (视频)",
    "96000": "96000 Hz (Hi-Res)",
    "192000": "192000 Hz (Hi-Res+)",
}

# 解密页的输出格式：值用于扩展名/转码，显示名交给 i18n 翻译。
DECRYPT_FORMAT_LABELS = {
    "原始格式 (解密得到什么就是什么)": "original",
    "FLAC (无损)": "flac",
    "WAV (无损)": "wav",
    "MP3 (320kbps)": "mp3",
}
DECRYPT_TARGET_FORMATS = {"original", "flac", "wav", "mp3"}

# 同名文件处理策略：显示名 -> 内部值。
DECRYPT_EXISTS_LABELS = {
    "自动改名（不覆盖）": "rename",
    "跳过已存在": "skip",
    "覆盖已存在": "overwrite",
}


def resource_path(*parts: str) -> str:
    """定位随程序分发的资源（源码运行与 PyInstaller 打包都能用）。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def task_label(task) -> str:
    """从任务对象提取显示标签,兼容 DownloadTask / ConvertTask / NcmTask。"""
    title = getattr(task, "title", "") or getattr(task, "url", "") or ""
    if not title:
        src = getattr(task, "src", "") or ""
        title = os.path.basename(src)
    return title


class TaskRow:
    """下载任务行 UI。"""

    def __init__(self, parent, task: downloader.DownloadTask):
        self.task = task
        self.frame = ctk.CTkFrame(
            parent, corner_radius=6, fg_color=PANEL_ALT,
            border_width=1, border_color=BORDER,
        )
        self.frame.pack(fill="x", padx=6, pady=3)
        top = ctk.CTkFrame(self.frame, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(6, 0))
        self.title_lbl = ctk.CTkLabel(top, text=task_label(task), anchor="w")
        self.title_lbl.pack(side="left", fill="x", expand=True)
        self.status_lbl = ctk.CTkLabel(top, text=task.status, width=150, anchor="e", text_color=MUTED)
        self.status_lbl.pack(side="right")
        self.progress = ctk.CTkProgressBar(
            self.frame, height=7, fg_color="#303035", progress_color=ACCENT
        )
        self.progress.pack(fill="x", padx=8, pady=(5, 2))
        self.progress.set(0)
        self.detail_lbl = ctk.CTkLabel(self.frame, text="", anchor="w", text_color=MUTED)
        self.detail_lbl.pack(fill="x", padx=8, pady=(0, 6))

    def refresh(self):
        self.title_lbl.configure(text=task_label(self.task))
        self.status_lbl.configure(text=self.task.status)
        self.progress.set(self.task.progress / 100.0)
        error = getattr(self.task, "error", "")
        if error:
            detail = error
        elif getattr(self.task, "file_path", ""):
            detail = getattr(self.task, "file_path")
        else:
            speed = getattr(self.task, "speed", "")
            eta = getattr(self.task, "eta", "")
            detail = "  ".join(part for part in (speed, f"剩余 {eta}" if eta else "") if part)
        self.detail_lbl.configure(text=detail)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.configure(fg_color=APP_BG)
        self.title(f"{APP_NAME} v{APP_VERSION} · {i18n.tr('音乐抓取与无损转码工具')}")
        self.geometry("1120x820")
        self.minsize(960, 700)
        self._apply_window_icon()

        self.default_out_dir = os.path.join(os.path.expanduser("~"), "Music", "ShadowMusicGrabber")
        self.tasks: list[downloader.DownloadTask] = []
        self.task_rows: list[TaskRow] = []
        # id(task) -> 行对象: 几百个任务时, 逐条线性查找会成为卡顿来源。
        self.task_row_map: dict[int, TaskRow] = {}
        self.cv_row_map: dict[int, TaskRow] = {}
        self.ncm_row_map: dict[int, TaskRow] = {}
        self.ui_queue: queue.Queue = queue.Queue()
        self._running_jobs: set[str] = set()
        self._job_remaining: dict[str, int] = {}
        self._last_progress_update: dict[int, float] = {}
        self._progress_lock = threading.Lock()
        # 同名成品"查重 + 落盘"的互斥锁:并行批次下两个同名文件会同时
        # 通过 exists 检查然后互相覆盖。
        self._rename_lock = threading.Lock()
        # 限制同时进行的下载数(每 URL 一个线程,这里统一闸门)。
        self._dl_semaphore = threading.BoundedSemaphore(DOWNLOAD_CONCURRENCY)
        # 各模块的取消开关与批次执行器(取消按钮 → flag/executor)。
        self._cancel_flags: dict[str, threading.Event] = {}
        self._executors: dict[str, BatchExecutor] = {}
        # 各模块的"启动类"按钮(运行中禁用)与取消按钮(运行中启用)。
        self._job_buttons: dict[str, dict] = {}
        # 空队列提示标签,按 tab 键索引。
        self._empty_hints: dict[str, object] = {}
        self._closing = False
        self._update_busy = False
        self.ffmpeg = converter.find_ffmpeg()
        # 解密产物是否按"歌手 - 歌名"重命名(取不到歌名时维持源文件名)。
        self.rename_by_meta = True
        # 缺标签时是否用声学指纹联网补全(需 fpcalc + AcoustID API Key)。
        self.fingerprint_enabled = False
        self.fingerprint_key = ""

        log.info(
            "start v%s | ffmpeg=%s | log=%s",
            APP_VERSION,
            self.ffmpeg or "-",
            LOG_PATH,
        )

        self._build_ui()
        self._apply_saved_settings()
        self._register_setting_savers()
        self.after(30, self._drain_ui_queue)
        self._log(f"就绪。ffmpeg: {self.ffmpeg or '未找到(转码将不可用)'}")
        self._log("提示:仅处理你拥有或明确获授权的内容;不绕过 DRM、订阅、地区或访问限制。")

    def _guide(self, parent, title: str, body: str):
        frame = ctk.CTkFrame(
            parent, fg_color=PANEL, corner_radius=6,
            border_width=1, border_color=BORDER,
        )
        frame.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(
            frame, text=title, anchor="w", justify="left",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=TEXT,
        ).pack(fill="x", padx=10, pady=(8, 1))
        ctk.CTkLabel(
            frame, text=body, anchor="w", justify="left", wraplength=980,
            text_color=MUTED,
        ).pack(fill="x", padx=10, pady=(0, 8))

    def _format_hint(self, fmt: downloader.FormatKind) -> str:
        if fmt == downloader.FormatKind.ORIGINAL:
            return "原格式: 保留站点提供的音频格式,通常无需重新编码。"
        if fmt == downloader.FormatKind.MP3:
            return "MP3: 兼容性优先,会使用 ffmpeg 重新编码。"
        if fmt == downloader.FormatKind.WAV:
            return "WAV: 无压缩容器,文件会较大;从有损源转换不会提升音质。"
        return "FLAC: 无损容器,适合归档;从有损源转换不会提升音质。"

    def _on_cv_rate_change(self, label: str):
        """把下拉框显示文案映射回内部采样率值(下拉项可能已被翻译成英文)。"""
        text = i18n.untr(label)
        for value, zh_label in CV_SAMPLE_RATE_LABELS.items():
            if zh_label == text:
                self.cv_rate_var.set(value)
                if hasattr(self, "cv_rate_hint"):
                    self.cv_rate_hint.configure(text=self._cv_rate_hint(value))
                return

    def _on_dl_rate_change(self, label: str):
        """链接下载页的采样率下拉框。"""
        text = i18n.untr(label)
        for value, zh_label in CV_SAMPLE_RATE_LABELS.items():
            if zh_label == text:
                self.dl_rate_var.set(value)
                if hasattr(self, "dl_rate_hint"):
                    self.dl_rate_hint.configure(text=self._cv_rate_hint(value))
                return

    def _dl_sample_rate(self) -> Optional[int]:
        return converter.normalize_sample_rate(self.dl_rate_var.get())

    def _cv_sample_rate(self) -> Optional[int]:
        """返回当前选择的采样率(None 表示跟随源文件)。"""
        return converter.normalize_sample_rate(self.cv_rate_var.get())

    def _rate_desc(self, sample_rate: Optional[int]) -> str:
        """把采样率拼成日志里的一句话(保持原始时用英文,避免译文里夹中文)。"""
        return f"{sample_rate} Hz" if sample_rate else "source rate"

    def _cv_rate_hint(self, value: str) -> str:
        if not value:
            return "采样率: 保持原始 — 输出跟随每个源文件自身的采样率,不重采样。"
        if value == "48000":
            return "采样率: 48000 Hz — 视频项目标配,与 YouTube/B 站等主流平台一致。"
        if value == "96000":
            return "采样率: 96000 Hz — Hi-Res 规格,文件明显变大,不会提升原始音质。"
        if value == "192000":
            return "采样率: 192000 Hz — 极高规格,文件非常大,仅在源素材本身够高时才有意义。"
        return "采样率: 44100 Hz — CD 经典规格,兼容性最好。"

    def _button(
        self,
        parent,
        text: str,
        command,
        *,
        primary: bool = False,
        width: int = 110,
    ):
        """Create a compact button with the app's dark visual language."""
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=34,
            corner_radius=5,
            fg_color=ACCENT if primary else BUTTON,
            hover_color=ACCENT_HOVER if primary else BUTTON_HOVER,
            text_color="#101012" if primary else TEXT,
            font=ctk.CTkFont(size=13, weight="bold" if primary else "normal"),
        )

    def _begin_job(self, name: str, count: int = 1) -> bool:
        if self._closing:
            return False
        if name in self._running_jobs:
            messagebox.showinfo("任务进行中", "当前模块已有任务正在运行,请等待完成后再开始新的任务。")
            return False
        self._running_jobs.add(name)
        self._job_remaining[name] = max(1, count)
        self._cancel_flags[name] = threading.Event()
        self._set_job_buttons(name, running=True)
        return True

    def _end_job(self, name: str):
        self.ui_queue.put(("job_done", name))

    def _finish_job(self, name: str):
        remaining = self._job_remaining.get(name, 1) - 1
        if remaining > 0:
            self._job_remaining[name] = remaining
            return
        self._job_remaining.pop(name, None)
        self._running_jobs.discard(name)
        self._cancel_flags.pop(name, None)
        self._executors.pop(name, None)
        self._set_job_buttons(name, running=False)

    def _set_job_buttons(self, name: str, running: bool) -> None:
        """按运行状态切换模块按钮:启动类禁用/恢复,取消按钮反之。"""
        entry = self._job_buttons.get(name)
        if not entry:
            return
        state = "disabled" if running else "normal"
        for button in entry.get("starts", []):
            try:
                button.configure(state=state)
            except Exception:  # noqa: BLE001 - 控件已销毁
                pass
        cancel = entry.get("cancel")
        if cancel is not None:
            try:
                cancel.configure(state="normal" if running else "disabled")
                if not running:
                    cancel.configure(text=i18n.tr("取消"))
            except Exception:  # noqa: BLE001
                pass

    def _register_job_buttons(self, name: str, starts: list, cancel=None) -> None:
        self._job_buttons.setdefault(name, {"starts": [], "cancel": None})
        self._job_buttons[name]["starts"].extend(starts)
        if cancel is not None:
            self._job_buttons[name]["cancel"] = cancel

    def _request_cancel(self, name: str) -> None:
        """取消按钮:置位标志;进行中的任务跑完当前文件即停。"""
        flag = self._cancel_flags.get(name)
        if flag is None:
            return
        flag.set()
        entry = self._job_buttons.get(name, {})
        cancel = entry.get("cancel")
        if cancel is not None:
            try:
                cancel.configure(text=i18n.tr("停止中…"), state="disabled")
            except Exception:  # noqa: BLE001
                pass
        self._log("已请求停止，等待进行中的任务结束…")

    def _cancelled(self, name: str) -> bool:
        flag = self._cancel_flags.get(name)
        return flag is not None and flag.is_set()

    def _ensure_output_dir(self, value: str, purpose: str) -> str:
        """Create an output directory before a worker starts, with a GUI error."""
        path = os.path.abspath(value.strip() or self.default_out_dir)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("无法使用输出目录", f"{i18n.tr(purpose)}无法创建输出目录:\n{path}\n\n{exc}")
            self._log(f"✗ {i18n.tr(purpose)}输出目录不可用: {path} -> {exc}")
            return ""
        return path

    def _open_output_dir(self, value: str, purpose: str):
        """Open an output directory in the native file manager."""
        path = self._ensure_output_dir(value, purpose)
        if not path:
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as exc:
            messagebox.showerror("无法打开目录", f"{i18n.tr(purpose)}目录打开失败:\n{path}\n\n{exc}")
            self._log(f"✗ 无法打开{i18n.tr(purpose)}目录: {path} -> {exc}")

    def _open_log_folder(self):
        """打开日志目录, 方便用户回传 app.log。"""
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            if os.name == "nt":
                os.startfile(LOG_DIR)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", LOG_DIR])
            else:
                subprocess.Popen(["xdg-open", LOG_DIR])
        except OSError as exc:
            messagebox.showerror("无法打开日志目录", f"{LOG_DIR}\n\n{exc}")

    def _require_idle(self, name: str, label: str) -> bool:
        if name not in self._running_jobs:
            return True
        messagebox.showinfo("任务进行中", f"{i18n.tr(label)}正在运行。请等待当前批次完成后再修改队列。")
        return False

    def _selected_download_format(self) -> downloader.FormatKind:
        value = i18n.untr(self.fmt_var.get())
        for fmt, label in FORMAT_LABELS.items():
            if value == label or value == fmt.value:
                return fmt
        return downloader.FormatKind.FLAC

    def _progress_update_allowed(self, task) -> bool:
        now = time.monotonic()
        key = id(task)
        with self._progress_lock:
            previous = self._last_progress_update.get(key, 0.0)
            if task.progress < 100.0 and now - previous < 0.15:
                return False
            self._last_progress_update[key] = now
            return True

    def _run_batch(
        self, jobs, run_one, name: str, total_kind: str, refresh_kind: str,
        label_fn=None,
    ) -> None:
        """并行执行一批任务(阻塞到收尾,含取消收尾)。

        状态标记与 end_job 对账由 BatchExecutor 统一负责;run_one 抛出的
        异常会转为该任务的"失败",BatchCancelled 转为"已取消"并停止派新任务。
        """
        executor = BatchExecutor(
            name=name,
            workers=BATCH_WORKERS,
            publish=self.ui_queue.put,
            end_job=self._end_job,
            total_kind=total_kind,
            refresh_kind=refresh_kind,
            logger=log,
        )
        self._executors[name] = executor
        try:
            executor.run(jobs, run_one, label_fn)
        finally:
            self._executors.pop(name, None)

    def _guard_progress(self, name: str, base):
        """包装进度回调:批次被取消时抛 BatchCancelled,穿过解密/转码循环。"""
        def guarded(p: float):
            if self._cancelled(name):
                raise BatchCancelled()
            base(p)
        return guarded

    def _handle_batch_summary(self, name: str, stats: dict) -> None:
        """批次收尾:日志汇总 + (窗口不在前台时)系统通知。"""
        parts = [
            i18n.tr(f"完成 {stats.get('succeeded', 0)}"),
            i18n.tr(f"失败 {stats.get('failed', 0)}"),
        ]
        if stats.get("cancelled"):
            parts.append(i18n.tr(f"已取消 {stats['cancelled']}"))
        summary = i18n.tr("批次结束") + " · " + " · ".join(parts)
        self._log(f"✓ {summary}")
        failed_labels = stats.get("failed_labels") or []
        if failed_labels:
            self._log(i18n.tr("失败文件:"))
            for label in failed_labels[:10]:
                self._log(f"  ✗ {label}")
            if len(failed_labels) > 10:
                self._log(i18n.tr(f"… 其余 {len(failed_labels) - 10} 个见上方日志"))
        self._notify(i18n.tr("✓ 批次完成"), summary)

    def _notify(self, title: str, body: str) -> None:
        """系统通知;没有可用的通知后端时退化为响铃 + 标题栏提示。"""
        try:
            from plyer import notification

            notification.notify(title=title, message=body, app_name=APP_NAME, timeout=6)
            return
        except Exception:  # noqa: BLE001 - plyer 缺失或平台不支持
            pass
        try:
            original = self.title()
            self.bell()
            self.title(f"{title} — {body}")
            self.after(6000, lambda: self._restore_title(original))
        except Exception:  # noqa: BLE001
            pass

    def _restore_title(self, original: str) -> None:
        if not self._closing:
            try:
                self.title(original)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 空队列提示 ----------
    def _add_empty_hint(self, container, key: str, text: str) -> None:
        label = ctk.CTkLabel(
            container, text=i18n.tr(text), anchor="center", justify="center",
            text_color=MUTED, font=ctk.CTkFont(size=13),
        )
        label.pack(fill="both", expand=True, padx=12, pady=24)
        self._empty_hints[key] = label

    def _hide_empty_hint(self, key: str) -> None:
        label = self._empty_hints.get(key)
        if label is not None:
            try:
                label.pack_forget()
            except Exception:  # noqa: BLE001 - 控件已销毁
                pass

    def _show_empty_hint(self, key: str) -> None:
        label = self._empty_hints.get(key)
        if label is not None:
            try:
                label.pack(fill="both", expand=True, padx=12, pady=24)
                label.lift()
            except Exception:  # noqa: BLE001
                pass

    # ---------- UI 构建 ----------
    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkLabel(
            header,
            text=APP_NAME.upper(),
            anchor="w",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            header,
            text="下载 · 转码 · 本地还原",
            anchor="w",
            text_color=MUTED,
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(12, 0), pady=(5, 0))

        # 更新入口: 版本号 + 状态 + 检查更新按钮(更新源固定为本项目仓库)
        self.update_btn = self._button(header, "检查更新", self._check_updates, width=96)
        self.update_btn.pack(side="right")
        self.log_btn = self._button(header, "日志", self._open_log_folder, width=64)
        self.log_btn.pack(side="right", padx=(0, 8))
        self.lang_btn = self._button(header, i18n.toggle_label(), self._toggle_language, width=84)
        self.lang_btn.pack(side="right", padx=(0, 8))
        self.update_status_lbl = ctk.CTkLabel(
            header, text="", anchor="e", text_color=MUTED,
            font=ctk.CTkFont(size=12),
        )
        self.update_status_lbl.pack(side="right", padx=(6, 10), pady=(5, 0))
        ctk.CTkLabel(
            header, text=f"v{APP_VERSION}", anchor="e", text_color=MUTED,
            font=ctk.CTkFont(size=12),
        ).pack(side="right", padx=(0, 4), pady=(5, 0))

        self.tabs = ctk.CTkTabview(
            self, corner_radius=6, fg_color=PANEL,
            segmented_button_fg_color=PANEL,
            segmented_button_selected_color="#334018",
            segmented_button_selected_hover_color="#425321",
            segmented_button_unselected_color=PANEL,
            segmented_button_unselected_hover_color=BUTTON_HOVER,
            text_color=TEXT,
        )
        self.tabs.pack(fill="both", expand=True, padx=10, pady=10)

        tab_dl = self.tabs.add("链接下载")
        tab_cv = self.tabs.add("本地转码")
        tab_ncm = self.tabs.add("解密")
        tab_am = self.tabs.add("Apple/Beatport")
        tab_va = self.tabs.add("视频转音频")

        self._build_download_tab(tab_dl)
        self._build_convert_tab(tab_cv)
        self._build_ncm_tab(tab_ncm)
        self._build_premium_tab(tab_am)
        self._build_video_tab(tab_va)

        # 日志区(全局,所有标签页共用可见)
        log_frame = ctk.CTkFrame(
            self, corner_radius=6, fg_color=PANEL,
            border_width=1, border_color=BORDER,
        )
        log_frame.pack(fill="x", expand=False, padx=10, pady=(0, 10))
        ctk.CTkLabel(log_frame, text="活动日志", anchor="w", text_color=TEXT).pack(fill="x", padx=8, pady=(4, 0))
        self.log_box = ctk.CTkTextbox(
            log_frame, height=110, state="disabled", wrap="word",
            fg_color="#0f0f10", border_width=0, text_color=MUTED,
        )
        self.log_box.pack(fill="x", padx=6, pady=6)

    def _build_download_tab(self, parent):
        self._guide(
            parent,
            "链接下载 · 适用范围与步骤",
            "适用于 yt-dlp 能访问的公开媒体链接,常见为 YouTube、SoundCloud、B 站、Vimeo 和 Bandcamp。"
            "Spotify、网易云、QQ 音乐和 Apple Music 的目录页是否可用取决于站点、地区、登录状态及授权,受 DRM 保护内容不会下载。"
            "每行一个链接 → 可先点“解析第一条”检查 → 选格式和目录 → 点“开始下载”。Apple Music/Beatport 的已授权内容请使用对应专用页。",
        )
        # 输入区
        input_frame = ctk.CTkFrame(
            parent, corner_radius=6, fg_color=PANEL,
            border_width=1, border_color=BORDER,
        )
        input_frame.pack(fill="x", padx=10, pady=(10, 6))

        ctk.CTkLabel(
            input_frame, text="链接（每行一个；也可用空格分隔）", anchor="w"
        ).pack(fill="x", padx=10, pady=(8, 0))
        self.url_textbox = ctk.CTkTextbox(
            input_frame, height=72, wrap="word",
            fg_color="#0f0f10", border_width=0,
        )
        self.url_textbox.pack(fill="x", padx=10, pady=(3, 6))

        row = ctk.CTkFrame(input_frame, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkLabel(row, text="输出格式:").pack(side="left")
        self.fmt_var = ctk.StringVar(value=i18n.tr(FORMAT_LABELS[downloader.FormatKind.FLAC]))
        self.fmt_menu = ctk.CTkOptionMenu(
            row,
            values=[i18n.tr(FORMAT_LABELS[f]) for f in FORMAT_ORDER],
            variable=self.fmt_var,
            width=190,
            command=self._on_fmt_change,
            fg_color=BUTTON,
            button_color=BUTTON,
            button_hover_color=BUTTON_HOVER,
            dropdown_fg_color=PANEL_ALT,
            dropdown_hover_color=BUTTON_HOVER,
            text_color=TEXT,
        )
        self.fmt_menu.pack(side="left", padx=(6, 16))

        ctk.CTkLabel(row, text="保存到:").pack(side="left")
        self.out_dir_var = ctk.StringVar(value=self.default_out_dir)
        self.out_dir_entry = ctk.CTkEntry(
            row, textvariable=self.out_dir_var,
            fg_color="#0f0f10", border_color=BORDER, text_color=TEXT,
        )
        self.out_dir_entry.pack(side="left", fill="x", expand=True, padx=6)
        self._button(row, "浏览…", self._pick_out_dir, width=78).pack(side="left")
        self._button(
            row,
            "打开目录",
            lambda: self._open_output_dir(self.out_dir_var.get(), "下载"),
            width=92,
        ).pack(side="left", padx=(6, 0))

        self.fmt_hint = ctk.CTkLabel(
            input_frame,
            text=self._format_hint(downloader.FormatKind.FLAC),
            anchor="w",
            text_color=MUTED,
        )
        self.fmt_hint.pack(fill="x", padx=10, pady=(0, 6))

        rate_row = ctk.CTkFrame(input_frame, fg_color="transparent")
        rate_row.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(rate_row, text="采样率:").pack(side="left")
        self.dl_rate_var = ctk.StringVar(value="")
        self.dl_rate_label_var = ctk.StringVar(value=CV_SAMPLE_RATE_LABELS[""])
        ctk.CTkOptionMenu(
            rate_row,
            values=[CV_SAMPLE_RATE_LABELS[c] for c in CV_SAMPLE_RATE_CHOICES],
            variable=self.dl_rate_label_var,
            command=self._on_dl_rate_change,
            width=170,
            fg_color=BUTTON, button_color=BUTTON,
            button_hover_color=BUTTON_HOVER, dropdown_fg_color=PANEL_ALT,
            dropdown_hover_color=BUTTON_HOVER, text_color=TEXT,
        ).pack(side="left", padx=6)
        self.dl_rate_hint = ctk.CTkLabel(
            rate_row, text=self._cv_rate_hint(""), anchor="w", text_color=MUTED,
        )
        self.dl_rate_hint.pack(side="left", padx=(12, 0))

        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=6)
        self._button(btn_frame, "解析第一条", self._probe, width=120).pack(side="left")
        dl_start = self._button(btn_frame, "开始下载", self._start_download, primary=True, width=140)
        dl_start.pack(side="left", padx=8)
        dl_retry = self._button(btn_frame, "重试失败", self._retry_failed_downloads, width=100)
        dl_retry.pack(side="left")
        self._button(btn_frame, "清空队列", self._clear_download_list, width=100).pack(side="left")
        dl_cancel = self._button(btn_frame, "取消", lambda: self._request_cancel("download"), width=78)
        dl_cancel.configure(state="disabled")
        dl_cancel.pack(side="right")

        # 任务列表
        list_frame = ctk.CTkFrame(
            parent, corner_radius=6, fg_color=PANEL,
            border_width=1, border_color=BORDER,
        )
        list_frame.pack(fill="both", expand=True, padx=10, pady=6)
        ctk.CTkLabel(list_frame, text="下载队列", anchor="w").pack(fill="x", padx=8, pady=(6, 0))
        self.task_container = ctk.CTkScrollableFrame(
            list_frame, height=180, fg_color="#0f0f10",
        )
        self.task_container.pack(fill="both", expand=True, padx=6, pady=6)
        self._add_empty_hint(self.task_container, "download", "粘贴链接后点「开始下载」")
        self._register_job_buttons("download", starts=[dl_start, dl_retry], cancel=dl_cancel)

    def _build_convert_tab(self, parent):
        self._guide(
            parent,
            "本地转码 · 处理电脑上的音频文件",
            "支持 MP3、WAV、FLAC、M4A、AAC、OGG、OPUS、WMA、AIFF、APE 等常见格式。"
            "步骤:选择目标 FLAC/WAV 和采样率 → 添加一个或多个文件 → 选择输出目录 → 点“开始转码”。"
            "转码只改变容器/编码方式;从有损音频转成 FLAC/WAV 不会提升原始音质。",
        )

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(row, text="目标格式:").pack(side="left")
        self.cv_fmt_var = ctk.StringVar(value="flac")
        ctk.CTkOptionMenu(
            row, values=["flac", "wav"], variable=self.cv_fmt_var, width=120,
            fg_color=BUTTON, button_color=BUTTON,
            button_hover_color=BUTTON_HOVER, dropdown_fg_color=PANEL_ALT,
            dropdown_hover_color=BUTTON_HOVER, text_color=TEXT,
        ).pack(side="left", padx=6)

        ctk.CTkLabel(row, text="采样率:").pack(side="left", padx=(16, 0))
        self.cv_rate_var = ctk.StringVar(value="")
        self.cv_rate_label_var = ctk.StringVar(value=CV_SAMPLE_RATE_LABELS[""])
        self.cv_rate_menu = ctk.CTkOptionMenu(
            row,
            values=[CV_SAMPLE_RATE_LABELS[c] for c in CV_SAMPLE_RATE_CHOICES],
            variable=self.cv_rate_label_var,
            command=self._on_cv_rate_change,
            width=170,
            fg_color=BUTTON, button_color=BUTTON,
            button_hover_color=BUTTON_HOVER, dropdown_fg_color=PANEL_ALT,
            dropdown_hover_color=BUTTON_HOVER, text_color=TEXT,
        )
        self.cv_rate_menu.pack(side="left", padx=6)

        ctk.CTkLabel(row, text="输出目录:").pack(side="left", padx=(16, 0))
        self.cv_out_var = ctk.StringVar(value=self.default_out_dir)
        ctk.CTkEntry(
            row, textvariable=self.cv_out_var,
            fg_color="#0f0f10", border_color=BORDER, text_color=TEXT,
        ).pack(side="left", fill="x", expand=True, padx=6)
        self._button(row, "浏览…", self._pick_cv_out, width=78).pack(side="left")
        self._button(
            row,
            "打开目录",
            lambda: self._open_output_dir(self.cv_out_var.get(), "转码"),
            width=92,
        ).pack(side="left", padx=(6, 0))
        self.cv_rate_hint = ctk.CTkLabel(
            parent, text=self._cv_rate_hint(""), anchor="w",
            text_color=MUTED,
        )
        self.cv_rate_hint.pack(fill="x", padx=16, pady=(0, 2))
        ctk.CTkLabel(
            parent, text="FLAC 适合归档;WAV 文件较大但兼容性广。重采样只统一规格,不会提升音质。",
            anchor="w", text_color=MUTED,
        ).pack(fill="x", padx=16, pady=(0, 4))

        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=6)
        self._button(btn_frame, "添加文件…", self._pick_convert_files, width=140).pack(side="left")
        cv_start = self._button(btn_frame, "开始转码", self._start_convert, primary=True, width=140)
        cv_start.pack(side="left", padx=8)
        cv_retry = self._button(btn_frame, "重试失败", lambda: self._start_convert(retry_only=True), width=100)
        cv_retry.pack(side="left")
        self._button(btn_frame, "清空列表", self._clear_convert_list, width=100).pack(side="left")
        cv_cancel = self._button(btn_frame, "取消", lambda: self._request_cancel("convert"), width=78)
        cv_cancel.configure(state="disabled")
        cv_cancel.pack(side="right")

        list_frame = ctk.CTkFrame(
            parent, corner_radius=6, fg_color=PANEL,
            border_width=1, border_color=BORDER,
        )
        list_frame.pack(fill="both", expand=True, padx=10, pady=6)
        ctk.CTkLabel(list_frame, text="转码队列", anchor="w").pack(fill="x", padx=8, pady=(6, 0))
        self.cv_container = ctk.CTkScrollableFrame(list_frame, fg_color="#0f0f10")
        self.cv_container.pack(fill="both", expand=True, padx=6, pady=6)
        self._add_empty_hint(self.cv_container, "convert", "添加音频文件后开始转码")
        self._register_job_buttons("convert", starts=[cv_start, cv_retry], cancel=cv_cancel)

        self.cv_tasks: list[converter.ConvertTask] = []
        self.cv_rows: list[TaskRow] = []
        total_row = ctk.CTkFrame(parent, fg_color="transparent")
        total_row.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(
            total_row, text=i18n.tr("总进度"), anchor="w", text_color=MUTED,
            font=ctk.CTkFont(size=12),
        ).pack(side="left")
        self.cv_progress_label = ctk.CTkLabel(
            total_row, text="0/0 · 0%", anchor="e", text_color=MUTED,
            font=ctk.CTkFont(size=12),
        )
        self.cv_progress_label.pack(side="right")
        self.cv_progress_bar = ctk.CTkProgressBar(
            parent, height=10, fg_color="#303035", progress_color=ACCENT
        )
        self.cv_progress_bar.pack(fill="x", padx=10, pady=(0, 10))
        self.cv_progress_bar.set(0)
        try:  # 固定滚动步长, 减少拖动滚动条时的大量重绘
            self.cv_container._parent_canvas.configure(yscrollincrement=20)
        except Exception:
            pass

    # ---------- 视频转音频 ----------
    def _build_video_tab(self, parent):
        """从公开视频链接或本地视频文件提取音频。"""
        self._guide(
            parent,
            "视频转音频 · 从链接或本地视频提取音轨",
            "链接支持抖音、TikTok、哔哩哔哩、YouTube 等公开页面；本地文件支持 MP4、MKV、MOV、"
            "AVI、WEBM、FLV 等常见视频容器。步骤：粘贴链接或添加视频文件 → 选输出格式与目录 "
            "→ 点“从链接提取”或“开始转换”。请只处理你拥有或已获授权的内容。",
        )

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(row, text="视频链接（每行一个）:", anchor="w").pack(fill="x")
        self.va_textbox = ctk.CTkTextbox(
            row, height=66, fg_color="#0f0f10", border_color=BORDER,
            border_width=1, text_color=TEXT,
        )
        self.va_textbox.pack(fill="x", pady=(4, 0))

        row2 = ctk.CTkFrame(parent, fg_color="transparent")
        row2.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(row2, text="输出格式:").pack(side="left")
        self.va_fmt_var = ctk.StringVar(value="mp3")
        ctk.CTkOptionMenu(
            row2, values=["mp3", "wav", "flac"], variable=self.va_fmt_var, width=110,
            fg_color=BUTTON, button_color=BUTTON, button_hover_color=BUTTON_HOVER,
            dropdown_fg_color=PANEL_ALT, dropdown_hover_color=BUTTON_HOVER,
            text_color=TEXT,
        ).pack(side="left", padx=6)
        ctk.CTkLabel(row2, text="采样率:").pack(side="left", padx=(16, 0))
        self.va_rate_var = ctk.StringVar(value="")
        self.va_rate_label_var = ctk.StringVar(value=CV_SAMPLE_RATE_LABELS[""])
        ctk.CTkOptionMenu(
            row2,
            values=[CV_SAMPLE_RATE_LABELS[c] for c in CV_SAMPLE_RATE_CHOICES],
            variable=self.va_rate_label_var,
            command=self._on_va_rate_change,
            width=170,
            fg_color=BUTTON, button_color=BUTTON, button_hover_color=BUTTON_HOVER,
            dropdown_fg_color=PANEL_ALT, dropdown_hover_color=BUTTON_HOVER,
            text_color=TEXT,
        ).pack(side="left", padx=6)
        ctk.CTkLabel(row2, text="输出目录:").pack(side="left", padx=(16, 0))
        self.va_out_var = ctk.StringVar(value=self.default_out_dir)
        ctk.CTkEntry(
            row2, textvariable=self.va_out_var,
            fg_color="#0f0f10", border_color=BORDER, text_color=TEXT,
        ).pack(side="left", fill="x", expand=True, padx=6)
        self._button(row2, "浏览…", self._pick_va_out, width=78).pack(side="left")
        self._button(
            row2, "打开目录",
            lambda: self._open_output_dir(self.va_out_var.get(), "视频转音频"),
            width=92,
        ).pack(side="left", padx=(6, 0))

        self.va_rate_hint = ctk.CTkLabel(
            parent, text=self._cv_rate_hint(""), anchor="w",
            text_color=MUTED,
        )
        self.va_rate_hint.pack(fill="x", padx=16, pady=(0, 4))

        btn = ctk.CTkFrame(parent, fg_color="transparent")
        btn.pack(fill="x", padx=10, pady=6)
        va_link_btn = self._button(
            btn, "从链接提取", self._start_va_links, primary=True, width=130
        )
        va_link_btn.pack(side="left")
        self._button(btn, "添加视频文件…", self._pick_va_files, width=150).pack(
            side="left", padx=8
        )
        va_start = self._button(btn, "开始转换", self._start_va_files, width=110)
        va_start.pack(side="left")
        va_retry = self._button(
            btn, "重试失败", lambda: self._start_va_files(retry_only=True), width=100
        )
        va_retry.pack(side="left", padx=8)
        self._button(btn, "清空列表", self._clear_va_list, width=100).pack(side="left")
        va_cancel = self._button(btn, "取消", lambda: self._request_cancel("video"), width=78)
        va_cancel.configure(state="disabled")
        va_cancel.pack(side="right")

        list_frame = ctk.CTkFrame(
            parent, corner_radius=6, fg_color=PANEL,
            border_width=1, border_color=BORDER,
        )
        list_frame.pack(fill="both", expand=True, padx=10, pady=6)
        head = ctk.CTkFrame(list_frame, fg_color="transparent")
        head.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkLabel(head, text="任务队列", anchor="w").pack(side="left")
        self.va_drop_hint = ctk.CTkLabel(
            head, text="· 可直接拖入视频文件", anchor="w", text_color=MUTED,
            font=ctk.CTkFont(size=12),
        )
        self.va_drop_hint.pack(side="left", padx=(8, 0))
        self.va_container = ctk.CTkScrollableFrame(list_frame, fg_color="#0f0f10")
        self.va_container.pack(fill="both", expand=True, padx=6, pady=6)

        self.va_tasks: list = []
        self.va_rows: list[TaskRow] = []
        self.va_row_map: dict[int, TaskRow] = {}
        self._add_empty_hint(self.va_container, "video", "拖入视频文件或点「添加视频文件…」")
        self._register_job_buttons(
            "video", starts=[va_link_btn, va_start, va_retry], cancel=va_cancel
        )
        total_row = ctk.CTkFrame(parent, fg_color="transparent")
        total_row.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(
            total_row, text=i18n.tr("总进度"), anchor="w", text_color=MUTED,
            font=ctk.CTkFont(size=12),
        ).pack(side="left")
        self.va_progress_label = ctk.CTkLabel(
            total_row, text="0/0 · 0%", anchor="e", text_color=MUTED,
            font=ctk.CTkFont(size=12),
        )
        self.va_progress_label.pack(side="right")
        self.va_progress_bar = ctk.CTkProgressBar(
            parent, height=10, fg_color="#303035", progress_color=ACCENT
        )
        self.va_progress_bar.pack(fill="x", padx=10, pady=(0, 10))
        self.va_progress_bar.set(0)
        try:
            self.va_container._parent_canvas.configure(yscrollincrement=20)
        except Exception:
            pass

        if dragdrop.enable_drop(
            self.va_container,
            self._on_va_drop,
            exts=set(converter.SUPPORTED_INPUT_EXT),
        ):
            self.va_drop_enabled = True
        else:
            self.va_drop_enabled = False
            self.va_drop_hint.configure(text="· 请用“添加视频文件”按钮选择")

    def _on_va_rate_change(self, label: str):
        """视频页采样率下拉框:显示文案可能已被翻译,先用 untr 还原。"""
        text = i18n.untr(label)
        for value, zh_label in CV_SAMPLE_RATE_LABELS.items():
            if zh_label == text:
                self.va_rate_var.set(value)
                if hasattr(self, "va_rate_hint"):
                    self.va_rate_hint.configure(text=self._cv_rate_hint(value))
                return

    def _va_sample_rate(self) -> Optional[int]:
        return converter.normalize_sample_rate(self.va_rate_var.get())

    def _pick_va_out(self):
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.va_out_var.set(path)

    def _pick_va_files(self):
        files = filedialog.askopenfilenames(
            title="选择视频或音频文件",
            filetypes=[
                ("视频 / 音频", "*.mp4 *.mkv *.mov *.avi *.webm *.flv *.m4v *.wmv *.ts *.mpg *.mpeg *.3gp"),
                ("所有文件", "*.*"),
            ],
        )
        if files:
            self._va_add_files(files)

    def _on_va_drop(self, paths: list):
        files = [p for p in paths if os.path.isfile(p)]
        if not files:
            self._log("拖入的内容里没有可转换的文件")
            return
        self._va_add_files(files)

    def _va_add_files(self, paths) -> int:
        self._hide_empty_hint("video")
        added = 0
        # 同批次内不同来源可能产出同名文件(如 A/song.mp4 与 B/song.mp4),
        # 必须用 reserved 集合去重,否则后一个任务会静默覆盖前一个的结果。
        reserved: set[str] = {
            os.path.normcase(os.path.abspath(getattr(t, "dst", "")))
            for t in self.va_tasks
            if getattr(t, "dst", "")
        }
        for raw in paths:
            f = os.path.abspath(os.fspath(raw))
            if not converter.is_supported(f):
                messagebox.showwarning("提示", f"不支持该文件类型: {f}")
                continue
            if any(getattr(t, "src", "") == f for t in self.va_tasks):
                continue
            fmt = self.va_fmt_var.get()
            out_dir = self.va_out_var.get().strip() or self.default_out_dir
            dst = converter.build_output_path(f, out_dir, fmt, reserved)
            task = converter.ConvertTask(src=f, dst=dst, fmt=fmt)
            self.va_tasks.append(task)
            row = TaskRow(self.va_container, task)
            row.title_lbl.configure(text=os.path.basename(f), width=400)
            row.status_lbl.configure(text="排队中")
            self.va_rows.append(row)
            self.va_row_map[id(task)] = row
            added += 1
        if added:
            self._log(f"已添加 {added} 个待转换文件")
            if getattr(self, "va_drop_enabled", False):
                dragdrop.enable_drop(
                    self.va_container,
                    self._on_va_drop,
                    exts=set(converter.SUPPORTED_INPUT_EXT),
                )
        return added

    def _start_va_links(self):
        raw = self.va_textbox.get("1.0", "end").strip()
        urls = [u.strip() for u in raw.splitlines() if u.strip()]
        urls = [u for u in urls if downloader.is_valid_url(u)]
        if not urls:
            messagebox.showwarning("提示", "请先粘贴至少一个有效的视频链接")
            return
        if not self._begin_job("video", len(urls)):
            return
        out_dir = self._ensure_output_dir(self.va_out_var.get(), "视频转音频")
        if not out_dir:
            self._running_jobs.discard("video")
            self._job_remaining.pop("video", None)
            return
        self._hide_empty_hint("video")
        kind = {
            "mp3": downloader.FormatKind.MP3,
            "wav": downloader.FormatKind.WAV,
            "flac": downloader.FormatKind.FLAC,
        }.get(self.va_fmt_var.get(), downloader.FormatKind.MP3)
        tasks = []
        for url in urls:
            task = downloader.DownloadTask(url=url, out_dir=out_dir, fmt=kind, title=url)
            self.va_tasks.append(task)
            row = TaskRow(self.va_container, task)
            row.title_lbl.configure(text=url, width=400)
            self.va_rows.append(row)
            self.va_row_map[id(task)] = row
            tasks.append(task)
        self.va_textbox.delete("1.0", "end")
        self.va_progress_bar.set(0)
        self.va_progress_label.configure(text="0%")
        threading.Thread(target=self._va_worker, args=(tasks,), daemon=True).start()

    def _start_va_files(self, retry_only: bool = False):
        if retry_only:
            pending = [t for t in self.va_tasks if t.status == "失败"]
            if not pending:
                messagebox.showinfo("没有失败的任务", "当前队列里没有失败的任务。")
                return
        else:
            pending = [
                t for t in self.va_tasks
                if isinstance(t, converter.ConvertTask) and t.status != "完成"
            ]
            if not pending:
                messagebox.showwarning("提示", "请先添加要转换的视频文件")
                return
        if not self._begin_job("video", len(pending)):
            return
        self.va_progress_bar.set(0)
        self.va_progress_label.configure(text="0%")
        threading.Thread(target=self._va_worker, args=(pending,), daemon=True).start()

    def _va_worker(self, tasks: list):
        # 批次开始时锁定采样率,避免中途改动下拉框影响同一批任务。
        sample_rate = self._va_sample_rate()
        rate_desc = self._rate_desc(sample_rate)
        self.ui_queue.put(("log", f"提取批次开始 · {len(tasks)} 个任务 · {rate_desc}"))

        def run_one(task):
            task.status = "处理中…"
            self.ui_queue.put(("va_refresh", task))
            if isinstance(task, downloader.DownloadTask):
                def on_dl_progress(p, speed, eta):
                    task.update_progress(p, speed, eta)
                    if self._progress_update_allowed(task):
                        self.ui_queue.put(("va_progress", task))

                done_path = downloader.download_url(
                    task.url, task.out_dir, task.fmt,
                    on_progress=on_dl_progress,
                    ffmpeg_location=self.ffmpeg,
                )
                task.file_path = done_path
            else:
                def on_cv_progress(p):
                    task.set_progress(p)
                    if self._progress_update_allowed(task):
                        self.ui_queue.put(("va_progress", task))

                done_path = converter.convert_file(
                    task.src, task.dst, task.fmt,
                    on_progress=self._guard_progress("video", on_cv_progress),
                    ffmpeg=self.ffmpeg,
                    sample_rate=sample_rate,
                )
                task.dst = done_path
            task.set_progress(100.0)
            self.ui_queue.put(("va_refresh", task))
            self.ui_queue.put(("log", f"✓ 已提取音频: {os.path.basename(done_path)}"))

        self._run_batch(
            tasks, run_one, "video", "va_total", "va_refresh",
            label_fn=lambda t: getattr(t, "src", None) or getattr(t, "url", ""),
        )

    def _clear_va_list(self):
        if not self._require_idle("video", "视频转音频队列"):
            return
        self.va_tasks.clear()
        for row in self.va_rows:
            row.frame.destroy()
        self.va_rows.clear()
        self.va_row_map.clear()
        self._show_empty_hint("video")
        self.va_progress_bar.set(0)
        self.va_progress_label.configure(text="0%")

    # ---------- NCM 解密逻辑 ----------
    def _build_ncm_tab(self, parent):
        self._guide(
            parent,
            "解密 · 网易云 NCM 与 QQ 音乐加密文件",
            "支持网易云音乐下载得到的 .ncm 与 QQ 音乐加密文件(.qmc0/.qmc3/.qmcflac/.qmcogg,以及 .mflac/.mgg 等)。"
            "步骤:选择输出目录 → 添加一个或多个加密文件 → 点“开始解密”。程序会在本地还原音频;NCM 会尽力写入歌曲名、歌手、专辑和封面,QQ 音乐文件自带标签会保留。"
            "不会上传文件;请只处理你拥有或明确获授权的内容。"
            "若文件使用 QQ 音乐 musicex 新版权保护(2024 起部署),密钥由 QQ 音乐服务器在登录态动态下发,本工具作为离线解密器无法处理,请改用 QQ 音乐客户端播放。",
        )

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(row, text="输出目录:").pack(side="left")
        self.ncm_out_var = ctk.StringVar(value=self.default_out_dir)
        ctk.CTkEntry(
            row, textvariable=self.ncm_out_var,
            fg_color="#0f0f10", border_color=BORDER, text_color=TEXT,
        ).pack(side="left", fill="x", expand=True, padx=6)
        self._button(row, "浏览…", self._pick_ncm_out, width=78).pack(side="left")

        fmt_row = ctk.CTkFrame(parent, fg_color="transparent")
        fmt_row.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(fmt_row, text="输出格式:").pack(side="left")
        decrypt_labels = [i18n.tr(label) for label in DECRYPT_FORMAT_LABELS]
        # 下拉框显示译文，取值必须用原文标签映射。
        self.decrypt_fmt_var = ctk.StringVar(value="original")
        self.decrypt_fmt_label_var = ctk.StringVar(value=decrypt_labels[0])
        ctk.CTkOptionMenu(
            fmt_row, width=300,
            values=decrypt_labels,
            variable=self.decrypt_fmt_label_var,
            command=self._on_decrypt_fmt_change,
            fg_color=BUTTON, button_color=BUTTON,
            button_hover_color=BUTTON_HOVER, dropdown_fg_color=PANEL_ALT,
            dropdown_hover_color=BUTTON_HOVER, text_color=TEXT,
        ).pack(side="left", padx=6)
        self._button(
            row,
            "打开目录",
            lambda: self._open_output_dir(self.ncm_out_var.get(), "解密"),
            width=92,
        ).pack(side="left", padx=(6, 0))

        rate_row = ctk.CTkFrame(parent, fg_color="transparent")
        rate_row.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(rate_row, text="采样率:").pack(side="left")
        self.ncm_rate_var = ctk.StringVar(value="")
        self.ncm_rate_label_var = ctk.StringVar(value=CV_SAMPLE_RATE_LABELS[""])
        ctk.CTkOptionMenu(
            rate_row, width=170,
            values=[CV_SAMPLE_RATE_LABELS[c] for c in CV_SAMPLE_RATE_CHOICES],
            variable=self.ncm_rate_label_var,
            command=self._on_ncm_rate_change,
            fg_color=BUTTON, button_color=BUTTON,
            button_hover_color=BUTTON_HOVER, dropdown_fg_color=PANEL_ALT,
            dropdown_hover_color=BUTTON_HOVER, text_color=TEXT,
        ).pack(side="left", padx=6)
        self.ncm_rate_hint = ctk.CTkLabel(
            rate_row, text=self._cv_rate_hint(""), anchor="w", text_color=MUTED,
        )
        self.ncm_rate_hint.pack(side="left", padx=(12, 0))

        exists_row = ctk.CTkFrame(parent, fg_color="transparent")
        exists_row.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(exists_row, text="同名文件:").pack(side="left")
        self.decrypt_exists_var = ctk.StringVar(value="rename")
        self.decrypt_exists_label_var = ctk.StringVar(
            value=i18n.tr("自动改名（不覆盖）")
        )
        ctk.CTkOptionMenu(
            exists_row, width=190,
            values=[i18n.tr(label) for label in DECRYPT_EXISTS_LABELS],
            variable=self.decrypt_exists_label_var,
            command=self._on_exists_change,
            fg_color=BUTTON, button_color=BUTTON,
            button_hover_color=BUTTON_HOVER, dropdown_fg_color=PANEL_ALT,
            dropdown_hover_color=BUTTON_HOVER, text_color=TEXT,
        ).pack(side="left", padx=6)
        ctk.CTkLabel(
            exists_row,
            text="· 跳过可避免重复生成 _1/_2",
            anchor="w", text_color=MUTED, font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(10, 0))

        opt_row = ctk.CTkFrame(parent, fg_color="transparent")
        opt_row.pack(fill="x", padx=10, pady=(0, 4))
        self.rename_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(
            opt_row,
            text="按“歌手 - 歌名”重命名输出文件",
            variable=self.rename_var,
            command=self._on_rename_toggle,
            font=ctk.CTkFont(size=12),
            text_color=TEXT,
            progress_color=ACCENT,
            fg_color=BUTTON,
            button_color=TEXT,
            button_hover_color=TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            opt_row,
            text="· 取不到歌名时维持原文件名",
            anchor="w", text_color=MUTED, font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(10, 0))

        fp_row = ctk.CTkFrame(parent, fg_color="transparent")
        fp_row.pack(fill="x", padx=10, pady=(0, 4))
        self.fp_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            fp_row,
            text="标签缺失时用声学指纹联网识别",
            variable=self.fp_var,
            command=self._on_fingerprint_toggle,
            font=ctk.CTkFont(size=12),
            text_color=TEXT,
            progress_color=ACCENT,
            fg_color=BUTTON,
            button_color=TEXT,
            button_hover_color=TEXT,
        ).pack(side="left")
        self.fp_status = ctk.CTkLabel(
            fp_row, text=self._fingerprint_status_text(), anchor="w",
            text_color=MUTED, font=ctk.CTkFont(size=12),
        )
        self.fp_status.pack(side="left", padx=(10, 0))

        key_row = ctk.CTkFrame(parent, fg_color="transparent")
        key_row.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(key_row, text="AcoustID Key:").pack(side="left")
        self.fp_key_var = ctk.StringVar(value="")
        ctk.CTkEntry(
            key_row, textvariable=self.fp_key_var, width=300, show="*",
            placeholder_text="在 acoustid.org 用 MusicBrainz 账号登录获取",
            fg_color="#0f0f10", border_color=BORDER, text_color=TEXT,
        ).pack(side="left", padx=6)
        # Key 填完即生效,不需要额外点保存。
        self.fp_key_var.trace_add(
            "write",
            lambda *_: setattr(self, "fingerprint_key", self.fp_key_var.get().strip()),
        )

        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=6)
        self._button(btn_frame, "添加加密文件…", self._pick_ncm_files, width=150).pack(side="left")
        ncm_start = self._button(btn_frame, "开始解密", self._start_ncm_decrypt, primary=True, width=140)
        ncm_start.pack(side="left", padx=8)
        ncm_retry = self._button(btn_frame, "重试失败", lambda: self._start_ncm_decrypt(retry_only=True), width=100)
        ncm_retry.pack(side="left")
        self._button(btn_frame, "清空列表", self._clear_ncm_list, width=100).pack(side="left")
        ncm_cancel = self._button(btn_frame, "取消", lambda: self._request_cancel("ncm"), width=78)
        ncm_cancel.configure(state="disabled")
        ncm_cancel.pack(side="right")

        list_frame = ctk.CTkFrame(
            parent, corner_radius=6, fg_color=PANEL,
            border_width=1, border_color=BORDER,
        )
        list_frame.pack(fill="both", expand=True, padx=10, pady=6)
        queue_head = ctk.CTkFrame(list_frame, fg_color="transparent")
        queue_head.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkLabel(queue_head, text="解密队列", anchor="w").pack(side="left")
        self.ncm_drop_hint = ctk.CTkLabel(
            queue_head, text="· 可直接拖入文件或文件夹", anchor="w",
            text_color=MUTED, font=ctk.CTkFont(size=12),
        )
        self.ncm_drop_hint.pack(side="left", padx=(8, 0))
        self.ncm_container = ctk.CTkScrollableFrame(list_frame, fg_color="#0f0f10")
        self.ncm_container.pack(fill="both", expand=True, padx=6, pady=6)

        # 拖入即入队；拖放库不可用时自动降级为“仅按钮添加”。
        if dragdrop.enable_drop(
            self.ncm_container,
            self._on_decrypt_drop,
            exts={".ncm"} | set(qmc_decrypt.QMC_EXTS),
        ):
            self.ncm_drop_enabled = True
        else:
            self.ncm_drop_enabled = False
            self.ncm_drop_hint.configure(text="· 请用“添加加密文件”按钮选择")

        # 解密队列混合 NcmTask 与 QmcTask(两者接口一致)
        self.ncm_tasks: list = []
        self.ncm_rows: list[TaskRow] = []
        self._add_empty_hint(self.ncm_container, "ncm", "拖入加密文件或文件夹，或点「添加加密文件…」")
        self._register_job_buttons("ncm", starts=[ncm_start, ncm_retry], cancel=ncm_cancel)
        total_row = ctk.CTkFrame(parent, fg_color="transparent")
        total_row.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(
            total_row, text=i18n.tr("总进度"), anchor="w", text_color=MUTED,
            font=ctk.CTkFont(size=12),
        ).pack(side="left")
        self.ncm_progress_label = ctk.CTkLabel(
            total_row, text="0/0 · 0%", anchor="e", text_color=MUTED,
            font=ctk.CTkFont(size=12),
        )
        self.ncm_progress_label.pack(side="right")
        self.ncm_progress_bar = ctk.CTkProgressBar(
            parent, height=10, fg_color="#303035", progress_color=ACCENT
        )
        self.ncm_progress_bar.pack(fill="x", padx=10, pady=(0, 10))
        self.ncm_progress_bar.set(0)
        try:  # 固定滚动步长, 减少拖动滚动条时的大量重绘
            self.ncm_container._parent_canvas.configure(yscrollincrement=20)
        except Exception:
            pass

    def _pick_ncm_out(self):
        if not self._require_idle("ncm", "解密"):
            return
        d = filedialog.askdirectory(initialdir=self.ncm_out_var.get() or os.path.expanduser("~"))
        if d:
            self.ncm_out_var.set(d)

    def _pick_ncm_files(self):
        if not self._require_idle("ncm", "解密队列"):
            return
        qmc_patterns = " ".join(qmc_decrypt.QMC_PATTERNS)
        files = filedialog.askopenfilenames(
            title="选择加密音乐文件",
            filetypes=[
                ("加密音乐文件", f"*.ncm {qmc_patterns}"),
                ("网易云音乐 NCM", "*.ncm"),
                ("QQ 音乐 QMC", qmc_patterns),
                ("所有文件", "*.*"),
            ],
        )
        self._enqueue_decrypt_files(files)

    def _on_decrypt_drop(self, paths: list):
        """拖放回调:paths 已按扩展名过滤并展开过目录(可能为空)。"""
        if not paths:
            self._log("拖入的内容里没有可解密的文件")
            return
        if not self._require_idle("ncm", "解密队列"):
            return
        self._enqueue_decrypt_files(paths)

    def _on_decrypt_fmt_change(self, label: str):
        self.decrypt_fmt_var.set(DECRYPT_FORMAT_LABELS.get(i18n.untr(label), "original"))

    def _on_exists_change(self, label: str):
        """同名文件处理策略。"""
        self.decrypt_exists_var.set(
            DECRYPT_EXISTS_LABELS.get(i18n.untr(label), "rename")
        )

    def _on_ncm_rate_change(self, label: str):
        """解密页的采样率下拉框。"""
        text = i18n.untr(label)
        for value, zh_label in CV_SAMPLE_RATE_LABELS.items():
            if zh_label == text:
                self.ncm_rate_var.set(value)
                if hasattr(self, "ncm_rate_hint"):
                    self.ncm_rate_hint.configure(text=self._cv_rate_hint(value))
                return

    def _on_rename_toggle(self):
        """解密产物是否按"歌手 - 歌名"命名。"""
        self.rename_by_meta = bool(self.rename_var.get())
        self._log(
            "输出命名: 按“歌手 - 歌名”"
            if self.rename_by_meta
            else "输出命名: 沿用源文件名"
        )

    def _fingerprint_status_text(self) -> str:
        """指纹识别的就绪状态:fpcalc 在不在、Key 有没有。"""
        if not fingerprint.find_fpcalc():
            return "· 未找到 fpcalc(需下载到 bin 目录或 PATH)"
        if not self.fingerprint_key:
            return "· 已就绪,但尚未填写 AcoustID Key"
        return "· 已就绪"

    def _on_fingerprint_toggle(self):
        self.fingerprint_enabled = bool(self.fp_var.get())
        self.fingerprint_key = self.fp_key_var.get().strip()
        if hasattr(self, "fp_status"):
            self.fp_status.configure(text=self._fingerprint_status_text())
        if self.fingerprint_enabled:
            missing = []
            if not fingerprint.find_fpcalc():
                missing.append("fpcalc")
            if not self.fingerprint_key:
                missing.append("AcoustID Key")
            if missing:
                # 这里不用模态弹窗:开关是可反复切换的,弹窗会打断操作,
                # 而且开关显示为"开"但功能不可用,容易误解。改为日志 + 状态栏提示。
                self._log("⚠ 指纹识别缺少前置条件,本次会自动跳过(不影响解密):"
                          + "、".join(missing))
                self._log("  fpcalc: https://github.com/acoustid/chromaprint/releases")
                self._log("  AcoustID Key: https://acoustid.org/")
            else:
                self._log("已开启指纹识别:标签缺失时会联网查询 AcoustID")
        else:
            self._log("已关闭指纹识别")

    def _ncm_sample_rate(self) -> Optional[int]:
        return converter.normalize_sample_rate(self.ncm_rate_var.get())

    def _decrypt_output_path(self, task, out_dir: str) -> str:
        """本轮解密该任务的目标路径(扩展名由加密格式推定)。"""
        if isinstance(task, ncm_decrypt.NcmTask):
            return ncm_decrypt.make_output_path(task.src, out_dir)
        return qmc_decrypt.make_output_path(task.src, out_dir)

    def _enqueue_decrypt_files(self, paths) -> int:
        """把一批路径加入解密队列(按钮选择与拖放共用),返回新增数量。"""
        added = 0
        pending: list = []
        for raw in paths:
            f = os.path.abspath(os.fspath(raw))
            ext = os.path.splitext(f)[1].lower()
            if ext == ".ncm":
                task = ncm_decrypt.NcmTask(src=f, dst="")
            elif ext in qmc_decrypt.QMC_EXTS:
                task = qmc_decrypt.QmcTask(src=f, dst="")
            else:
                messagebox.showwarning("提示", f"不支持该文件类型: {f}")
                continue
            if any(t.src == f for t in self.ncm_tasks):
                continue
            task.dst = self._decrypt_output_path(task, self.ncm_out_var.get())
            self.ncm_tasks.append(task)
            pending.append(task)
            added += 1
        if added:
            # 几百个文件时一次性建行会卡住主线程, 改成分批创建。
            self._create_ncm_rows_in_batches(pending)
            self._log(f"已添加 {added} 个加密文件")
        return added

    def _clear_ncm_list(self):
        if not self._require_idle("ncm", "解密队列"):
            return
        self.ncm_tasks.clear()
        for row in self.ncm_rows:
            row.frame.destroy()
        self.ncm_rows.clear()
        self.ncm_row_map.clear()
        self._show_empty_hint("ncm")
        self.ncm_progress_bar.set(0)
        self.ncm_progress_label.configure(text="0%")

    def _start_ncm_decrypt(self, retry_only: bool = False):
        if not self.ncm_tasks:
            messagebox.showwarning("提示", "请先添加要解密的文件")
            return
        if retry_only:
            pending = [task for task in self.ncm_tasks if task.status == "失败"]
            if not pending:
                messagebox.showinfo("没有失败的任务", "当前解密队列里没有失败的任务。")
                return
        else:
            pending = [task for task in self.ncm_tasks if task.status != "完成"]
            if not pending:
                messagebox.showinfo("没有待处理任务", "当前解密队列中的文件都已完成。若要重新解密,请清空队列后重新添加。")
                return
        target_fmt = self.decrypt_fmt_var.get() or "original"
        if target_fmt not in DECRYPT_TARGET_FORMATS:
            target_fmt = "original"
        sample_rate = self._ncm_sample_rate()
        if (target_fmt != "original" or sample_rate) and not self.ffmpeg:
            messagebox.showerror("错误", "未找到 ffmpeg,无法转码。请先安装 ffmpeg 并加入 PATH。")
            return
        if not self._begin_job("ncm", len(pending)):
            return
        self.ncm_progress_bar.set(0)
        out_dir = self._ensure_output_dir(self.ncm_out_var.get(), "解密")
        if not out_dir:
            self._running_jobs.discard("ncm")
            self._job_remaining.pop("ncm", None)
            return
        policy = self.decrypt_exists_var.get() or "rename"
        try:
            reserved: set[str] = set()
            ready: list = []
            skipped = 0
            for task in pending:
                task.error = ""
                task.status = "排队中"
                task.set_progress(0.0)
                avoid = policy == "rename"
                if isinstance(task, ncm_decrypt.NcmTask):
                    task.dst = ncm_decrypt.make_output_path(
                        task.src, out_dir, reserved, avoid_existing=avoid
                    )
                else:
                    task.dst = qmc_decrypt.make_output_path(
                        task.src, out_dir, reserved, avoid_existing=avoid
                    )
                if policy == "skip" and self._decrypt_output_exists(task, out_dir):
                    task.status = "已跳过"
                    task.set_progress(100.0)
                    self.ui_queue.put(("ncm_refresh", task))
                    skipped += 1
                    continue
                ready.append(task)
            if skipped:
                self.ui_queue.put(("log", f"已跳过 {skipped} 个已存在的文件"))
            if not ready:
                self._running_jobs.discard("ncm")
                self._job_remaining.pop("ncm", None)
                self._log("没有需要解密的文件(全部已存在)")
                return
            self._job_remaining["ncm"] = len(ready)
            threading.Thread(
                target=self._ncm_worker,
                args=(ready, target_fmt, sample_rate, policy),
                daemon=True,
            ).start()
        except Exception:
            self._running_jobs.discard("ncm")
            self._job_remaining.pop("ncm", None)
            raise

    def _decrypt_output_exists(self, task, out_dir: str) -> bool:
        """“跳过已存在”判断：能预读元数据时按最终文件名，否则按源文件名。"""
        if isinstance(task, ncm_decrypt.NcmTask):
            try:
                meta = ncm_decrypt.peek_meta(task.src)
                stem = qmc_decrypt.track_filename(meta.artist, meta.music_name)
                if stem:
                    # original 模式下内嵌 MP3 的成品是 .mp3,其余是 .flac;
                    # 两种扩展名都查一遍,避免同名不同容器时误判。
                    for ext in (".flac", ".mp3"):
                        if os.path.exists(os.path.join(out_dir, stem + ext)):
                            return True
            except Exception:  # noqa: BLE001 - 预读失败就退回 task.dst
                pass
        return os.path.exists(task.dst)

    def _transcode_decrypted(self, raw: str, fmt: str, on_progress, sample_rate=None, metadata=None, cover=None) -> str:
        """把解密结果落到目标格式; fmt="original" 时只按需重采样,绝不转码。"""
        return converter.finalize_decrypted(
            raw,
            fmt,
            sample_rate=sample_rate,
            ffmpeg=self.ffmpeg,
            on_progress=on_progress,
            metadata=metadata,
            cover=cover,
        )

    def _ncm_worker(self, tasks: list, target_fmt: str = "original", sample_rate=None, policy: str = "rename"):
        # 批次开始时锁定采样率,避免中途改动下拉框影响同一批任务。
        rate_desc = self._rate_desc(sample_rate)
        self.ui_queue.put(("log", f"解密批次开始 · {len(tasks)} 个文件 · {rate_desc}"))

        def run_one(task):
            task.status = "解密中…"
            self.ui_queue.put(("ncm_refresh", task))

            def on_progress(p: float):
                task.set_progress(p)
                if self._progress_update_allowed(task):
                    self.ui_queue.put(("ncm_progress", task))

            on_progress = self._guard_progress("ncm", on_progress)

            try:
                t_start = time.perf_counter()
                # 可移动盘/网络盘：先在本机暂存目录完成全部处理，最后只复制成品，
                # 避免把中间文件反复写到慢盘上把整机 I/O 打满。
                final_dst = task.dst
                use_staging = converter.is_removable_path(final_dst)
                work_dst = (
                    converter.staging_path(os.path.basename(final_dst))
                    if use_staging
                    else final_dst
                )
                # 需要重采样时,解密阶段只占进度条前 50%。
                turning = target_fmt != "original" or bool(sample_rate)
                step = (lambda p: on_progress(p * 0.5)) if turning else on_progress
                # 原格式 + 重采样时,把标签/封面推迟到 ffmpeg 的那一次写入,
                # 少跑一遍整文件重写(USB/机械盘上这一遍很贵)。
                defer_meta = (
                    isinstance(task, ncm_decrypt.NcmTask)
                    and target_fmt == "original"
                    and bool(sample_rate)
                )
                # 重命名统一在批次末尾由 _rename_decrypted 加锁完成;
                # 并行解密时若让 decrypt_file 各自改名,同名曲目会互相覆盖。
                if isinstance(task, ncm_decrypt.NcmTask):
                    raw, meta = ncm_decrypt.decrypt_file(
                        task.src, work_dst, on_progress=step, ffmpeg=self.ffmpeg,
                        on_line=lambda line: self.ui_queue.put(("log", line)),
                        rename_by_meta=False,
                        write_meta=not defer_meta,
                        # original 模式:内嵌 MP3 保留 MP3,不强制转 FLAC。
                        output_fmt="original" if target_fmt == "original" else "flac",
                    )
                else:
                    raw, meta = qmc_decrypt.decrypt_file(
                        task.src, work_dst, on_progress=step,
                        rename_by_meta=False,
                    )
                t_decrypted = time.perf_counter()
                dst = raw
                if turning:
                    task.status = "转码中…"
                    self.ui_queue.put(("ncm_refresh", task))
                    metadata = None
                    cover = None
                    identifier = ""
                    if defer_meta:
                        if meta.music_name:
                            identifier = f"{meta.music_name} - {meta.artist}".strip(" -")
                        metadata = ncm_decrypt.meta_tags(meta, identifier)
                        cover = getattr(meta, "cover", b"") or None
                    try:
                        dst = self._transcode_decrypted(
                            raw, target_fmt, on_progress, sample_rate=sample_rate,
                            metadata=metadata, cover=cover,
                        )
                    except Exception:
                        if defer_meta:
                            # 转码失败也要把已经解密出来的 FLAC 补上标签。
                            try:
                                ncm_decrypt.write_flac_meta(
                                    raw, meta, getattr(meta, "cover", b""), identifier
                                )
                            except Exception:  # noqa: BLE001 - 补标签失败不影响抛错
                                pass
                        raise
                t_transcoded = time.perf_counter()
                # 解密产物缺少标签时,尝试用声学指纹联网补全(可关闭)。
                if self.fingerprint_enabled and not (
                    getattr(meta, "music_name", "") and getattr(meta, "artist", "")
                ):
                    task.status = "识别中…"
                    self.ui_queue.put(("ncm_refresh", task))
                    dst = self._identify_and_rewrite(task, dst, on_progress)
                if self.rename_by_meta:
                    # 加锁保证"查重 + 改名"原子:并行批次下两个同名曲目
                    # 会同时通过 exists 检查然后互相覆盖。
                    with self._rename_lock:
                        dst = self._rename_decrypted(dst, meta)
                if use_staging:
                    task.status = "写入目标盘…"
                    self.ui_queue.put(("ncm_refresh", task))
                    t_copy = time.perf_counter()
                    dst = converter.place_file(
                        dst, os.path.dirname(final_dst),
                        overwrite=(policy == "overwrite"),
                    )
                    log.info(
                        "copy to target %s in %.2fs",
                        dst, time.perf_counter() - t_copy,
                    )
                task.dst = dst
                task.set_progress(100.0)
                self.ui_queue.put(("ncm_refresh", task))
                name = getattr(meta, "music_name", "") or os.path.basename(dst)
                self.ui_queue.put(("log", f"✓ 解密完成: {name} -> {dst}"))
                log.info(
                    "timing %s decrypt=%.2fs transcode=%.2fs total=%.2fs staging=%s",
                    os.path.basename(task.src),
                    t_decrypted - t_start,
                    t_transcoded - t_decrypted,
                    time.perf_counter() - t_start,
                    use_staging,
                )
            except BatchCancelled:
                self.ui_queue.put(("log", f"· 已取消: {os.path.basename(task.src)}"))
                raise
            except Exception as e:
                log.exception("decrypt failed: %s", task.src)
                self.ui_queue.put(("log", f"✗ 解密失败: {task.src} -> {e}"))
                raise

        self._run_batch(
            tasks, run_one, "ncm", "ncm_total", "ncm_refresh",
            label_fn=lambda t: getattr(t, "src", ""),
        )

    def _identify_and_rewrite(self, task, path: str, on_progress) -> str:
        """对缺标签的解密产物做指纹识别,补写标签并按需下载封面。

        任何一步失败都只记录日志,绝不中断解密主流程。
        """
        def log(msg: str):
            self.ui_queue.put(("log", msg))

        try:
            match = fingerprint.identify(
                path,
                self.fingerprint_key,
                on_line=lambda m: log(f"  指纹识别 · {os.path.basename(path)} · {m}"),
            )
        except fingerprint.MissingBackend as e:
            log(f"⚠ 跳过指纹识别: {e}")
            return path
        except fingerprint.MissingApiKey as e:
            log(f"⚠ 跳过指纹识别: {e}")
            return path
        except fingerprint.FingerprintError as e:
            log(f"⚠ 指纹识别失败: {e}")
            return path
        except Exception as e:  # noqa: BLE001 - 识别永远不能影响解密产物
            log(f"⚠ 指纹识别异常: {e}")
            return path

        if not match.found:
            log(f"· 未匹配到歌曲: {os.path.basename(path)}"
                f"(AcoustID 对 QQ 音乐等中文曲库覆盖有限,属正常)")
            return path

        log(f"✓ 已识别: {match.artist} - {match.title}"
            + (f" · 匹配度 {match.score:.0%}" if match.score else ""))

        if getattr(task, "meta", None) is not None:
            task.meta.music_name = match.title or getattr(task.meta, "music_name", "")
            task.meta.artist = match.artist or getattr(task.meta, "artist", "")
            task.meta.album = match.album or getattr(task.meta, "album", "")

        cover = b""
        if match.cover_url:
            cover = fingerprint.download_cover(match.cover_url)
        new_path = self._apply_identified_tags(path, match, cover)
        return new_path

    def _apply_identified_tags(self, path: str, match, cover: bytes) -> str:
        """把识别结果写回音频文件;失败保留原文件。"""
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        try:
            changed = qmc_decrypt.write_basic_tags(
                path, title=match.title, artist=match.artist, album=match.album
            )
            if cover and ext in converter.PICTURE_CAPABLE_FORMATS:
                converter.attach_picture(path, cover, ext)
                self.ui_queue.put(("log", "  已写入封面"))
            if changed:
                self.ui_queue.put(("log", "  已写入标签"))
            elif not cover:
                self.ui_queue.put(("log", "  标签已存在,无需覆盖"))
        except Exception as e:  # noqa: BLE001
            self.ui_queue.put(("log", f"⚠ 写入识别结果失败: {e}"))
        return path

    def _rename_decrypted(self, path: str, meta) -> str:
        """按"歌手 - 歌名"重命名;取不到歌名则维持原文件名。"""
        artist = str(getattr(meta, "artist", "") or "")
        title = str(getattr(meta, "music_name", "") or "")
        target = qmc_decrypt.build_track_filename(
            artist, title, os.path.splitext(path)[1], os.path.dirname(path)
        )
        if not target:
            self.ui_queue.put(
                ("log", f"· 无法重命名(无歌名),维持: {os.path.basename(path)}")
            )
            return path
        try:
            if os.path.normcase(os.path.abspath(target)) != os.path.normcase(
                os.path.abspath(path)
            ):
                os.replace(path, target)
                self.ui_queue.put(
                    ("log", f"  已重命名 → {os.path.basename(target)}")
                )
                return target
        except OSError as e:
            self.ui_queue.put(("log", f"⚠ 重命名失败: {e}"))
        return path

    # ---------- Apple Music / Beatport 下载 ----------
    def _build_premium_tab(self, parent):
        # Premium content is intentionally scrollable: the credential and
        # output controls should remain reachable on compact laptop screens.
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        scroll.pack(fill="both", expand=True)
        parent = scroll
        self._guide(
            parent,
            "付费平台 · 需要你自己的官方订阅和授权",
            "Apple Music 需要有效订阅和浏览器导出的 Netscape cookies.txt;Beatport 需要自己的账号及对应流媒体方案。"
            "这些入口只调用第三方工具处理你有权访问的内容,不绕过 DRM、订阅或地区限制。遇到 cookies 过期、方案不匹配或区域限制时,请根据日志处理。"
            "下载前可先用“凭据自检”验证 cookies 与 Beatport 账号是否有效。",
        )
        # ---- Apple Music ----
        am_frame = ctk.CTkFrame(
            parent, corner_radius=6, fg_color=PANEL,
            border_width=1, border_color=BORDER,
        )
        am_frame.pack(fill="x", padx=10, pady=(10, 6))
        ctk.CTkLabel(
            am_frame, text="Apple Music  ·  需订阅 + cookies", anchor="w",
            font=ctk.CTkFont(size=15, weight="bold"), text_color=TEXT,
        ).pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(
            am_frame,
            text="1. 浏览器登录 music.apple.com 后,用扩展导出 cookies.txt(Netscape 格式)\n"
                 "2. 选择 cookies 文件,粘贴链接,选择音质,点下载",
            justify="left", anchor="w",
        ).pack(fill="x", padx=10)

        row1 = ctk.CTkFrame(am_frame, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(row1, text="cookies:").pack(side="left")
        self.am_cookies_var = ctk.StringVar(value="")
        ctk.CTkEntry(
            row1, textvariable=self.am_cookies_var,
            fg_color="#0f0f10", border_color=BORDER, text_color=TEXT,
        ).pack(side="left", fill="x", expand=True, padx=6)
        self._button(row1, "浏览…", self._pick_am_cookies, width=70).pack(side="left")

        row2 = ctk.CTkFrame(am_frame, fg_color="transparent")
        row2.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(row2, text="音质:").pack(side="left")
        am_codec_labels = [i18n.tr(label) for label in premium.GAMDL_CODECS]
        # 取值要用原文标签：显示名已被翻译成当前语言。
        self.am_codec_var = ctk.StringVar(
            value=premium.GAMDL_CODECS[next(iter(premium.GAMDL_CODECS))]
        )
        self.am_codec_label_var = ctk.StringVar(value=am_codec_labels[0])
        ctk.CTkOptionMenu(
            row2, width=320,
            values=am_codec_labels,
            variable=self.am_codec_label_var,
            command=self._on_am_codec_change,
            fg_color=BUTTON, button_color=BUTTON,
            button_hover_color=BUTTON_HOVER, dropdown_fg_color=PANEL_ALT,
            dropdown_hover_color=BUTTON_HOVER, text_color=TEXT,
        ).pack(side="left", padx=6)
        ctk.CTkLabel(row2, text="歌曲/专辑/歌单/艺人链接").pack(side="left", padx=10)
        self.am_url_entry = ctk.CTkEntry(
            am_frame, placeholder_text="https://music.apple.com/...",
            fg_color="#0f0f10", border_color=BORDER, text_color=TEXT,
        )
        self.am_url_entry.pack(fill="x", padx=10, pady=4)
        am_out_row = ctk.CTkFrame(am_frame, fg_color="transparent")
        am_out_row.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(am_out_row, text="保存到:").pack(side="left")
        self.am_out_dir_var = ctk.StringVar(value=os.path.join(self.default_out_dir, "AppleMusic"))
        ctk.CTkEntry(
            am_out_row, textvariable=self.am_out_dir_var,
            fg_color="#0f0f10", border_color=BORDER, text_color=TEXT,
        ).pack(side="left", fill="x", expand=True, padx=6)
        self._button(am_out_row, "浏览…", self._pick_am_out, width=70).pack(side="left")
        self._button(
            am_out_row,
            "打开目录",
            lambda: self._open_output_dir(self.am_out_dir_var.get(), "Apple Music"),
            width=92,
        ).pack(side="left", padx=(6, 0))
        self._button(am_frame, "开始下载 (Apple Music)", self._start_am_download, primary=True, width=220).pack(anchor="w", padx=10, pady=(2, 8))
        self.am_status = ctk.CTkLabel(am_frame, text="待命", anchor="w", text_color=MUTED)
        self.am_status.pack(fill="x", padx=10, pady=(0, 8))

        # ---- Beatport ----
        bp_frame = ctk.CTkFrame(
            parent, corner_radius=6, fg_color=PANEL,
            border_width=1, border_color=BORDER,
        )
        bp_frame.pack(fill="x", padx=10, pady=6)
        ctk.CTkLabel(
            bp_frame, text="Beatport  ·  需流媒体订阅（FLAC 需 Professional）", anchor="w",
            font=ctk.CTkFont(size=15, weight="bold"), text_color=TEXT,
        ).pack(fill="x", padx=10, pady=(8, 2))

        row3 = ctk.CTkFrame(bp_frame, fg_color="transparent")
        row3.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(row3, text="用户名:").pack(side="left")
        self.bp_user_var = ctk.StringVar(value="")
        ctk.CTkEntry(
            row3, textvariable=self.bp_user_var, width=180,
            fg_color="#0f0f10", border_color=BORDER, text_color=TEXT,
        ).pack(side="left", padx=6)
        ctk.CTkLabel(row3, text="密码:").pack(side="left", padx=(16, 0))
        self.bp_pass_var = ctk.StringVar(value="")
        ctk.CTkEntry(
            row3, textvariable=self.bp_pass_var, width=180, show="*",
            fg_color="#0f0f10", border_color=BORDER, text_color=TEXT,
        ).pack(side="left", padx=6)
        ctk.CTkLabel(row3, text="音质:").pack(side="left", padx=(16, 0))
        bp_quality_labels = [i18n.tr(label) for label in premium.BEATPORT_QUALITIES]
        self.bp_quality_var = ctk.StringVar(
            value=premium.BEATPORT_QUALITIES[next(iter(premium.BEATPORT_QUALITIES))][0]
        )
        self.bp_quality_label_var = ctk.StringVar(value=bp_quality_labels[0])
        ctk.CTkOptionMenu(
            row3, width=220,
            values=bp_quality_labels,
            variable=self.bp_quality_label_var,
            command=self._on_bp_quality_change,
            fg_color=BUTTON, button_color=BUTTON,
            button_hover_color=BUTTON_HOVER, dropdown_fg_color=PANEL_ALT,
            dropdown_hover_color=BUTTON_HOVER, text_color=TEXT,
        ).pack(side="left", padx=6)

        self.bp_url_entry = ctk.CTkEntry(
            bp_frame,
            placeholder_text="https://www.beatport.com/track/... 支持曲目/专辑/歌单/榜单/艺人",
            fg_color="#0f0f10", border_color=BORDER, text_color=TEXT,
        )
        self.bp_url_entry.pack(fill="x", padx=10, pady=4)
        bp_out_row = ctk.CTkFrame(bp_frame, fg_color="transparent")
        bp_out_row.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(bp_out_row, text="保存到:").pack(side="left")
        self.bp_out_dir_var = ctk.StringVar(value=os.path.join(self.default_out_dir, "Beatport"))
        ctk.CTkEntry(
            bp_out_row, textvariable=self.bp_out_dir_var,
            fg_color="#0f0f10", border_color=BORDER, text_color=TEXT,
        ).pack(side="left", fill="x", expand=True, padx=6)
        self._button(bp_out_row, "浏览…", self._pick_bp_out, width=70).pack(side="left")
        self._button(
            bp_out_row,
            "打开目录",
            lambda: self._open_output_dir(self.bp_out_dir_var.get(), "Beatport"),
            width=92,
        ).pack(side="left", padx=(6, 0))
        self._button(bp_frame, "开始下载 (Beatport)", self._start_bp_download, primary=True, width=220).pack(anchor="w", padx=10, pady=(2, 8))
        self.bp_status = ctk.CTkLabel(bp_frame, text="待命", anchor="w", text_color=MUTED)
        self.bp_status.pack(fill="x", padx=10, pady=(0, 8))

        # ---- 凭据自检: 先验证 cookies / 账号是否可用, 再开始下载 ----
        check_row = ctk.CTkFrame(parent, fg_color="transparent")
        check_row.pack(fill="x", padx=10, pady=(2, 12))
        self._button(check_row, "凭据自检", self._start_credential_check, width=120).pack(side="left")
        self.cred_check_status = ctk.CTkLabel(check_row, text="", anchor="w", text_color=MUTED)
        self.cred_check_status.pack(side="left", fill="x", expand=True, padx=8)

        self.premium_status_labels = {"apple_music": self.am_status, "beatport": self.bp_status}

    def _pick_am_cookies(self):
        if not self._require_idle("apple_music", "Apple Music"):
            return
        f = filedialog.askopenfilename(title="选择 cookies.txt", filetypes=[("Cookies", "*.txt"), ("所有文件", "*.*")])
        if f:
            self.am_cookies_var.set(f)

    def _pick_am_out(self):
        if not self._require_idle("apple_music", "Apple Music"):
            return
        d = filedialog.askdirectory(initialdir=self.am_out_dir_var.get() or os.path.expanduser("~"))
        if d:
            self.am_out_dir_var.set(d)

    def _pick_bp_out(self):
        if not self._require_idle("beatport", "Beatport"):
            return
        d = filedialog.askdirectory(initialdir=self.bp_out_dir_var.get() or os.path.expanduser("~"))
        if d:
            self.bp_out_dir_var.set(d)

    def _set_premium_status(self, service: str, text: str):
        self.ui_queue.put(("premium_status", service, text))

    def _start_credential_check(self):
        cookies = self.am_cookies_var.get().strip()
        user = self.bp_user_var.get().strip()
        password = self.bp_pass_var.get()
        if not cookies and not (user and password):
            messagebox.showwarning("提示", "请先填写要检查的凭据(Apple cookies 或 Beatport 账号)")
            return
        if not self._begin_job("cred_check"):
            return
        self.cred_check_status.configure(text="检查中…")
        threading.Thread(
            target=self._credential_check_worker,
            args=(cookies, user, password),
            daemon=True,
        ).start()

    def _credential_check_worker(self, cookies: str, user: str, password: str):
        lines: list[str] = []
        if cookies:
            try:
                lines.append(
                    f"✓ Apple Music: {i18n.tr(premium.check_apple_music_credentials(cookies))}"
                )
            except Exception as e:
                lines.append(f"✗ Apple Music: {i18n.tr(str(e))}")
        else:
            lines.append(i18n.tr("未填写 Apple cookies,已跳过 Apple 检查"))
        if user and password:
            try:
                lines.append(
                    f"✓ Beatport: {i18n.tr(premium.check_beatport_credentials(user, password))}"
                )
            except Exception as e:
                lines.append(f"✗ Beatport: {i18n.tr(str(e))}")
        else:
            lines.append(i18n.tr("未填写 Beatport 账号,已跳过 Beatport 检查"))
        for line in lines:
            self.ui_queue.put(("log", line))
        self.ui_queue.put(("cred_result", lines))
        self._end_job("cred_check")

    def _on_am_codec_change(self, label: str):
        codec = premium.GAMDL_CODECS.get(i18n.untr(label))
        if codec:
            self.am_codec_var.set(codec)

    def _on_bp_quality_change(self, label: str):
        entry = premium.BEATPORT_QUALITIES.get(i18n.untr(label))
        if entry:
            self.bp_quality_var.set(entry[0])

    def _start_am_download(self):
        url = self.am_url_entry.get().strip()
        cookies = self.am_cookies_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请粘贴 Apple Music 链接")
            return
        if not premium.is_apple_music_url(url):
            messagebox.showwarning("提示", "链接不是 music.apple.com")
            return
        if not cookies or not os.path.isfile(cookies):
            messagebox.showwarning("提示", "请先选择有效的 Netscape cookies.txt 文件。")
            return
        out_dir = self._ensure_output_dir(self.am_out_dir_var.get(), "Apple Music")
        if not out_dir:
            return
        if not self._begin_job("apple_music"):
            return
        codec = self.am_codec_var.get() or "aac-web"
        self._set_premium_status("apple_music", "Apple Music 下载中…")
        threading.Thread(
            target=self._am_worker, args=(url, cookies, codec, out_dir), daemon=True
        ).start()

    def _am_worker(self, url: str, cookies: str, codec: str, out_dir: str):
        def log(s: str):
            self.ui_queue.put(("log", s))
        try:
            files = premium.gamdl_download(url, cookies, out_dir, codec=codec, on_log=log,
                                           ffmpeg_location=self.ffmpeg)
            self._set_premium_status("apple_music", f"✓ Apple Music 完成,{len(files)} 个文件")
            self.ui_queue.put(("log", f"✓ Apple Music 下载完成: {len(files)} 个文件"))
        except Exception as e:
            self._set_premium_status("apple_music", f"✗ Apple Music 失败: {e}")
            self.ui_queue.put(("log", f"✗ Apple Music 失败: {e}"))
        finally:
            self._end_job("apple_music")

    def _start_bp_download(self):
        url = self.bp_url_entry.get().strip()
        if not url:
            messagebox.showwarning("提示", "请粘贴 Beatport 链接")
            return
        if not premium.is_beatport_url(url):
            messagebox.showwarning("提示", "链接不是 beatport.com")
            return
        username = self.bp_user_var.get().strip()
        password = self.bp_pass_var.get().strip()
        if not username or not password:
            messagebox.showwarning("提示", "请填写 Beatport 用户名和密码。")
            return
        out_dir = self._ensure_output_dir(self.bp_out_dir_var.get(), "Beatport")
        if not out_dir:
            return
        if not self._begin_job("beatport"):
            return
        quality = self.bp_quality_var.get() or "lossless"
        self._set_premium_status("beatport", "Beatport 下载中…")
        threading.Thread(
            target=self._bp_worker, args=(url, username, password, quality, out_dir), daemon=True
        ).start()

    def _bp_worker(
        self, url: str, username: str, password: str, quality: str, out_dir: str
    ):
        def log(s: str):
            self.ui_queue.put(("log", s))
        try:
            files = premium.beatportdl_download(
                url,
                username,
                password,
                out_dir,
                quality=quality, on_log=log,
            )
            self._set_premium_status("beatport", f"✓ Beatport 完成,{len(files)} 个文件")
            self.ui_queue.put(("log", f"✓ Beatport 下载完成: {len(files)} 个文件"))
        except Exception as e:
            self._set_premium_status("beatport", f"✗ Beatport 失败: {e}")
            self.ui_queue.put(("log", f"✗ Beatport 失败: {e}"))
        finally:
            self._end_job("beatport")

    # ---------- 下载逻辑 ----------
    def _on_fmt_change(self, value):
        if hasattr(self, "fmt_hint"):
            self.fmt_hint.configure(text=self._format_hint(self._selected_download_format()))

    def _pick_out_dir(self):
        if not self._require_idle("download", "下载"):
            return
        d = filedialog.askdirectory(initialdir=self.out_dir_var.get() or os.path.expanduser("~"))
        if d:
            self.out_dir_var.set(d)

    def _download_out_dir_changed(self, *_args):
        # Keep the typed path as-is; validation happens immediately before a run.
        return None

    def _probe(self):
        _raw_urls, urls = self._urls_from_entry()
        if not urls:
            messagebox.showwarning("提示", "请先粘贴至少一个链接")
            return
        url = urls[0]
        self._log(f"解析中: {url}")
        threading.Thread(target=self._probe_worker, args=(url,), daemon=True).start()

    def _probe_worker(self, url: str):
        try:
            info = downloader.probe_url(url)
            msg = f"✓ [{info['platform']}] {info['title']} | 时长 {info['duration'] or '?'}s | {info['formats']} 个可用格式"
        except Exception as e:
            msg = f"✗ 解析失败: {e}"
        self.ui_queue.put(("log", msg))

    def _start_download(self):
        raw_urls, urls = self._urls_from_entry()
        if not urls:
            messagebox.showwarning("提示", "请先粘贴至少一个链接")
            return
        invalid = [url for url in raw_urls if url not in urls]
        if invalid:
            messagebox.showwarning(
                "链接格式无效",
                "以下内容不是完整的 http/https 链接,已忽略:\n" + "\n".join(invalid[:5]),
            )
        if not self._begin_job("download", len(urls)):
            return
        out_dir = self._ensure_output_dir(self.out_dir_var.get(), "下载")
        if not out_dir:
            self._running_jobs.discard("download")
            self._job_remaining.pop("download", None)
            return
        fmt = self._selected_download_format()
        sample_rate = self._dl_sample_rate()
        rate_desc = self._rate_desc(sample_rate)
        self._hide_empty_hint("download")
        try:
            for url in urls:
                task = downloader.DownloadTask(url=url, out_dir=out_dir, fmt=fmt, title=url)
                task.sample_rate = sample_rate
                self._reset_task_for_run(task)
                self.tasks.append(task)
                row = TaskRow(self.task_container, task)
                self.task_rows.append(row)
                self.task_row_map[id(task)] = row
                threading.Thread(target=self._download_worker, args=(task,), daemon=True).start()
        except Exception:
            self._running_jobs.discard("download")
            self._job_remaining.pop("download", None)
            raise
        self.url_textbox.delete("1.0", "end")
        self._log(f"已加入 {len(urls)} 个任务 · 采样率 {rate_desc}")

    def _urls_from_entry(self) -> tuple[list[str], list[str]]:
        raw = self.url_textbox.get("1.0", "end").strip()
        urls = [u.strip() for u in raw.splitlines() if u.strip()]
        expanded: list[str] = []
        for line in urls:
            expanded.extend(line.split())
        urls = expanded
        return urls, [u for u in urls if downloader.is_valid_url(u)]

    def _clear_download_list(self):
        if not self._require_idle("download", "下载队列"):
            return
        self.tasks.clear()
        for row in self.task_rows:
            row.frame.destroy()
        self.task_rows.clear()
        self.task_row_map.clear()
        self._show_empty_hint("download")

    def _retry_failed_downloads(self):
        """只重跑下载队列里失败的任务(不重新添加行)。"""
        failed = [task for task in self.tasks if task.status == "失败"]
        if not failed:
            messagebox.showinfo("没有失败的任务", "当前下载队列里没有失败的任务。")
            return
        if not self._begin_job("download", len(failed)):
            return
        for task in failed:
            self._reset_task_for_run(task)
            self.ui_queue.put(("refresh", task))
            threading.Thread(target=self._download_worker, args=(task,), daemon=True).start()

    def _reset_task_for_run(self, task):
        task.status = "排队中"
        task.progress = 0.0
        task.error = ""
        if hasattr(task, "speed"):
            task.speed = ""
        if hasattr(task, "eta"):
            task.eta = ""
        if hasattr(task, "file_path"):
            task.file_path = ""

    def _download_worker(self, task: downloader.DownloadTask):
        # 任务入队时已锁定采样率,避免多个任务共享下拉框状态时被后续改动影响。
        sample_rate = getattr(task, "sample_rate", None)

        def on_status(s: str):
            task.status = s
            self.ui_queue.put(("refresh", task))

        def on_progress(p: float, speed: str, eta: str):
            task.update_progress(p, speed, eta)
            if self._progress_update_allowed(task):
                self.ui_queue.put(("progress", task))

        try:
            task.status = "解析中…"
            self.ui_queue.put(("refresh", task))
            # 信号量限制同时在跑的下载数:粘几十条链接时不会
            # 几十个 yt-dlp + ffmpeg 一起抢网络和磁盘。
            with self._dl_semaphore:
                if self._cancelled("download"):
                    # 排到闸门时批次已被取消:直接标记,不再下载。
                    task.status = "已取消"
                    self.ui_queue.put(("refresh", task))
                    return
                fp = downloader.download_url(
                    task.url, task.out_dir, task.fmt,
                    on_status=on_status, on_progress=on_progress,
                    ffmpeg_location=self.ffmpeg,
                    sample_rate=sample_rate,
                )
            task.status = "完成"
            task.file_path = fp
            task.title = os.path.basename(fp)
            self.ui_queue.put(("refresh", task))
            self.ui_queue.put(("log", f"✓ 完成: {fp}"))
        except Exception as e:
            task.status = "失败"
            task.error = str(e)
            self.ui_queue.put(("refresh", task))
            self.ui_queue.put(("log", f"✗ 失败: {task.url} -> {e}"))
        finally:
            self._end_job("download")

    # ---------- 转码逻辑 ----------
    def _pick_cv_out(self):
        if not self._require_idle("convert", "转码"):
            return
        d = filedialog.askdirectory(initialdir=self.cv_out_var.get() or os.path.expanduser("~"))
        if d:
            self.cv_out_var.set(d)

    def _pick_convert_files(self):
        if not self._require_idle("convert", "转码队列"):
            return
        files = filedialog.askopenfilenames(
            title="选择音频文件",
            filetypes=[("音频文件", "*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus *.wma *.aiff *.aif *.ape *.alac"), ("所有文件", "*.*")],
        )
        # 同批次不同来源可能同名(如 A/song.mp3 与 B/song.mp3),必须去重,
        # 否则后一个任务会静默覆盖前一个的结果。
        reserved: set[str] = {
            os.path.normcase(os.path.abspath(t.dst)) for t in self.cv_tasks if t.dst
        }
        for f in files:
            if not converter.is_supported(f):
                messagebox.showwarning("提示", f"不支持的格式: {f}")
                continue
            if any(t.src == f for t in self.cv_tasks):
                continue
            fmt = self.cv_fmt_var.get()
            dst = converter.build_output_path(
                f, self.cv_out_var.get().strip() or self.default_out_dir, fmt, reserved
            )
            task = converter.ConvertTask(src=f, dst=dst, fmt=fmt)
            self.cv_tasks.append(task)
            self._add_cv_row(task)
        self._log(f"已添加 {len(files)} 个转码文件")

    def _add_cv_row(self, task: converter.ConvertTask):
        self._hide_empty_hint("convert")
        row = TaskRow(self.cv_container, task)
        row.title_lbl.configure(text=os.path.basename(task.src), width=400)
        row.status_lbl.configure(text="排队中")
        self.cv_rows.append(row)
        self.cv_row_map[id(task)] = row

    def _clear_convert_list(self):
        if not self._require_idle("convert", "转码队列"):
            return
        self.cv_tasks.clear()
        for row in self.cv_rows:
            row.frame.destroy()
        self.cv_rows.clear()
        self.cv_row_map.clear()
        self._show_empty_hint("convert")
        self.cv_progress_bar.set(0)
        self.cv_progress_label.configure(text="0%")

    def _start_convert(self, retry_only: bool = False):
        if not self.cv_tasks:
            messagebox.showwarning("提示", "请先添加要转码的文件")
            return
        if not self.ffmpeg:
            messagebox.showerror("错误", "未找到 ffmpeg,无法转码。请先安装 ffmpeg 并加入 PATH。")
            return
        if retry_only:
            pending = [task for task in self.cv_tasks if task.status == "失败"]
            if not pending:
                messagebox.showinfo("没有失败的任务", "当前转码队列里没有失败的任务。")
                return
        else:
            pending = [task for task in self.cv_tasks if task.status != "完成"]
            if not pending:
                messagebox.showinfo("没有待处理任务", "当前转码队列中的文件都已完成。若要重新转码,请清空队列后重新添加。")
                return
        if not self._begin_job("convert", len(pending)):
            return
        self.cv_progress_bar.set(0)
        out_dir = self._ensure_output_dir(self.cv_out_var.get(), "转码")
        if not out_dir:
            self._running_jobs.discard("convert")
            self._job_remaining.pop("convert", None)
            return
        try:
            fmt = self.cv_fmt_var.get()
            reserved: set[str] = set()
            for task in pending:
                task.error = ""
                task.status = "排队中"
                task.set_progress(0.0)
                task.fmt = fmt
                task.dst = converter.build_output_path(task.src, out_dir, fmt, reserved)
            threading.Thread(target=self._convert_worker, args=(pending,), daemon=True).start()
        except Exception:
            self._running_jobs.discard("convert")
            self._job_remaining.pop("convert", None)
            raise

    def _convert_worker(self, tasks: list[converter.ConvertTask]):
        # 批次开始时锁定采样率,避免用户在转码中途改动下拉框影响同一批任务。
        sample_rate = self._cv_sample_rate()
        rate_desc = self._rate_desc(sample_rate)
        self.ui_queue.put(("log", f"转码批次开始 · {len(tasks)} 个文件 · {rate_desc}"))

        def run_one(task):
            task.status = "转码中…"
            self.ui_queue.put(("cv_refresh", task))

            def on_progress(p: float):
                task.set_progress(p)
                if self._progress_update_allowed(task):
                    self.ui_queue.put(("cv_progress", task))

            converter.convert_file(
                task.src, task.dst, task.fmt,
                on_progress=self._guard_progress("convert", on_progress),
                ffmpeg=self.ffmpeg,
                sample_rate=sample_rate,
            )
            task.set_progress(100.0)
            self.ui_queue.put(("cv_refresh", task))
            self.ui_queue.put(("log", f"✓ 转码完成: {task.dst}"))

        self._run_batch(
            tasks, run_one, "convert", "cv_total", "cv_refresh",
            label_fn=lambda t: getattr(t, "src", ""),
        )

    # ---------- 界面设置持久化 ----------
    def _apply_saved_settings(self) -> None:
        """启动时恢复上次的输出目录/格式/采样率/开关;凭据不存不读。"""

        def saved(key):
            return appsettings.get(key)

        for var, key in (
            (self.out_dir_var, "dl_out_dir"),
            (self.cv_out_var, "cv_out_dir"),
            (self.ncm_out_var, "ncm_out_dir"),
            (self.va_out_var, "va_out_dir"),
            (self.am_out_dir_var, "am_out_dir"),
            (self.bp_out_dir_var, "bp_out_dir"),
            (self.am_cookies_var, "am_cookies"),
        ):
            value = saved(key)
            if value:
                var.set(value)

        fmt = saved("dl_fmt")
        for kind, label in FORMAT_LABELS.items():
            if kind.value == fmt:
                self.fmt_var.set(i18n.tr(label))
                break
        if saved("cv_fmt"):
            self.cv_fmt_var.set(saved("cv_fmt"))
        if saved("va_fmt"):
            self.va_fmt_var.set(saved("va_fmt"))

        for label, value in DECRYPT_FORMAT_LABELS.items():
            if value == saved("ncm_fmt"):
                self.decrypt_fmt_var.set(value)
                self.decrypt_fmt_label_var.set(i18n.tr(label))
                break
        for label, value in DECRYPT_EXISTS_LABELS.items():
            if value == saved("ncm_exists"):
                self.decrypt_exists_var.set(value)
                self.decrypt_exists_label_var.set(i18n.tr(label))
                break

        for var, label_var, key in (
            (self.dl_rate_var, self.dl_rate_label_var, "dl_rate"),
            (self.cv_rate_var, self.cv_rate_label_var, "cv_rate"),
            (self.ncm_rate_var, self.ncm_rate_label_var, "ncm_rate"),
            (self.va_rate_var, self.va_rate_label_var, "va_rate"),
        ):
            value = saved(key)
            if value in CV_SAMPLE_RATE_LABELS:
                var.set(value)
                label_var.set(i18n.tr(CV_SAMPLE_RATE_LABELS[value]))

        if saved("ncm_rename") is not None:
            self.rename_var.set(bool(saved("ncm_rename")))
        if saved("ncm_fp") is not None:
            self.fp_var.set(bool(saved("ncm_fp")))
        if saved("acoustid_key"):
            self.fp_key_var.set(saved("acoustid_key"))

        for label, value in premium.GAMDL_CODECS.items():
            if value == saved("am_codec"):
                self.am_codec_var.set(value)
                self.am_codec_label_var.set(i18n.tr(label))
                break
        for label, entry in premium.BEATPORT_QUALITIES.items():
            if entry[0] == saved("bp_quality"):
                self.bp_quality_var.set(entry[0])
                self.bp_quality_label_var.set(i18n.tr(label))
                break

        # var.set 不会触发开关的 command,这里手动同步一次状态。
        self.rename_by_meta = bool(self.rename_var.get())
        self.fingerprint_enabled = bool(self.fp_var.get())
        self.fingerprint_key = self.fp_key_var.get().strip()

    def _register_setting_savers(self) -> None:
        """控件变化即写回设置文件(键值很小,直接写,不加防抖)。"""

        def bind_string(var, key):
            var.trace_add("write", lambda *_: appsettings.set(key, var.get()))

        def bind_bool(var, key):
            var.trace_add(
                "write", lambda *_: appsettings.set(key, bool(var.get()))
            )

        for var, key in (
            (self.out_dir_var, "dl_out_dir"),
            (self.cv_out_var, "cv_out_dir"),
            (self.ncm_out_var, "ncm_out_dir"),
            (self.va_out_var, "va_out_dir"),
            (self.am_out_dir_var, "am_out_dir"),
            (self.bp_out_dir_var, "bp_out_dir"),
            (self.am_cookies_var, "am_cookies"),
            (self.cv_fmt_var, "cv_fmt"),
            (self.va_fmt_var, "va_fmt"),
            (self.decrypt_fmt_var, "ncm_fmt"),
            (self.decrypt_exists_var, "ncm_exists"),
            (self.dl_rate_var, "dl_rate"),
            (self.cv_rate_var, "cv_rate"),
            (self.ncm_rate_var, "ncm_rate"),
            (self.va_rate_var, "va_rate"),
            (self.fp_key_var, "acoustid_key"),
            (self.am_codec_var, "am_codec"),
            (self.bp_quality_var, "bp_quality"),
        ):
            bind_string(var, key)
        for var, key in (
            (self.rename_var, "ncm_rename"),
            (self.fp_var, "ncm_fp"),
        ):
            bind_bool(var, key)
        # 下载格式下拉框存的是译文,保存时换算回内部值。
        self.fmt_var.trace_add(
            "write",
            lambda *_: appsettings.set(
                "dl_fmt", self._selected_download_format().value
            ),
        )

    # ---------- 窗口图标 ----------
    def _apply_window_icon(self):
        icon = resource_path("assets", "icon.ico")
        if not os.path.isfile(icon):
            return
        try:
            self.iconbitmap(icon)
        except tk.TclError:
            pass

    # ---------- 界面语言 ----------
    def _toggle_language(self):
        language = i18n.toggle_language()
        if language == i18n.EN:
            messagebox.showinfo(
                "Language changed",
                "The interface will switch to English after restarting the app.",
            )
        else:
            messagebox.showinfo("语言已切换", "重启应用后界面将切换为中文。")
        self.lang_btn.configure(text=i18n.toggle_label())

    # ---------- 更新检查 ----------
    def _set_update_ui(self, *, busy: bool, status: str = ""):
        self.update_btn.configure(state="disabled" if busy else "normal")
        self.update_status_lbl.configure(text=status)

    def _check_updates(self):
        if self._update_busy:
            return
        self._update_busy = True
        self._set_update_ui(busy=True, status="检查中…")
        self._log("正在检查更新…")
        threading.Thread(target=self._update_check_worker, daemon=True).start()

    def _update_check_worker(self):
        try:
            info = updater.check_for_update(APP_VERSION)
        except updater.UpdateError as exc:
            self.ui_queue.put(("update_error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - 网络层异常不应打断界面
            self.ui_queue.put(("update_error", f"检查更新失败: {exc}"))
        else:
            self.ui_queue.put(("update_result", info))

    def _handle_update_result(self, info):
        if info is None:
            self._log(f"✓ 已是最新版本 v{APP_VERSION}")
            messagebox.showinfo("检查更新", f"当前已是最新版本 v{APP_VERSION}。")
            return
        summary = f"发现新版本 {info.tag}(当前 v{APP_VERSION})。"
        self._log(summary)
        if not updater.can_self_update():
            if messagebox.askyesno(
                "发现新版本",
                summary + "\n\n当前以源码方式运行,无法自动替换程序。是否打开下载页?",
            ):
                updater.open_releases_page()
            return
        if not info.asset_url:
            if messagebox.askyesno(
                "发现新版本",
                summary + "\n\n该版本没有可下载的程序文件。是否打开 Releases 页面?",
            ):
                updater.open_releases_page()
            return
        if messagebox.askyesno(
            "发现新版本",
            summary + "\n\n是否立即下载并安装?(安装会重启程序)",
        ):
            self._start_update_download(info)

    def _start_update_download(self, info):
        self._update_busy = True
        self._set_update_ui(busy=True, status="下载中…")
        threading.Thread(
            target=self._update_download_worker, args=(info,), daemon=True
        ).start()

    def _update_download_worker(self, info):
        try:
            path = updater.download_update(info, on_progress=self._on_update_progress)
        except updater.UpdateError as exc:
            self.ui_queue.put(("update_error", str(exc)))
        except Exception as exc:  # noqa: BLE001
            self.ui_queue.put(("update_error", f"下载更新失败: {exc}"))
        else:
            self.ui_queue.put(("update_ready", path))

    def _on_update_progress(self, ratio: float):
        self.ui_queue.put(("update_progress", ratio))

    def _apply_update(self, path: str):
        try:
            updater.install_and_restart(path)
        except updater.UpdateError as exc:
            messagebox.showerror("更新失败", str(exc))
            self._log(f"✗ 更新失败: {exc}")
            self._update_busy = False
            self._set_update_ui(busy=False)
            return
        self._log("替换程序已就绪,退出后将自动完成安装并重启…")
        self._closing = True
        self.destroy()

    # ---------- UI 队列 ----------
    def _row_for(self, row_map: dict, task, action: str) -> None:
        """按 id(task) O(1) 取行; 几百个任务时避免每次消息都线性遍历。"""
        row = row_map.get(id(task))
        if row is None:
            return
        if action == "refresh":
            row.refresh()
        else:
            row.progress.set(task.progress / 100.0)

    def _set_total_progress(self, bar, label, payload) -> None:
        """更新总进度条与百分比文字; payload 兼容 (done, total) 与 0~1 比例。"""
        if isinstance(payload, tuple) and len(payload) == 2:
            done, total = payload
            ratio = (done / total) if total else 0.0
            text = f"{done}/{total} · {ratio * 100:.0f}%"
        else:
            ratio = float(payload or 0.0)
            text = f"{ratio * 100:.0f}%"
        bar.set(min(max(ratio, 0.0), 1.0))
        if label is not None:
            label.configure(text=text)

    def _create_ncm_rows_in_batches(self, tasks: list, batch: int = 12) -> None:
        """分批创建任务行: 一次几百个控件会把主线程卡住。"""
        self._hide_empty_hint("ncm")
        chunk, rest = tasks[:batch], tasks[batch:]
        for task in chunk:
            row = TaskRow(self.ncm_container, task)
            row.title_lbl.configure(text=os.path.basename(task.src), width=400)
            self.ncm_rows.append(row)
            self.ncm_row_map[id(task)] = row
        if rest:
            self.after(1, lambda: self._create_ncm_rows_in_batches(rest, batch))
            return
        if getattr(self, "ncm_drop_enabled", False):
            # 新行会挡住拖放(Tk 拖放不向父级冒泡), 全部建好后重新注册一次。
            dragdrop.enable_drop(
                self.ncm_container,
                self._on_decrypt_drop,
                exts={".ncm"} | set(qmc_decrypt.QMC_EXTS),
            )

    def _drain_ui_queue(self):
        # Keep a busy batch from starving Tk's event loop. Workers may still
        # enqueue while we render; the next scheduled drain will catch up.
        processed = 0
        max_messages = 400
        deadline = time.monotonic() + 0.008
        pending_logs: list[str] = []
        try:
            while processed < max_messages and time.monotonic() < deadline:
                msg = self.ui_queue.get_nowait()
                processed += 1
                kind = msg[0]
                if kind == "log":
                    pending_logs.append(msg[1])
                    continue
                elif kind == "refresh":
                    self._row_for(self.task_row_map, msg[1], "refresh")
                elif kind == "progress":
                    self._row_for(self.task_row_map, msg[1], "progress")
                elif kind == "cv_refresh":
                    self._row_for(self.cv_row_map, msg[1], "refresh")
                elif kind == "cv_progress":
                    self._row_for(self.cv_row_map, msg[1], "progress")
                elif kind == "cv_total":
                    self._set_total_progress(
                        self.cv_progress_bar, self.cv_progress_label, msg[1]
                    )
                elif kind == "ncm_refresh":
                    self._row_for(self.ncm_row_map, msg[1], "refresh")
                elif kind == "ncm_progress":
                    self._row_for(self.ncm_row_map, msg[1], "progress")
                elif kind == "ncm_total":
                    self._set_total_progress(
                        self.ncm_progress_bar, self.ncm_progress_label, msg[1]
                    )
                elif kind == "va_refresh":
                    self._row_for(self.va_row_map, msg[1], "refresh")
                elif kind == "va_progress":
                    self._row_for(self.va_row_map, msg[1], "progress")
                elif kind == "va_total":
                    self._set_total_progress(
                        self.va_progress_bar, self.va_progress_label, msg[1]
                    )
                elif kind == "premium_status":
                    service, text = msg[1], msg[2]
                    label = self.premium_status_labels.get(service)
                    if label is not None:
                        label.configure(text=text)
                elif kind == "cred_result":
                    self.cred_check_status.configure(text="自检完成")
                    messagebox.showinfo("凭据自检", "\n".join(msg[1]))
                elif kind == "job_done":
                    self._finish_job(msg[1])
                elif kind == "batch_summary":
                    self._handle_batch_summary(msg[1], msg[2])
                elif kind == "update_error":
                    self._update_busy = False
                    self._set_update_ui(busy=False)
                    self._log(f"✗ {msg[1]}")
                    messagebox.showerror("检查更新", msg[1])
                elif kind == "update_result":
                    self._update_busy = False
                    self._set_update_ui(busy=False)
                    self._handle_update_result(msg[1])
                elif kind == "update_progress":
                    self.update_status_lbl.configure(text=f"下载中 {msg[1] * 100:.0f}%")
                elif kind == "update_ready":
                    self._update_busy = False
                    self._set_update_ui(busy=False, status="待安装")
                    if messagebox.askyesno(
                        "更新已下载", "更新已下载完成。是否立即重启程序完成安装?"
                    ):
                        self._apply_update(msg[1])
        except queue.Empty:
            pass
        finally:
            if pending_logs:
                self._log_many(pending_logs)
        if not self._closing:
            self.after(20, self._drain_ui_queue)

    def _log(self, text: str):
        self._log_many([text])

    def _log_many(self, texts: list[str]):
        """成批写入日志: 一次 insert + 一次 see, 避免大量消息拖死界面。"""
        if not texts:
            return
        for text in texts:
            log.info("%s", text)
        joined = "\n".join(i18n.tr(text) for text in texts) + "\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", joined)
        try:
            lines = int(self.log_box.index("end-1c").split(".")[0])
            if lines > 5000:
                # 长时间运行后 Text 控件会越积越慢, 只保留最近的内容。
                self.log_box.delete("1.0", "1000.0")
        except (ValueError, IndexError):
            pass
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def on_close(self):
        if self._running_jobs:
            jobs = ", ".join(sorted(self._running_jobs))
            if not messagebox.askyesno(
                "任务进行中",
                f"以下任务仍在运行: {jobs}\n\n退出后下载/转码子进程可能仍需片刻结束。确定退出吗?",
            ):
                return
        self._closing = True
        self.destroy()


def _install_excepthooks() -> None:
    """把未捕获异常写进日志, 便于用户回传定位问题。"""

    def _hook(exc_type, exc, tb):
        log.error("uncaught exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    def _thread_hook(args):
        log.error(
            "uncaught thread exception (%s)",
            getattr(getattr(args, "thread", None), "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _hook
    threading.excepthook = _thread_hook


def main():
    _install_excepthooks()
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
