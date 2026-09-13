# -*- coding: utf-8 -*-
"""文件拖放支持。

用可选的第三方库 tkinterdnd2 给任意 Tk 控件注册"拖入文件"能力；
库不存在或平台不支持时返回 False，调用方回退到按钮选择即可，不影响其他功能。
"""

from __future__ import annotations

import os
from typing import Callable, Iterable, Optional

# tkinterdnd2 的常量；这里用字面量避免在模块顶层强依赖该库。
_DND_FILES = "DND_Files"


def available() -> bool:
    """拖放库是否可用。"""
    try:
        import tkinterdnd2  # noqa: F401
    except Exception:  # noqa: BLE001 - 缺失或加载失败都视为不可用
        return False
    return True


def parse_drop_paths(tk_widget, data: str) -> list[str]:
    """把拖放事件的原始数据解析成路径列表。

    Tcl 列表格式会把含空格的路径放在大括号里（例如 ``{C:/a b/x.mflac}``），
    ``tk.splitlist`` 能正确处理这些引用形式。
    """
    if not data:
        return []
    text = data.strip()
    try:
        parts = tk_widget.tk.splitlist(text)
    except Exception:  # noqa: BLE001 - 极少见的畸形数据，退回按行/空格粗切
        parts = text.replace("{", "").replace("}", "").split()
    return [part for part in (p.strip() for p in parts) if part]


def expand_paths(paths: Iterable[str], exts: Optional[Iterable[str]] = None) -> list[str]:
    """展开路径：目录会递归扫描，只保留扩展名匹配的文件。

    顺序稳定、结果去重；不改变用户传入的相对/绝对形式（转绝对路径处理）。
    """
    wanted = {e.lower() for e in exts} if exts else None
    found: list[str] = []
    seen: set[str] = set()

    def _keep(path: str) -> bool:
        return wanted is None or os.path.splitext(path)[1].lower() in wanted

    def _add(path: str) -> None:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        seen.add(key)
        found.append(path)

    for raw in paths:
        path = os.path.abspath(os.fspath(raw))
        if os.path.isdir(path):
            for dirpath, _dirs, files in os.walk(path):
                for name in sorted(files):
                    candidate = os.path.join(dirpath, name)
                    if _keep(candidate):
                        _add(candidate)
        elif os.path.isfile(path) and _keep(path):
            _add(path)
    return found


def enable_drop(
    widget,
    on_paths: Callable[[list[str]], None],
    *,
    exts: Optional[Iterable[str]] = None,
) -> bool:
    """把 widget 注册为文件拖放目标；成功返回 True。

    on_paths 收到的是过滤（并展开目录）后的绝对路径列表。
    """
    try:
        from tkinterdnd2 import TkinterDnD
    except Exception:  # noqa: BLE001 - 未安装则静默降级
        return False

    try:
        root = widget.winfo_toplevel()
        # 给 root 装载 tkdnd 扩展；同一 root 重复调用是安全的。
        TkinterDnD._require(root)

        def handler(event):
            paths = expand_paths(parse_drop_paths(widget, getattr(event, "data", "")), exts)
            on_paths(paths)
            return getattr(event, "action", None)

        widget.drop_target_register(_DND_FILES)
        widget.dnd_bind("<<Drop>>", handler)
        # 拖入时高亮，离开/放下后恢复，给一点反馈。
        widget.dnd_bind("<<DropEnter>>", lambda event: _set_hover(widget, True))
        widget.dnd_bind("<<DropLeave>>", lambda event: _set_hover(widget, False))
        widget.dnd_bind("<<Drop>>", lambda event: _set_hover(widget, False), add=True)
        return True
    except Exception:  # noqa: BLE001 - 平台/主题差异导致注册失败时不影响主流程
        return False


def _set_hover(widget, hovering: bool) -> None:
    """拖入拖出时给队列区域一点视觉反馈（失败忽略）。"""
    try:
        widget.configure(border_color="#c4f04c" if hovering else "#2a2a2e")
    except Exception:  # noqa: BLE001 - 控件不支持该属性时忽略
        pass
