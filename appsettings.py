# -*- coding: utf-8 -*-
"""界面设置持久化。

与 i18n 的语言设置共用同一个 settings.json(%APPDATA%\\ShadowMusicGrabber
或 ~/.config/ShadowMusicGrabber),读写时保留其他键,互相不覆盖。

持久化的都是"上次用过什么":各页输出目录、格式、采样率、同名策略、
重命名/指纹开关与 AcoustID Key。不存任何账号密码。
"""

from __future__ import annotations

import json
import os
import threading

_LOCK = threading.Lock()


def settings_path() -> str:
    root = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(root, "ShadowMusicGrabber", "settings.json")


def _load_all() -> dict:
    try:
        with open(settings_path(), "r", encoding="utf-8") as handle:
            return json.load(handle) or {}
    except (OSError, ValueError):
        return {}


def get(key: str, default=None):
    """读一个设置;不存在或文件损坏时返回 default。"""
    with _LOCK:
        return _load_all().get(key, default)


def set(key: str, value) -> bool:
    """写一个设置(保留文件里的其他键)。失败安静返回 False。"""
    with _LOCK:
        data = _load_all()
        data[key] = value
        path = settings_path()
        tmp = f"{path}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
