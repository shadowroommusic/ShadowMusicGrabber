# 音乐抓取与无损转码工具 (MusicGrabber)

Windows 桌面工具，提供四个彼此独立的工作模块：公开链接下载、本地音频转码、网易云 NCM 文件还原，以及需要用户自己订阅和账号的 Apple Music / Beatport 下载。界面采用深色极简布局，所有模块都提供“打开目录”入口。

项目主页与下载：<https://github.com/shadowroommusic/MusicGrabber>

> 合法使用：只处理你拥有或明确获授权的内容。程序不绕过 DRM、订阅、地区限制、访问控制或加密壳。Apple Music / Beatport 的下载结果和可用音质由你的账号权限、地区和第三方工具决定。

## 下载与安装

### 方式一：直接使用打包版本（推荐）

1. 打开 [Releases](https://github.com/shadowroommusic/MusicGrabber/releases/latest) 下载 `MusicGrabber.exe`。
2. 双击运行，无需安装 Python。
3. 建议把 FFmpeg 放到 EXE 同目录（或在系统 PATH 中），否则下载/转码功能不可用。

### 方式二：从源码运行

```powershell
git clone https://github.com/shadowroommusic/MusicGrabber.git
cd MusicGrabber
python -m pip install -r requirements.txt
python main.py
```

需要 Python 3.10+ 与可用的 FFmpeg，可用 `winget install Gyan.FFmpeg` 安装。程序会自动查找 PATH、程序目录、PyInstaller 临时目录和常见 WinGet 路径中的 `ffmpeg` / `ffprobe`。

## 自动更新

主界面右上角有 **检查更新** 按钮，更新源固定指向本仓库的 Releases：

- 程序启动后任何时候都可以点击“检查更新”。
- 发现新版本时，会询问是否下载；确认后自动下载并替换当前 EXE，然后重启程序。
- 网络异常或仓库暂时不可达时只提示错误，不影响其他功能。
- 从源码运行时不会替换文件，只提供下载页入口。

实现见 `updater.py`；版本号由 `main.py` 中的 `APP_VERSION` 决定，发布新版本时同步修改它并打上同名 tag（例如 `v1.5.0`）。

## 模块与使用方法

### 1. 链接下载

适用于 yt-dlp 能访问的公开媒体链接，常见平台包括 YouTube、SoundCloud、哔哩哔哩、Vimeo、Bandcamp，以及部分 Spotify、网易云音乐、QQ 音乐和 Apple Music 页面。不同平台可能要求登录、Cookie 或地区可用性；付费 Apple Music / Beatport 请使用专用模块。

使用步骤：

1. 每行粘贴一个 `http://` 或 `https://` 链接，也可以用空格分隔多个链接。
2. 点击“解析第一条”查看平台、标题、时长和可用格式。
3. 选择 `FLAC`、`WAV`、`MP3` 或“原格式”，设置保存目录后点击“开始下载”。

`FLAC/WAV` 是无损容器；如果源文件本身是 MP3/AAC/Opus，转换不会增加原始音质。默认不展开播放列表，避免误下载大量内容。

### 2. 本地转码

用于电脑上已有的音频文件。支持 MP3、WAV、FLAC、M4A、AAC、OGG、OPUS、WMA、AIFF、APE、ALAC 等常见扩展名，目标格式为 FLAC 或 WAV。

使用步骤：选择目标格式和输出目录，点击“添加文件”，确认队列后点击“开始转码”。程序会保留源文件，遇到同名输出会自动使用 `_1`、`_2` 等后缀；失败时不会破坏已有目标文件。

### 3. NCM 解密

只针对网易云音乐客户端下载得到的 `.ncm` 文件，不是网易云网页链接，也不支持 QQ 音乐 `.qmc/.mflac` 等其他加密格式。

使用步骤：选择输出目录，点击“添加 .ncm 文件”，再点击“开始解密”。程序在本地还原内嵌 FLAC/MP3 音频，并尽力写入歌曲名、歌手、专辑和封面，不上传文件。解密过程在后台执行，队列和日志会持续刷新；完成后可以直接点击“打开目录”。

### 4. Apple Music / Beatport

此页只服务于你自己的官方订阅和授权。

- **Apple Music**：在浏览器登录 `music.apple.com`，使用 Cookie 导出扩展导出 Netscape 格式 `cookies.txt`，选择文件、粘贴歌曲/专辑/歌单/艺人链接并选择编码。AAC 256kbps 通常无需额外 wrapper；ALAC 需要 gamdl 支持的 wrapper 服务。
- **Beatport**：填写自己的 Beatport 流媒体订阅账号，选择与你的方案匹配的音质。FLAC 和 AAC 256 需要 Professional，AAC 128 需要 Advanced，HLS 选项需要 Essential。程序将配置写入 `%APPDATA%\MusicGrabber`，输出目录与 Apple Music 分开。

Cookie、用户名和密码只用于本机调用第三方工具；请勿把凭据提交给不受信任的软件或他人。常见失败原因包括 Cookie 过期、账号方案不匹配、地区限制、URL 类型不支持和第三方工具变化，详细信息会显示在日志区。

> Beatport 下载依赖第三方命令行工具 `beatportdl.exe`。出于体积与再分发考虑，仓库不包含该文件；需要该功能时请自行获取并放入仓库的 `bin\` 目录（打包脚本会自动把它并入 EXE）。缺少它时其他三个模块不受影响。

## 构建 EXE

在安装依赖后执行：

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --clean MusicGrabber.spec
```

生成物为 `dist\MusicGrabber.exe`。spec 会收集 yt-dlp、customtkinter、gamdl、Crypto、mutagen；`bin\beatportdl.exe` 存在时一并打包。FFmpeg 体积较大且许可/版本各异，默认不复制进 EXE；发布时请确保用户机器可找到 FFmpeg，或将 `ffmpeg.exe` / `ffprobe.exe` 放在 EXE 同目录或 `bin` 子目录。

## 测试

```powershell
python -m py_compile main.py converter.py downloader.py ncm_decrypt.py premium.py updater.py
python -m unittest discover tests -v
```

测试覆盖 URL 校验与平台识别、yt-dlp 选项、FFmpeg/ffprobe 定位、24-bit 音频转码、原子输出、NCM 标准双长度封面段解析、优化后的 C 层 XOR 解密、元数据写入、Premium 参数校验，以及更新模块的版本比较与 Release 解析。联网平台是否可下载仍取决于实时网络、账号和平台策略。

## 目录

```text
MusicGrabber/
├── main.py              # customtkinter GUI(含“检查更新”入口)
├── updater.py           # GitHub Releases 更新检查/下载/替换
├── downloader.py        # yt-dlp 链接下载与解析
├── converter.py         # FFmpeg 本地转码
├── ncm_decrypt.py       # 网易云 .ncm 本地还原
├── premium.py           # gamdl / beatportdl 授权调用
├── MusicGrabber.spec    # PyInstaller 配置
├── bin/                 # 可选第三方工具(beatportdl.exe)
├── tests/               # 单元测试
└── LICENSE              # MIT
```
