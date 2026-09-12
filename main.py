# -*- coding: utf-8 -*-
"""音乐抓取与无损转码工具 - Windows 桌面版。

提供公开链接下载、本地转码，以及用户自有网易云 .ncm 文件的本地还原。
不包含 DRM、订阅、地区或访问限制绕过能力。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

import converter
import downloader
import i18n
import ncm_decrypt
import premium
import updater

APP_NAME = "Shadow MusicGrabber"
APP_VERSION = "1.6.0"

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

        self.default_out_dir = os.path.join(os.path.expanduser("~"), "Music", "ShadowMusicGrabber")
        self.tasks: list[downloader.DownloadTask] = []
        self.task_rows: list[TaskRow] = []
        self.ui_queue: queue.Queue = queue.Queue()
        self._running_jobs: set[str] = set()
        self._job_remaining: dict[str, int] = {}
        self._last_progress_update: dict[int, float] = {}
        self._progress_lock = threading.Lock()
        self._closing = False
        self._update_busy = False
        self.ffmpeg = converter.find_ffmpeg()

        self._build_ui()
        self.after(100, self._drain_ui_queue)
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
        tab_ncm = self.tabs.add("NCM 解密")
        tab_am = self.tabs.add("Apple/Beatport")

        self._build_download_tab(tab_dl)
        self._build_convert_tab(tab_cv)
        self._build_ncm_tab(tab_ncm)
        self._build_premium_tab(tab_am)

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
        self.fmt_hint.pack(fill="x", padx=10, pady=(0, 8))

        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=6)
        self._button(btn_frame, "解析第一条", self._probe, width=120).pack(side="left")
        self._button(btn_frame, "开始下载", self._start_download, primary=True, width=140).pack(side="left", padx=8)
        self._button(btn_frame, "清空队列", self._clear_download_list, width=100).pack(side="left")

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

    def _build_convert_tab(self, parent):
        self._guide(
            parent,
            "本地转码 · 处理电脑上的音频文件",
            "支持 MP3、WAV、FLAC、M4A、AAC、OGG、OPUS、WMA、AIFF、APE 等常见格式。"
            "步骤:选择目标 FLAC/WAV → 添加一个或多个文件 → 选择输出目录 → 点“开始转码”。"
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
        ctk.CTkLabel(
            parent, text="FLAC 适合归档;WAV 文件较大但兼容性广。", anchor="w",
            text_color=MUTED,
        ).pack(fill="x", padx=16, pady=(0, 4))

        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=6)
        self._button(btn_frame, "添加文件…", self._pick_convert_files, width=140).pack(side="left")
        self._button(btn_frame, "开始转码", self._start_convert, primary=True, width=140).pack(side="left", padx=8)
        self._button(btn_frame, "清空列表", self._clear_convert_list, width=100).pack(side="left")

        list_frame = ctk.CTkFrame(
            parent, corner_radius=6, fg_color=PANEL,
            border_width=1, border_color=BORDER,
        )
        list_frame.pack(fill="both", expand=True, padx=10, pady=6)
        ctk.CTkLabel(list_frame, text="转码队列", anchor="w").pack(fill="x", padx=8, pady=(6, 0))
        self.cv_container = ctk.CTkScrollableFrame(list_frame, fg_color="#0f0f10")
        self.cv_container.pack(fill="both", expand=True, padx=6, pady=6)

        self.cv_tasks: list[converter.ConvertTask] = []
        self.cv_rows: list[TaskRow] = []
        self.cv_progress_bar = ctk.CTkProgressBar(
            parent, height=10, fg_color="#303035", progress_color=ACCENT
        )
        self.cv_progress_bar.pack(fill="x", padx=10, pady=(0, 10))
        self.cv_progress_bar.set(0)

    # ---------- NCM 解密逻辑 ----------
    def _build_ncm_tab(self, parent):
        self._guide(
            parent,
            "NCM 解密 · 仅针对网易云音乐缓存文件",
            "输入必须是网易云音乐下载得到的 .ncm 文件,不是网易云网页链接,也不支持 QQ 音乐 .qmc/.mflac 等加密格式。"
            "步骤:选择输出目录 → 添加一个或多个 .ncm → 点“开始解密”。程序会在本地还原音频,尽力写入歌曲名、歌手、专辑和封面。"
            "不会上传文件;请只处理你拥有或明确获授权的内容。",
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
        self._button(
            row,
            "打开目录",
            lambda: self._open_output_dir(self.ncm_out_var.get(), "NCM 解密"),
            width=92,
        ).pack(side="left", padx=(6, 0))

        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=6)
        self._button(btn_frame, "添加 .ncm 文件…", self._pick_ncm_files, width=150).pack(side="left")
        self._button(btn_frame, "开始解密", self._start_ncm_decrypt, primary=True, width=140).pack(side="left", padx=8)
        self._button(btn_frame, "清空列表", self._clear_ncm_list, width=100).pack(side="left")

        list_frame = ctk.CTkFrame(
            parent, corner_radius=6, fg_color=PANEL,
            border_width=1, border_color=BORDER,
        )
        list_frame.pack(fill="both", expand=True, padx=10, pady=6)
        ctk.CTkLabel(list_frame, text="解密队列", anchor="w").pack(fill="x", padx=8, pady=(6, 0))
        self.ncm_container = ctk.CTkScrollableFrame(list_frame, fg_color="#0f0f10")
        self.ncm_container.pack(fill="both", expand=True, padx=6, pady=6)

        self.ncm_tasks: list[ncm_decrypt.NcmTask] = []
        self.ncm_rows: list[TaskRow] = []
        self.ncm_progress_bar = ctk.CTkProgressBar(
            parent, height=10, fg_color="#303035", progress_color=ACCENT
        )
        self.ncm_progress_bar.pack(fill="x", padx=10, pady=(0, 10))
        self.ncm_progress_bar.set(0)

    def _pick_ncm_out(self):
        if not self._require_idle("ncm", "NCM 解密"):
            return
        d = filedialog.askdirectory(initialdir=self.ncm_out_var.get() or os.path.expanduser("~"))
        if d:
            self.ncm_out_var.set(d)

    def _pick_ncm_files(self):
        if not self._require_idle("ncm", "NCM 解密队列"):
            return
        files = filedialog.askopenfilenames(
            title="选择 .ncm 文件",
            filetypes=[("网易云音乐 NCM", "*.ncm"), ("所有文件", "*.*")],
        )
        for f in files:
            if not f.lower().endswith(".ncm"):
                messagebox.showwarning("提示", f"只支持 .ncm 文件: {f}")
                continue
            if any(t.src == f for t in self.ncm_tasks):
                continue
            dst = ncm_decrypt.make_output_path(f, self.ncm_out_var.get())
            task = ncm_decrypt.NcmTask(src=f, dst=dst)
            self.ncm_tasks.append(task)
            row = TaskRow(self.ncm_container, task)
            row.title_lbl.configure(text=os.path.basename(f), width=400)
            self.ncm_rows.append(row)
        self._log(f"已添加 {len(files)} 个 .ncm 文件")

    def _clear_ncm_list(self):
        if not self._require_idle("ncm", "NCM 解密队列"):
            return
        self.ncm_tasks.clear()
        for row in self.ncm_rows:
            row.frame.destroy()
        self.ncm_rows.clear()
        self.ncm_progress_bar.set(0)

    def _start_ncm_decrypt(self):
        if not self.ncm_tasks:
            messagebox.showwarning("提示", "请先添加 .ncm 文件")
            return
        pending = [task for task in self.ncm_tasks if task.status != "完成"]
        if not pending:
            messagebox.showinfo("没有待处理任务", "当前 NCM 队列中的文件都已完成。若要重新解密,请清空队列后重新添加。")
            return
        if not self._begin_job("ncm", len(pending)):
            return
        self.ncm_progress_bar.set(0)
        out_dir = self._ensure_output_dir(self.ncm_out_var.get(), "NCM 解密")
        if not out_dir:
            self._running_jobs.discard("ncm")
            self._job_remaining.pop("ncm", None)
            return
        try:
            reserved: set[str] = set()
            for task in pending:
                task.error = ""
                task.status = "排队中"
                task.set_progress(0.0)
                task.dst = ncm_decrypt.make_output_path(task.src, out_dir, reserved)
            threading.Thread(target=self._ncm_worker, args=(pending,), daemon=True).start()
        except Exception:
            self._running_jobs.discard("ncm")
            self._job_remaining.pop("ncm", None)
            raise

    def _ncm_worker(self, tasks: list[ncm_decrypt.NcmTask]):
        total = len(tasks)
        for i, task in enumerate(tasks):
            task.status = "解密中…"
            self.ui_queue.put(("ncm_refresh", task))

            def on_progress(p: float):
                task.set_progress(p)
                if self._progress_update_allowed(task):
                    self.ui_queue.put(("ncm_progress", task))

            try:
                dst, meta = ncm_decrypt.decrypt_file(
                    task.src, task.dst, on_progress=on_progress, ffmpeg=self.ffmpeg)
                task.status = "完成"
                task.set_progress(100.0)
                self.ui_queue.put(("ncm_refresh", task))
                name = meta.music_name or os.path.basename(dst)
                self.ui_queue.put(("log", f"✓ 解密完成: {name} -> {dst}"))
            except Exception as e:
                task.status = "失败"
                task.error = str(e)
                self.ui_queue.put(("ncm_refresh", task))
                self.ui_queue.put(("log", f"✗ 解密失败: {task.src} -> {e}"))
            self.ui_queue.put(("ncm_total", (i + 1) / total))
            self._end_job("ncm")

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
            "这些入口只调用第三方工具处理你有权访问的内容,不绕过 DRM、订阅或地区限制。遇到 cookies 过期、方案不匹配或区域限制时,请根据日志处理。",
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
        self.am_codec_var = ctk.StringVar(value=premium.GAMDL_CODECS[am_codec_labels[0]])
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
        self.bp_quality_var = ctk.StringVar(value=premium.BEATPORT_QUALITIES[bp_quality_labels[0]][0])
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
        try:
            for url in urls:
                task = downloader.DownloadTask(url=url, out_dir=out_dir, fmt=fmt, title=url)
                self._reset_task_for_run(task)
                self.tasks.append(task)
                row = TaskRow(self.task_container, task)
                self.task_rows.append(row)
                threading.Thread(target=self._download_worker, args=(task,), daemon=True).start()
        except Exception:
            self._running_jobs.discard("download")
            self._job_remaining.pop("download", None)
            raise
        self.url_textbox.delete("1.0", "end")
        self._log(f"已加入 {len(urls)} 个任务")

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
            fp = downloader.download_url(
                task.url, task.out_dir, task.fmt,
                on_status=on_status, on_progress=on_progress,
                ffmpeg_location=self.ffmpeg,
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
        for f in files:
            if not converter.is_supported(f):
                messagebox.showwarning("提示", f"不支持的格式: {f}")
                continue
            if any(t.src == f for t in self.cv_tasks):
                continue
            fmt = self.cv_fmt_var.get()
            dst = converter.build_output_path(f, self.cv_out_var.get().strip() or self.default_out_dir, fmt)
            task = converter.ConvertTask(src=f, dst=dst, fmt=fmt)
            self.cv_tasks.append(task)
            self._add_cv_row(task)
        self._log(f"已添加 {len(files)} 个转码文件")

    def _add_cv_row(self, task: converter.ConvertTask):
        row = TaskRow(self.cv_container, task)
        row.title_lbl.configure(text=os.path.basename(task.src), width=400)
        row.status_lbl.configure(text="排队中")
        self.cv_rows.append(row)

    def _clear_convert_list(self):
        if not self._require_idle("convert", "转码队列"):
            return
        self.cv_tasks.clear()
        for row in self.cv_rows:
            row.frame.destroy()
        self.cv_rows.clear()
        self.cv_progress_bar.set(0)

    def _start_convert(self):
        if not self.cv_tasks:
            messagebox.showwarning("提示", "请先添加要转码的文件")
            return
        if not self.ffmpeg:
            messagebox.showerror("错误", "未找到 ffmpeg,无法转码。请先安装 ffmpeg 并加入 PATH。")
            return
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
        total = len(tasks)
        for i, task in enumerate(tasks):
            task.status = "转码中…"
            self.ui_queue.put(("cv_refresh", task))

            def on_progress(p: float):
                task.set_progress(p)
                if self._progress_update_allowed(task):
                    self.ui_queue.put(("cv_progress", task))

            try:
                converter.convert_file(
                    task.src, task.dst, task.fmt,
                    on_progress=on_progress,
                    ffmpeg=self.ffmpeg,
                )
                task.status = "完成"
                task.set_progress(100.0)
                self.ui_queue.put(("cv_refresh", task))
                self.ui_queue.put(("log", f"✓ 转码完成: {task.dst}"))
            except Exception as e:
                task.status = "失败"
                task.error = str(e)
                self.ui_queue.put(("cv_refresh", task))
                self.ui_queue.put(("log", f"✗ 转码失败: {task.src} -> {e}"))
            self.ui_queue.put(("cv_total", (i + 1) / total))
            self._end_job("convert")

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
    def _drain_ui_queue(self):
        # Keep a busy batch from starving Tk's event loop. Workers may still
        # enqueue while we render; the next scheduled drain will catch up.
        processed = 0
        max_messages = 400
        try:
            while processed < max_messages:
                msg = self.ui_queue.get_nowait()
                processed += 1
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "refresh":
                    for row in self.task_rows:
                        if row.task is msg[1]:
                            row.refresh()
                elif kind == "progress":
                    for row in self.task_rows:
                        if row.task is msg[1]:
                            row.progress.set(msg[1].progress / 100.0)
                elif kind == "cv_refresh":
                    for row in self.cv_rows:
                        if row.task is msg[1]:
                            row.refresh()
                elif kind == "cv_progress":
                    for row in self.cv_rows:
                        if row.task is msg[1]:
                            row.progress.set(msg[1].progress / 100.0)
                elif kind == "cv_total":
                    self.cv_progress_bar.set(msg[1])
                elif kind == "ncm_refresh":
                    for row in self.ncm_rows:
                        if row.task is msg[1]:
                            row.refresh()
                elif kind == "ncm_progress":
                    for row in self.ncm_rows:
                        if row.task is msg[1]:
                            row.progress.set(msg[1].progress / 100.0)
                elif kind == "ncm_total":
                    self.ncm_progress_bar.set(msg[1])
                elif kind == "premium_status":
                    service, text = msg[1], msg[2]
                    label = self.premium_status_labels.get(service)
                    if label is not None:
                        label.configure(text=text)
                elif kind == "job_done":
                    self._finish_job(msg[1])
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
        if not self._closing:
            self.after(100, self._drain_ui_queue)

    def _log(self, text: str):
        text = i18n.tr(text)
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
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


def main():
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
