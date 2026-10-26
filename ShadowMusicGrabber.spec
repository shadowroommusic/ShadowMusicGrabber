# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all

datas = [('assets/icon.ico', 'assets')]
binaries = []
# beatportdl.exe 是第三方下载器, 不随仓库分发; 本地存在时才打包进 EXE。
_beatportdl = os.path.join("bin", "beatportdl.exe")
if os.path.isfile(_beatportdl):
    binaries.append((_beatportdl, "bin"))
# fpcalc(Chromaprint) 用于声学指纹识别, 同样是外部二进制; 放了才打包。
# 下载: https://github.com/acoustid/chromaprint/releases (chromaprint-fpcalc-*-windows-x86_64.zip)
_fpcalc = os.path.join("bin", "fpcalc.exe")
if os.path.isfile(_fpcalc):
    binaries.append((_fpcalc, "bin"))
hiddenimports = []
tmp_ret = collect_all('yt_dlp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('gamdl')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('Crypto')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('mutagen')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# 拖放支持: 只声明模块, tkdnd 的平台库/tcl 由 pyinstaller-hooks-contrib 的
# 官方 hook 自动按当前平台收集, 避免把其它平台的二进制也塞进来。
hiddenimports += ['tkinterdnd2', 'tkinterdnd2.TkinterDnD']
# gamdl imports its Click entry point dynamically in the frozen build.
hiddenimports += ['gamdl.cli', 'gamdl.cli.cli', 'Crypto.Cipher.AES', 'Crypto.Util.Padding', 'mutagen.flac']


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ShadowMusicGrabber',
    # .ico 只有 Windows 的 EXE 会内嵌;mac/linux 上传 None 以免告警。
    icon='assets/icon.ico' if os.name == 'nt' else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
