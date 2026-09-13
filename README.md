# Shadow MusicGrabber

<img src="assets/icon.png" width="104" align="right" alt="app icon">

Windows 桌面工具，四个彼此独立的工作模块：公开链接下载、本地音频转码、网易云 NCM 与 QQ 音乐加密文件还原，以及需要用户自己订阅和账号的 Apple Music / Beatport 下载。深色极简界面，**支持中英文双语**，内置 GitHub Releases 自动更新。

[![Latest release](https://img.shields.io/github/v/release/shadowroommusic/ShadowMusicGrabber?label=release)](https://github.com/shadowroommusic/ShadowMusicGrabber/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## 界面预览 / Screenshots

| 中文界面 | English UI |
| --- | --- |
| ![中文界面](docs/screenshot-zh.png) | ![English UI](docs/screenshot-en.png) |

A Windows desktop toolbox with four independent modules: public link downloading, local audio conversion, NetEase Cloud Music `.ncm` and QQ Music encrypted-file decryption, and Apple Music / Beatport downloading for accounts you own. Dark minimal UI with **Chinese/English interface** and built-in GitHub Releases auto-update.

项目主页 / Repository: <https://github.com/shadowroommusic/ShadowMusicGrabber>

> 合法使用：只处理你拥有或明确获授权的内容。程序不绕过 DRM、订阅、地区限制、访问控制或加密壳。Apple Music / Beatport 的下载结果和可用音质由你的账号权限、地区和第三方工具决定。

应用图标：纯黑圆角底 + 白色 S，由 `tools/make_icon.py` 生成（改参数即可重画）。

## 下载与安装

### 方式一：直接使用打包版本（推荐）

1. 打开 [Releases](https://github.com/shadowroommusic/ShadowMusicGrabber/releases/latest) 下载 `ShadowMusicGrabber.exe`。
2. 双击运行，无需安装 Python。
3. 建议把 FFmpeg 放到 EXE 同目录（或在系统 PATH 中），否则下载/转码功能不可用。

### 方式二：从源码运行

```powershell
git clone https://github.com/shadowroommusic/ShadowMusicGrabber.git
cd ShadowMusicGrabber
python -m pip install -r requirements.txt
python main.py
```

需要 Python 3.10+ 与可用的 FFmpeg，可用 `winget install Gyan.FFmpeg` 安装。程序会自动查找 PATH、程序目录、PyInstaller 临时目录和常见 WinGet 路径中的 `ffmpeg` / `ffprobe`。

## 界面语言 / Language

- 首次启动按系统语言自动选择：`zh*` 用中文，其他用英文。
- 主界面右上角按钮可在 **中文 / English** 之间切换，设置保存在 `%APPDATA%\ShadowMusicGrabber\settings.json`，重启后生效。
- 文案表在 `i18n.py`：新增界面文字后，往 `_EN` 加一条即可；含变量的文案加到 `_PREFIXES` 或 `_PATTERNS`。
- `tests/test_i18n.py` 会扫描全部界面文案，缺译文会直接测试失败，避免漏翻。

## 自动更新

主界面右上角有 **检查更新 / Check for updates** 按钮，更新源固定指向本仓库的 Releases：

- 发现新版本时询问是否下载；确认后自动下载并替换当前 EXE，然后重启程序。
- 优先走 GitHub API；**API 被限流（未登录每小时 60 次）或不可用时自动退回网页方式**读取最新版本与资产地址。
- 下载后校验文件大小，不一致则拒绝安装；替换由「等待退出 → 覆盖 → 重启」的批处理脚本完成。
- 从源码运行时不会替换文件，只提供下载页入口。

版本号由 `main.py` 中的 `APP_VERSION` 决定；发布新版本时同步修改它，并打上同名 tag（例如 `v1.6.0`）上传 EXE 资产。

## 模块与使用方法

### 1. 链接下载

适用于 yt-dlp 能访问的公开媒体链接，常见平台包括 YouTube、SoundCloud、哔哩哔哩、Vimeo、Bandcamp，以及部分 Spotify、网易云音乐、QQ 音乐和 Apple Music 页面。不同平台可能要求登录、Cookie 或地区可用性；付费 Apple Music / Beatport 请使用专用模块。

使用步骤：每行粘贴一个链接（也可用空格分隔）→ 可先点“解析第一条”检查 → 选格式和目录 → 点“开始下载”。

`FLAC/WAV` 是无损容器；如果源文件本身是 MP3/AAC/Opus，转换不会增加原始音质。默认不展开播放列表。

### 2. 本地转码

支持 MP3、WAV、FLAC、M4A、AAC、OGG、OPUS、WMA、AIFF、APE、ALAC 等常见扩展名，目标格式为 FLAC 或 WAV。会保留源文件，同名输出自动加 `_1`、`_2` 后缀，失败时不会破坏已有目标文件。

### 3. NCM / QQ 音乐解密

- **网易云 NCM**：还原客户端下载得到的 `.ncm` 内嵌 FLAC/MP3 音频，并尽力写入歌曲名、歌手、专辑和封面。
- **QQ 音乐**：支持旧格式 `.qmc0/.qmc3/.qmcflac/.qmcogg`（整文件静态异或）与新格式 `.mflac/.mflac0/.mflac1/.mgg/.mgg0/.mgg1/.mggl/.mmp4`（尾部内嵌密钥：TEA 保护 + Mask128 / 强化 RC4），自动识别长度前缀与 QTag 两种尾部，并在输出前校验音频头。新版 `STag` / `musicex` 格式不内嵌离线密钥，会给出明确提示。
- 全部在本地完成、不上传任何文件，请仅用于你合法获得的文件。

`qmc_decrypt.py` 为自包含纯 Python 实现；解密结果已与两个独立开源实现（MusicDecrypto、libtakiyasha）逐字节交叉验证，回归夹具见 `tests/data/qmc/`（由 `tools/make_qmc_fixtures.py` 生成的合成数据）。

### 4. Apple Music / Beatport

此页只服务于你自己的官方订阅和授权。

- **Apple Music**：在浏览器登录 `music.apple.com`，用 Cookie 导出扩展导出 Netscape 格式 `cookies.txt`，选择文件、粘贴链接并选择编码。
- **Beatport**：填写自己的 Beatport 流媒体订阅账号，选择与你的方案匹配的音质（FLAC 与 AAC 256 需 Professional，AAC 128 需 Advanced，HLS 需 Essential）。配置写入 `%APPDATA%\ShadowMusicGrabber`。
- **凭据自检**：下载前可点此按钮实时校验 Apple cookies 与订阅状态、Beatport 账号密码是否可用，结果写入日志并弹窗提示。

> Beatport 下载依赖第三方命令行工具 `beatportdl.exe`。出于体积与再分发考虑，仓库不包含该文件；需要该功能时请自行获取并放入仓库的 `bin\` 目录（打包脚本会自动把它并入 EXE）。缺少它时其他模块不受影响。

## 构建 EXE

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --clean ShadowMusicGrabber.spec
```

生成物为 `dist\ShadowMusicGrabber.exe`。spec 会收集 yt-dlp、customtkinter、gamdl、Crypto、mutagen；`bin\beatportdl.exe` 存在时一并打包。FFmpeg 不复制进 EXE，请确保目标机器可找到它，或把 `ffmpeg.exe` / `ffprobe.exe` 放在 EXE 同目录。

## 发布新版本

1. 修改 `main.py` 里的 `APP_VERSION`（例如 `1.8.1`）。
2. 跑测试：`python -m unittest discover tests`。
3. 打包：`python -m PyInstaller --noconfirm --clean ShadowMusicGrabber.spec`。
4. 在 GitHub 建一个**同名 tag** 的 Release（例如 `v1.8.1`），把 `dist\ShadowMusicGrabber.exe` 传上去。

> 程序内的「检查更新」只看 Releases，所以版本号、tag、Release 三者保持一致即可。
> 想改成自动化（推 tag 自动跑测试+打包+发布）也可以：在 `.github/workflows/` 放一个 `on: push: tags: ["v*"]` 的工作流即可，
> 但用于推送的 token 需要额外勾选 `workflow` 权限。

## 测试

```powershell
python -m py_compile main.py converter.py downloader.py ncm_decrypt.py qmc_decrypt.py premium.py updater.py i18n.py
python -m unittest discover tests -v
```

覆盖 URL 校验与平台识别、yt-dlp 选项、FFmpeg/ffprobe 定位、24-bit 音频转码、原子输出、NCM 封面段解析与 C 层 XOR 解密、元数据写入、**QQ 音乐 QMC 解密（v1/v2 全格式、真实测试向量、尾部/密钥链异常）**、Premium 参数校验与凭据自检、更新模块的版本比较/限流兜底，以及界面文案的翻译覆盖率。

## 目录

```text
ShadowMusicGrabber/
├── main.py                    # customtkinter GUI(更新入口 + 语言切换)
├── i18n.py                    # 中英文界面文案与自动翻译
├── updater.py                 # GitHub Releases 更新检查/下载/替换
├── downloader.py              # yt-dlp 链接下载与解析
├── converter.py               # FFmpeg 本地转码
├── ncm_decrypt.py             # 网易云 .ncm 本地还原
├── qmc_decrypt.py             # QQ 音乐 QMC 本地还原(全格式)
├── premium.py                 # gamdl / beatportdl 授权调用(含凭据自检)
├── ShadowMusicGrabber.spec    # PyInstaller 配置
├── assets/                    # 图标(icon.png / icon.ico)
├── tools/make_icon.py         # 图标生成脚本
├── tools/make_qmc_fixtures.py # QMC 测试夹具生成脚本
├── bin/                       # 可选第三方工具(beatportdl.exe)
├── tests/                     # 单元测试(含 tests/data/qmc 夹具)
└── LICENSE                    # MIT
```
