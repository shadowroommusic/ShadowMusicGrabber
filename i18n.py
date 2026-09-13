# -*- coding: utf-8 -*-
"""中英文界面支持。

设计取舍：界面文案直接写在控件构造处，逐个改成 ``t("key")`` 会让改动面非常大。
这里改为在边界统一翻译——导入本模块并调用 :func:`install` 后，CustomTkinter
控件、标签页、下拉选项、文件对话框和 messagebox 都会在构造/更新时按表翻译。

因此：
* 新增文案 → 往 ``_EN`` 里加一条即可；
* 含变量（路径、版本号、数量）的文案 → 加到 ``_PREFIXES`` 或 ``_PATTERNS``；
* 语言来源 → 设置文件 > 系统语言（zh* 视为中文，其余为英文）；
* 切换语言需重启应用（界面在启动时构建）。
"""

from __future__ import annotations

import json
import os
import re
import sys

ZH = "zh"
EN = "en"

_language = ZH

# ---------------------------------------------------------------- 文案对照表
# 键为界面里的中文原文，值为英文。未收录的文案原样返回。
_EN = {
    # 通用
    "音乐抓取与无损转码工具": "Music downloader & lossless converter",
    "下载 · 转码 · 本地还原": "Download · Convert · Decrypt",
    "活动日志": "Activity log",
    "检查更新": "Check for updates",
    "提示": "Notice",
    "错误": "Error",
    "任务进行中": "Task in progress",
    "没有待处理任务": "Nothing pending",
    "发现新版本": "Update available",
    "更新失败": "Update failed",
    "更新已下载": "Update downloaded",
    "语言已切换": "Language changed",
    "待命": "Idle",
    "下载": "Download",
    "转码": "Convert",
    "完成": "Done",
    "失败": "Failed",
    "排队中": "Queued",
    "解析中…": "Resolving…",
    "转码中…": "Converting…",
    "解密中…": "Decrypting…",
    "下载中…": "Downloading…",
    "检查中…": "Checking…",
    "待安装": "Ready to install",
    "下载完成,转码中…": "Downloaded, converting…",
    "处理完成": "Finished",
    "转码为 WAV…": "Converting to WAV…",
    "Apple Music 下载中…": "Downloading from Apple Music…",
    "Beatport 下载中…": "Downloading from Beatport…",
    # 标签页
    "链接下载": "Link download",
    "本地转码": "Local convert",
    "解密": "Decrypt",
    "NCM 解密": "NCM decrypt",
    # 链接下载页
    "链接下载 · 适用范围与步骤": "Link download · scope and steps",
    "适用于 yt-dlp 能访问的公开媒体链接,常见为 YouTube、SoundCloud、B 站、Vimeo 和 Bandcamp。"
    "Spotify、网易云、QQ 音乐和 Apple Music 的目录页是否可用取决于站点、地区、登录状态及授权,"
    "受 DRM 保护内容不会下载。每行一个链接 → 可先点“解析第一条”检查 → 选格式和目录 → 点“开始下载”。"
    "Apple Music/Beatport 的已授权内容请使用对应专用页。":
        "Works with public media links that yt-dlp can reach, typically YouTube, SoundCloud, Bilibili, "
        "Vimeo and Bandcamp. Whether Spotify, NetEase Cloud Music, QQ Music or Apple Music pages work "
        "depends on the site, region, sign-in state and authorization; DRM-protected content is never "
        "downloaded. One link per line → optionally click “Probe first link” → choose format and folder → "
        "click “Start download”. For authorized Apple Music / Beatport content use the dedicated tab.",
    "链接（每行一个；也可用空格分隔）": "Links (one per line; spaces also work)",
    "输出格式:": "Output format:",
    "保存到:": "Save to:",
    "浏览…": "Browse…",
    "打开目录": "Open folder",
    "解析第一条": "Probe first link",
    "开始下载": "Start download",
    "清空队列": "Clear queue",
    "下载队列": "Download queue",
    "原格式: 保留站点提供的音频格式,通常无需重新编码。":
        "Original: keep the format the site serves; usually no re-encoding.",
    "MP3: 兼容性优先,会使用 ffmpeg 重新编码。": "MP3: compatibility first; re-encoded with ffmpeg.",
    "WAV: 无压缩容器,文件会较大;从有损源转换不会提升音质。":
        "WAV: uncompressed container, large files; converting a lossy source does not add quality.",
    "FLAC: 无损容器,适合归档;从有损源转换不会提升音质。":
        "FLAC: lossless container for archiving; converting a lossy source does not add quality.",
    "FLAC (无损)": "FLAC (lossless)",
    "WAV (无损)": "WAV (lossless)",
    "原格式 (不转码)": "Original (no re-encode)",
    # 本地转码页
    "本地转码 · 处理电脑上的音频文件": "Local convert · files on this PC",
    "支持 MP3、WAV、FLAC、M4A、AAC、OGG、OPUS、WMA、AIFF、APE 等常见格式。"
    "步骤:选择目标 FLAC/WAV → 添加一个或多个文件 → 选择输出目录 → 点“开始转码”。"
    "转码只改变容器/编码方式;从有损音频转成 FLAC/WAV 不会提升原始音质。":
        "Supports MP3, WAV, FLAC, M4A, AAC, OGG, OPUS, WMA, AIFF, APE and more. Steps: choose target "
        "FLAC/WAV → add one or more files → choose the output folder → click “Start convert”. Converting "
        "only changes the container/codec; a lossy source converted to FLAC/WAV keeps the original quality.",
    "目标格式:": "Target format:",
    "输出目录:": "Output folder:",
    "FLAC 适合归档;WAV 文件较大但兼容性广。":
        "FLAC is good for archiving; WAV is larger but widely compatible.",
    "添加文件…": "Add files…",
    "开始转码": "Start convert",
    "清空列表": "Clear list",
    "转码队列": "Convert queue",
    "选择音频文件": "Select audio files",
    "音频文件": "Audio files",
    "不支持的格式: ": "Unsupported format: ",
    "只支持 .ncm 文件: ": "Only .ncm files are supported: ",
    "请先添加要转码的文件": "Add the files you want to convert first",
    "未找到 ffmpeg,无法转码。请先安装 ffmpeg 并加入 PATH。":
        "ffmpeg not found, cannot convert. Install ffmpeg and add it to PATH.",
    "当前转码队列中的文件都已完成。若要重新转码,请清空队列后重新添加。":
        "Every file in the convert queue is already done. Clear the queue and add them again to re-convert.",
    # 解密页
    "解密 · 网易云 NCM 与 QQ 音乐加密文件": "Decrypt · NetEase NCM & QQ Music encrypted files",
    "支持网易云音乐下载得到的 .ncm 与 QQ 音乐加密文件(.qmc0/.qmc3/.qmcflac/.qmcogg,以及 .mflac/.mgg 等)。"
    "步骤:选择输出目录 → 添加一个或多个加密文件 → 点“开始解密”。程序会在本地还原音频;NCM 会尽力写入歌曲名、歌手、专辑和封面,QQ 音乐文件自带标签会保留。"
    "不会上传文件;请只处理你拥有或明确获授权的内容。":
        "Supports .ncm files downloaded by the NetEase Cloud Music client and QQ Music encrypted files "
        "(.qmc0/.qmc3/.qmcflac/.qmcogg, .mflac/.mgg, etc.). Steps: choose the output folder → add one or "
        "more encrypted files → click “Start decrypt”. Decryption runs locally; NCM files get title, "
        "artist, album and cover written when available, and QQ Music tags are preserved. Nothing is "
        "uploaded; only process content you own or are authorized to handle.",
    "添加加密文件…": "Add encrypted files…",
    "开始解密": "Start decrypt",
    "解密队列": "Decrypt queue",
    "选择加密音乐文件": "Select encrypted music files",
    "所有文件": "All files",
    "加密音乐文件": "Encrypted music files",
    "网易云音乐 NCM": "NetEase Cloud Music NCM",
    "QQ 音乐 QMC": "QQ Music QMC",
    "请先添加要解密的文件": "Add files to decrypt first",
    # 解密页：输出格式与拖放
    "原始格式 (解密得到什么就是什么)": "Original (keep whatever decryption produces)",
    "· 可直接拖入文件或文件夹": "· drag files or folders straight in",
    "· 请用“添加加密文件”按钮选择": "· use the “Add encrypted files” button instead",
    "拖入的内容里没有可解密的文件": "Nothing droppable in there — no .ncm / .qmc / .mflac / .mgg files",
    "当前解密队列中的文件都已完成。若要重新解密,请清空队列后重新添加。":
        "Every file in the decrypt queue is already done. Clear the queue and add them again to re-decrypt.",
    # Premium 页
    "付费平台 · 需要你自己的官方订阅和授权": "Paid platforms · requires your own subscription and authorization",
    "Apple Music 需要有效订阅和浏览器导出的 Netscape cookies.txt;Beatport 需要自己的账号及对应流媒体方案。"
    "这些入口只调用第三方工具处理你有权访问的内容,不绕过 DRM、订阅或地区限制。"
    "遇到 cookies 过期、方案不匹配或区域限制时,请根据日志处理。"
    "下载前可先用“凭据自检”验证 cookies 与 Beatport 账号是否有效。":
        "Apple Music needs an active subscription plus a Netscape cookies.txt exported from your browser; "
        "Beatport needs your own account and matching streaming plan. These entries only call third-party "
        "tools for content you are entitled to access and never bypass DRM, subscriptions or regional "
        "limits. Check the log when cookies expire or the plan/region does not match. Use “Check "
        "credentials” to verify your cookies and Beatport account first.",
    "Apple Music  ·  需订阅 + cookies": "Apple Music  ·  subscription + cookies required",
    "1. 浏览器登录 music.apple.com 后,用扩展导出 cookies.txt(Netscape 格式)\n"
    "2. 选择 cookies 文件,粘贴链接,选择音质,点下载":
        "1. Sign in to music.apple.com in your browser and export cookies.txt (Netscape format)\n"
        "2. Pick the cookies file, paste the link, choose quality and start",
    "音质:": "Quality:",
    "歌曲/专辑/歌单/艺人链接": "Song / album / playlist / artist link",
    "开始下载 (Apple Music)": "Start download (Apple Music)",
    "Beatport  ·  需流媒体订阅（FLAC 需 Professional）":
        "Beatport  ·  streaming plan required (FLAC needs Professional)",
    "用户名:": "Username:",
    "密码:": "Password:",
    "https://www.beatport.com/track/... 支持曲目/专辑/歌单/榜单/艺人":
        "https://www.beatport.com/track/... tracks, releases, playlists, charts and artists are supported",
    "开始下载 (Beatport)": "Start download (Beatport)",
    "选择 cookies.txt": "Select cookies.txt",
    "请粘贴 Apple Music 链接": "Paste an Apple Music link first",
    "链接不是 music.apple.com": "That is not a music.apple.com link",
    "请先选择有效的 Netscape cookies.txt 文件。": "Pick a valid Netscape cookies.txt file first.",
    "请粘贴 Beatport 链接": "Paste a Beatport link first",
    "链接不是 beatport.com": "That is not a beatport.com link",
    "请填写 Beatport 用户名和密码。": "Enter your Beatport username and password.",
    "FLAC 无损 (44.1kHz)": "FLAC lossless (44.1kHz)",
    "AAC 256kbps": "AAC 256kbps",
    "AAC 128kbps": "AAC 128kbps",
    "AAC 128kbps (HLS)": "AAC 128kbps (HLS)",
    "AAC 256kbps (推荐, 无需额外配置)": "AAC 256kbps (recommended, no extra setup)",
    "ALAC 无损 24bit/192kHz (需 wrapper 服务)": "ALAC lossless 24bit/192kHz (needs a wrapper service)",
    # 队列与弹窗
    "当前模块已有任务正在运行,请等待完成后再开始新的任务。":
        "This module is already running a task. Wait for it to finish before starting a new one.",
    "无法使用输出目录": "Cannot use the output folder",
    "无法打开目录": "Cannot open the folder",
    "正在运行。请等待当前批次完成后再修改队列。":
        "is running. Wait for the current batch to finish before changing the queue.",
    "请先粘贴至少一个链接": "Paste at least one link first",
    "链接格式无效": "Invalid link format",
    "以下内容不是完整的 http/https 链接,已忽略:\n":
        "These entries are not complete http/https links and were ignored:\n",
    "请先粘贴至少一个链接": "Paste at least one link first",
    # 更新
    "正在检查更新…": "Checking for updates…",
    "当前已是最新版本 v": "You are on the latest version v",
    "✓ 已是最新版本 v": "✓ Already on the latest version v",
    "检查更新失败: ": "Update check failed: ",
    "下载更新失败: ": "Update download failed: ",
    "✗ 更新失败: ": "✗ Update failed: ",
    "替换程序已就绪,退出后将自动完成安装并重启…":
        "Replacement is ready; the installer will finish and restart the app after exit…",
    "下载中 ": "Downloading ",
    "更新已下载完成。是否立即重启程序完成安装?":
        "The update has been downloaded. Restart now to finish installing?",
    "发现新版本 ": "New version available ",
    "(当前 v": " (current v",
    "。\n\n当前以源码方式运行,无法自动替换程序。是否打开下载页?":
        ".\n\nRunning from source, so the app cannot replace itself. Open the download page?",
    "。\n\n该版本没有可下载的程序文件。是否打开 Releases 页面?":
        ".\n\nThis release has no downloadable program file. Open the Releases page?",
    "。\n\n是否立即下载并安装?(安装会重启程序)":
        ".\n\nDownload and install now? (installing restarts the app)",
    # 其他
    "就绪。ffmpeg: ": "Ready. ffmpeg: ",
    "未找到(转码将不可用)": "not found (conversion unavailable)",
    "提示:仅处理你拥有或明确获授权的内容;不绕过 DRM、订阅、地区或访问限制。":
        "Note: only process content you own or are authorized to handle; no DRM, subscription, region or "
        "access-control bypass.",
    "网易云音乐": "NetEase Cloud Music",
    "QQ音乐": "QQ Music",
    "哔哩哔哩": "Bilibili",
    "通用": "Generic",
    "剩余 ": "ETA ",
    # 更新器 / 转码器 / 解密 / 付费模块的提示
    "更新仓库还没有发布任何 Release。": "This repository has not published any release yet.",
    "更新服务暂时限制了访问频率，请稍后再试。": "The update service is rate-limiting requests; try again later.",
    "这个 Release 没有可下载的 exe/zip 资产。": "This release has no downloadable exe/zip asset.",
    "下载的文件大小与 Release 不一致，已取消安装。": "Downloaded file size does not match the release; install cancelled.",
    "当前以源码方式运行，不能自动替换程序文件。": "Running from source, the program file cannot be replaced automatically.",
    "更新服务返回了无法解析的数据。": "The update service returned unreadable data.",
    "输出路径不能与源文件相同,请改用其他文件名或目录": "The output path must differ from the source; use another file name or folder",
    "未找到 ffmpeg,请先安装并加入 PATH": "ffmpeg not found; install it and add it to PATH",
    "转码后未生成有效输出文件": "No valid output file was produced",
    "不是有效的 NCM 文件(魔数错误)": "Not a valid NCM file (bad magic header)",
    "NCM 文件损坏(key 长度非法)": "Corrupted NCM file (invalid key length)",
    "key 数据为空": "The key data is empty",
    "NCM 文件损坏(元数据长度非法)": "Corrupted NCM file (invalid metadata length)",
    "只支持 .ncm 文件": "Only .ncm files are supported",
    "未找到 ffmpeg,无法将内嵌 MP3 转码为 FLAC": "ffmpeg not found; cannot convert the embedded MP3 to FLAC",
    # QMC / QQ 音乐解密模块的提示
    "解密结果不是有效的音频: 密钥不匹配或该文件使用了更新的加密格式":
        "Decrypted output is not valid audio: key mismatch or a newer unsupported format",
    "密钥密文长度非法": "Invalid encrypted-key length",
    "密钥密文填充异常": "Unexpected encrypted-key padding",
    "密钥校验失败(可能是未知的新格式或文件损坏)":
        "Key validation failed (possibly an unknown new format or a corrupted file)",
    "文件太小,不是有效的 QMC 文件": "File is too small to be a valid QMC file",
    "检测到更新的 STag 加密格式: 该格式不内嵌密钥,无法离线解密":
        "Newer STag encryption detected: it embeds no key and cannot be decrypted offline",
    "检测到 musicex 加密格式: 该格式暂不受支持": "musicex encryption detected: not supported yet",
    "无法解析文件尾部密钥(可能是未知的新加密格式)":
        "Cannot parse the embedded key at the end of the file (possibly a newer unsupported format)",
    "尾部密钥为空": "The embedded key is empty",
    "QTag 数据不完整": "Incomplete QTag data",
    "QTag 数据长度非法": "Invalid QTag data length",
    "文件不含音频数据": "The file contains no audio data",
    "文件读取不完整": "Incomplete file read",
    "RC4 密钥无效": "Invalid RC4 key",
    "xor 长度不一致": "xor length mismatch",
    # 凭据自检(Apple / Beatport)
    "凭据自检": "Check credentials",
    "自检完成": "Check completed",
    "请先填写要检查的凭据(Apple cookies 或 Beatport 账号)":
        "Enter the credentials to check first (Apple cookies or Beatport account)",
    "未填写 Apple cookies,已跳过 Apple 检查": "Apple cookies not filled in — Apple check skipped",
    "未填写 Beatport 账号,已跳过 Beatport 检查": "Beatport account not filled in — Beatport check skipped",
    "cookies.txt 解析失败: 请使用 Netscape 格式(浏览器扩展导出的原始文件)。":
        "Failed to parse cookies.txt: use the Netscape format (the raw file exported by a browser extension).",
    "无法访问 music.apple.com: 请检查网络后重试。": "Cannot reach music.apple.com: check your network and try again.",
    "无法从 Apple Music 页面提取令牌(页面结构可能已更新)。":
        "Could not extract a token from the Apple Music page (the page structure may have changed).",
    "cookies 中没有 media-user-token: 请先登录 music.apple.com 再导出。":
        "The cookies contain no media-user-token: sign in on music.apple.com and export again.",
    "无法访问 Apple Music 接口: 请检查网络后重试。":
        "Cannot reach the Apple Music API: check your network and try again.",
    "Apple cookies 已失效: 请重新登录 music.apple.com 并导出新的 cookies.txt。":
        "Apple cookies have expired: sign in on music.apple.com again and export a new cookies.txt.",
    "Apple Music 接口返回了无法解析的数据。": "The Apple Music API returned unreadable data.",
    "cookies 有效,但该 Apple 账号当前没有有效的 Apple Music 订阅。":
        "The cookies are valid, but this Apple account has no active Apple Music subscription.",
    "无法访问 Beatport: 请检查网络后重试。": "Cannot reach Beatport: check your network and try again.",
    "Beatport 暂时限制了登录尝试: 请稍后再试。": "Beatport is rate-limiting login attempts: try again later.",
    "Beatport 凭据有效: 登录成功(可用音质取决于订阅档)。":
        "Beatport credentials are valid: sign-in succeeded (available quality depends on your plan).",
    "Beatport 响应异常(未返回会话): 请稍后重试。":
        "Unexpected Beatport response (no session returned): try again later.",
    "Beatport 账号或密码错误: 请检查后重试(连续失败可能触发人机验证)。":
        "Wrong Beatport username or password: check and retry (repeated failures may trigger a captcha).",
    "文件内容全为 0，疑似未完成下载或云盘占位文件；请重新下载/同步后再试":
        "The file is entirely zeros — likely an unfinished download or a cloud placeholder; "
        "download or sync it again",
    "不是 Apple Music 链接": "Not an Apple Music link",
    "未找到 cookies.txt。请先在 Apple Music 网页登录后,用浏览器扩展(如 Get cookies.txt LOCALLY)导出 Netscape 格式 cookies。":
        "cookies.txt not found. Sign in on the Apple Music website, then export Netscape-format cookies "
        "with a browser extension such as Get cookies.txt LOCALLY.",
    "gamdl 已返回成功,但没有生成新的音频文件;请检查 URL、cookies、订阅和地区权限。":
        "gamdl reported success but produced no new audio file; check the URL, cookies, subscription "
        "and regional access.",
    "不是 Beatport 链接": "Not a Beatport link",
    "请填写 Beatport 账号(用户名/密码)": "Enter your Beatport account (username/password)",
    "未找到 beatportdl,请将其放入程序 bin/ 目录": "beatportdl not found; put it in the program's bin/ folder",
    "beatportdl 已返回成功,但没有生成新的音频文件;请检查账号、订阅档、URL 和地区权限。":
        "beatportdl reported success but produced no new audio file; check the account, plan, URL "
        "and regional access.",
    "重启应用后界面将切换为中文。": "The interface will switch back to Chinese after restarting the app.",
    "\n\n是否立即下载并安装?(安装会重启程序)":
        "\n\nDownload and install now? (installing restarts the app)",
    "\n\n当前以源码方式运行,无法自动替换程序。是否打开下载页?":
        "\n\nRunning from source, so the app cannot replace itself. Open the download page?",
    "\n\n该版本没有可下载的程序文件。是否打开 Releases 页面?":
        "\n\nThis release has no downloadable program file. Open the Releases page?",
}

# 前缀式文案（含变量，取前缀替换，其余原样保留）
_PREFIXES = (
    ("✓ 完成: ", "✓ Done: "),
    ("✗ 失败: ", "✗ Failed: "),
    ("✓ 转码完成: ", "✓ Converted: "),
    ("✗ 转码失败: ", "✗ Convert failed: "),
    ("✓ 解密完成: ", "✓ Decrypted: "),
    ("✗ 解密失败: ", "✗ Decrypt failed: "),
    ("解析中: ", "Resolving: "),
    ("✗ 解析失败: ", "✗ Resolve failed: "),
    ("解析失败: ", "Resolve failed: "),
    ("只支持 .ncm 文件: ", "Only .ncm files are supported: "),
    ("不支持的格式: ", "Unsupported format: "),
    ("不支持该文件类型: ", "Unsupported file type: "),
    ("不支持的文件类型: ", "Unsupported file type: "),
    ("密钥解析失败: ", "Key parsing failed: "),
    ("密钥 Base64 解码失败: ", "Key Base64 decoding failed: "),
    ("解密后的密钥长度异常: ", "Unexpected key length after decryption: "),
    ("文件不存在: ", "File not found: "),
    ("剩余 ", "ETA "),
)

# 正则兜底：处理中文出现在中间、或需要重排语序的文案
_PATTERNS = (
    (
        re.compile(r"^(.+)无法创建输出目录:\n(.+)\n\n(.+)$", re.S),
        "Cannot create the {0} output folder:\n{1}\n\n{2}",
    ),
    (re.compile(r"^(.+)目录打开失败:\n(.+)\n\n(.+)$", re.S), "Cannot open the {0} folder:\n{1}\n\n{2}"),
    (re.compile(r"^(.+)输出目录不可用: (.+) -> (.+)$"), "The {0} output folder is unusable: {1} -> {2}"),
    (re.compile(r"^无法打开(.+)目录: (.+) -> (.+)$"), "Cannot open the {0} folder: {1} -> {2}"),
    (
        re.compile(r"^以下任务仍在运行: (.+)\n\n退出后下载/转码子进程可能仍需片刻结束。确定退出吗\?$", re.S),
        "These tasks are still running: {0}\n\nChild download/convert processes may take a moment to stop "
        "after exit. Quit anyway?",
    ),
    (re.compile(r"^以下内容不是完整的 http/https 链接,已忽略:\n(.+)$", re.S),
     "These entries are not complete http/https links and were ignored:\n{0}"),
    (re.compile(r"^✓ \[(.+)\] (.+) \| 时长 (\d+)s \| (\d+) 个可用格式$"),
     "✓ [{0}] {1} | duration {2}s | {3} format(s) available"),
    (re.compile(r"^就绪。ffmpeg: (.+)$"), "Ready. ffmpeg: {0}"),
    (re.compile(r"^\.\.\.$"), "..."),
    (re.compile(r"^(.+),请查看上方日志\(常见原因:cookies 过期或订阅无效\)$"),
     "{0} — see the log above (usual causes: expired cookies or an inactive subscription)"),
    (re.compile(r"^✓ 已是最新版本 v(.+)$"), "✓ Already on the latest version v{0}"),
    (re.compile(r"^当前已是最新版本 v(.+)。$"), "You are on the latest version v{0}."),
    (re.compile(r"^发现新版本 (.+)\(当前 v(.+)\)。$"), "New version {0} available (current v{1})."),
    (re.compile(r"^下载中 (\d+)%$"), "Downloading {0}%"),
    (re.compile(r"^提示:仅处理(.+)$"), "Note: only process{0}"),
    # 组合文案：中文出现在中间，需要整体重排
    (re.compile(r"^已加入 (.+) 个任务$"), "Queued {0} task(s)"),
    (re.compile(r"^已添加 (.+) 个加密文件$"), "Added {0} encrypted file(s)"),
    (re.compile(r"^已添加 (.+) 个转码文件$"), "Added {0} file(s) to convert"),
    (re.compile(r"^✓ Apple Music 完成,(.+) 个文件$"), "✓ Apple Music finished: {0} file(s)"),
    (re.compile(r"^✓ Apple Music 下载完成: (.+) 个文件$"), "✓ Apple Music finished: {0} file(s)"),
    (re.compile(r"^✓ Beatport 完成,(.+) 个文件$"), "✓ Beatport finished: {0} file(s)"),
    (re.compile(r"^✓ Beatport 下载完成: (.+) 个文件$"), "✓ Beatport finished: {0} file(s)"),
    (re.compile(r"^Apple Music 凭据有效,订阅正常\(区域 (.+)\)$"),
     "Apple Music credentials are valid; subscription active (storefront {0})"),
    (re.compile(r"^Apple Music 接口返回 HTTP (\d+): 请稍后重试。$"),
     "The Apple Music API returned HTTP {0}: try again later."),
    (re.compile(r"^(.+)正在运行。请等待当前批次完成后再修改队列。$"),
     "{0} is running. Wait for the current batch to finish before changing the queue."),
    (re.compile(r"^更新服务返回 HTTP (\d+)。$"), "Update service returned HTTP {0}."),
    (re.compile(r"^gamdl 退出码 (.+),请查看上方日志\(常见原因:cookies 过期或订阅无效\)$"),
     "gamdl exit code {0} — see the log above (usual causes: expired cookies or an inactive subscription)"),
    (re.compile(r"^beatportdl 退出码 (.+),请查看上方日志\(常见原因:订阅档不支持所选音质或账号错误\)$"),
     "beatportdl exit code {0} — see the log above (usual causes: the plan does not allow that quality, "
     "or wrong credentials)"),
    (re.compile(r"^发现新版本 (.+)\(当前 v(.+)\)。\n\n是否立即下载并安装\?\(安装会重启程序\)$", re.S),
     "New version {0} available (current v{1}).\n\nDownload and install now? (installing restarts the app)"),
    (re.compile(r"^发现新版本 (.+)\(当前 v(.+)\)。\n\n当前以源码方式运行,无法自动替换程序。是否打开下载页\?$", re.S),
     "New version {0} available (current v{1}).\n\nRunning from source, so the app cannot replace "
     "itself. Open the download page?"),
    (re.compile(r"^发现新版本 (.+)\(当前 v(.+)\)。\n\n该版本没有可下载的程序文件。是否打开 Releases 页面\?$", re.S),
     "New version {0} available (current v{1}).\n\nThis release has no downloadable program file. "
     "Open the Releases page?"),
)

_REVERSE = None


def _settings_path() -> str:
    root = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(root, "ShadowMusicGrabber", "settings.json")


def current_language() -> str:
    return _language


def set_language(language: str) -> str:
    """设置当前界面语言，返回生效后的语言代码。"""
    global _language
    _language = EN if str(language or "").lower().startswith("en") else ZH
    return _language


def detect_language() -> str:
    """读取已保存的语言；没有保存时按系统语言判断。"""
    try:
        with open(_settings_path(), "r", encoding="utf-8") as handle:
            saved = json.load(handle).get("language")
        if saved in (ZH, EN):
            return saved
    except (OSError, ValueError):
        pass
    try:
        import locale

        code = (locale.getlocale()[0] or "")
    except Exception:  # noqa: BLE001 - 语言检测失败时退回英文
        code = ""
    if not code:
        code = os.environ.get("LANG", "")
    return ZH if str(code).lower().startswith("zh") else EN


def load_language() -> str:
    return set_language(detect_language())


def save_language(language: str | None = None) -> None:
    """把语言写入设置文件（与 premium 的配置目录一致）。"""
    value = language or _language
    path = _settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle) or {}
            except (OSError, ValueError):
                data = {}
        data["language"] = value
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def toggle_language() -> str:
    """在中文与英文之间切换并保存，返回新的语言。"""
    language = set_language(EN if _language == ZH else ZH)
    save_language(language)
    return language


def toggle_label() -> str:
    """语言切换按钮上显示的目标语言。"""
    return "English" if _language == ZH else "中文"


def _reverse_table() -> dict:
    global _REVERSE
    if _REVERSE is None:
        _REVERSE = {value: key for key, value in _EN.items()}
    return _REVERSE


def untr(text: str) -> str:
    """把已翻译的界面文字还原成中文原文（下拉框回读时用）。"""
    if not isinstance(text, str):
        return text
    if _language == ZH:
        return text
    return _reverse_table().get(text, text)


def tr(text):
    """把界面文案翻译成当前语言；未收录时原样返回。"""
    if not isinstance(text, str) or not text or _language == ZH:
        return text
    translated = _EN.get(text)
    if translated is not None:
        return translated
    for prefix, replacement in _PREFIXES:
        if len(prefix) > 1 and text.startswith(prefix):
            return replacement + text[len(prefix):]
    for pattern, template in _PATTERNS:
        match = pattern.match(text)
        if match:
            return template.format(*match.groups())
    return text


# ------------------------------------------------------------ 边界统一翻译
def _translate_kwargs(kwargs: dict, *, values_key: bool, extra: tuple) -> None:
    if isinstance(kwargs.get("text"), str):
        kwargs["text"] = tr(kwargs["text"])
    for key in extra:
        if isinstance(kwargs.get(key), str):
            kwargs[key] = tr(kwargs[key])
    if values_key and isinstance(kwargs.get("values"), (list, tuple)):
        kwargs["values"] = [tr(item) if isinstance(item, str) else item for item in kwargs["values"]]


def _patch_widget(cls, *, values_key: bool = False, extra: tuple = ()) -> None:
    if getattr(cls, "_i18n_patched", False):
        return
    original_init = cls.__init__
    original_configure = cls.configure

    def __init__(self, *args, **kwargs):
        _translate_kwargs(kwargs, values_key=values_key, extra=extra)
        original_init(self, *args, **kwargs)

    def configure(self, *args, **kwargs):
        _translate_kwargs(kwargs, values_key=values_key, extra=extra)
        return original_configure(self, *args, **kwargs)

    cls.__init__ = __init__
    cls.configure = configure
    cls._i18n_patched = True


def _patch_tabview(cls) -> None:
    if getattr(cls, "_i18n_patched_tabs", False):
        return
    original_add = cls.add

    def add(self, name, **kwargs):
        if isinstance(name, str):
            name = tr(name)
        return original_add(self, name, **kwargs)

    cls.add = add
    cls._i18n_patched_tabs = True


def _patch_messagebox(module) -> None:
    for name in (
        "showinfo",
        "showwarning",
        "showerror",
        "askyesno",
        "askokcancel",
        "askquestion",
        "askretrycancel",
    ):
        original = getattr(module, name, None)
        if original is None or getattr(original, "_i18n_patched", False):
            continue

        def make_wrapper(func):
            def wrapper(title=None, message=None, *args, **kwargs):
                return func(tr(title), tr(message), *args, **kwargs)

            wrapper._i18n_patched = True
            return wrapper

        setattr(module, name, make_wrapper(original))


def _patch_filedialog(module) -> None:
    for name in ("askopenfilename", "askopenfilenames", "asksaveasfilename", "askdirectory"):
        original = getattr(module, name, None)
        if original is None or getattr(original, "_i18n_patched", False):
            continue

        def make_wrapper(func):
            def wrapper(*args, **kwargs):
                if isinstance(kwargs.get("title"), str):
                    kwargs["title"] = tr(kwargs["title"])
                filetypes = kwargs.get("filetypes")
                if isinstance(filetypes, (list, tuple)):
                    kwargs["filetypes"] = [
                        (tr(item[0]), *item[1:]) if isinstance(item, (list, tuple)) and item else item
                        for item in filetypes
                    ]
                return func(*args, **kwargs)

            wrapper._i18n_patched = True
            return wrapper

        setattr(module, name, make_wrapper(original))


def install() -> None:
    """给控件与对话框装上翻译钩子（重复调用无副作用）。"""
    import customtkinter as ctk
    from tkinter import filedialog, messagebox

    for widget in (ctk.CTkLabel, ctk.CTkButton, ctk.CTkFrame, ctk.CTkScrollableFrame):
        _patch_widget(widget)
    for widget in (ctk.CTkCheckBox, ctk.CTkRadioButton, ctk.CTkSwitch):
        _patch_widget(widget)
    _patch_widget(ctk.CTkOptionMenu, values_key=True)
    _patch_widget(ctk.CTkSegmentedButton, values_key=True)
    _patch_widget(ctk.CTkEntry, extra=("placeholder_text",))
    _patch_widget(ctk.CTkTextbox, extra=("placeholder_text",))
    _patch_tabview(ctk.CTkTabview)
    _patch_messagebox(messagebox)
    _patch_filedialog(filedialog)
