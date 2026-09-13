#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shadow MusicGrabber — MCP server (stdio transport).

Exposes the tool's core capabilities to AI agents:
  probe_audio / decrypt_audio / decrypt_many / convert_audio / probe_url / download_audio

Speaks JSON-RPC 2.0 over stdin/stdout and reuses this repository's own modules,
so no third-party MCP SDK is needed.

Run directly:     python mcp_server.py
Run through uvx:  uvx --from git+https://github.com/shadowroommusic/ShadowMusicGrabber shadow-musicgrabber-mcp
"""
from __future__ import annotations

import json
import os
import sys
import traceback
import warnings
from typing import Any, Callable, Optional

# yt-dlp 依赖的 requests 等库可能在导入时向 stderr 打版本警告, 对 MCP 无意义。
warnings.filterwarnings("ignore")

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import converter  # noqa: E402
import downloader  # noqa: E402
import ncm_decrypt  # noqa: E402
import qmc_decrypt  # noqa: E402

SERVER_NAME = "shadow-musicgrabber"
SERVER_VERSION = "1.9.1"  # keep in sync with pyproject.toml / main.py APP_VERSION
PROTOCOL_VERSION = "2024-11-05"

_ENCRYPTED_EXTS = {".ncm"} | set(qmc_decrypt.QMC_EXTS)
_OUTPUT_FORMATS = ("original", "flac", "wav", "mp3")
_DOWNLOAD_FORMATS = {
    "original": downloader.FormatKind.ORIGINAL,
    "flac": downloader.FormatKind.FLAC,
    "wav": downloader.FormatKind.WAV,
    "mp3": downloader.FormatKind.MP3,
}


class ToolError(Exception):
    """Error message that is safe to show to the caller as-is."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _default_output_dir() -> str:
    base = os.path.join(os.path.expanduser("~"), "Music", "ShadowMusicGrabber")
    os.makedirs(base, exist_ok=True)
    return base


def _resolve_out_dir(raw: Optional[str]) -> str:
    out = os.path.abspath(os.fspath(raw)) if raw else _default_output_dir()
    os.makedirs(out, exist_ok=True)
    return out


def _require_path(raw: Any) -> str:
    if not raw or not isinstance(raw, str):
        raise ToolError("A file path is required.")
    resolved = os.path.abspath(os.fspath(raw))
    if not os.path.exists(resolved):
        raise ToolError(f"Path does not exist: {resolved}")
    return resolved


def _check_format(raw: Any, allowed: tuple[str, ...], label: str) -> str:
    value = (raw or "original").strip().lower() if isinstance(raw, str) else "original"
    if value not in allowed:
        raise ToolError(f"Unsupported {label} {value!r}; use one of {list(allowed)}")
    return value


def _unique_path(directory: str, stem: str, ext: str, reserved: set[str]) -> str:
    candidate = os.path.join(directory, f"{stem}{ext}")
    index = 1
    while os.path.exists(candidate) or os.path.normcase(os.path.abspath(candidate)) in reserved:
        candidate = os.path.join(directory, f"{stem} ({index}){ext}")
        index += 1
    reserved.add(os.path.normcase(os.path.abspath(candidate)))
    return candidate


def _collect_encrypted(paths: Any) -> list[str]:
    """Expand files / directories into a stable, de-duplicated list."""
    if not isinstance(paths, list) or not paths:
        raise ToolError("`paths` must be a non-empty array of files or directories.")
    found: list[str] = []
    seen: set[str] = set()

    def remember(path: str) -> None:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            found.append(path)

    for raw in paths:
        target = _require_path(raw)
        if os.path.isdir(target):
            for root, _dirs, files in os.walk(target):
                for name in sorted(files):
                    if os.path.splitext(name)[1].lower() in _ENCRYPTED_EXTS:
                        remember(os.path.join(root, name))
        else:
            remember(target)
    if not found:
        raise ToolError(
            "No encrypted files found. Supported: .ncm and QMC "
            f"({', '.join(sorted(qmc_decrypt.QMC_EXTS))})."
        )
    return found


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def tool_get_capabilities(_args: dict) -> dict:
    ffmpeg = converter.find_ffmpeg()
    return {
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION, "platform": sys.platform},
        "ffmpeg": ffmpeg or None,
        "ffprobe": (converter.find_ffprobe(ffmpeg) or None) if ffmpeg else None,
        "encrypted_inputs": sorted(_ENCRYPTED_EXTS),
        "conversion_targets": ["flac", "wav", "mp3"],
        "default_output_dir": _default_output_dir(),
        "notes": "Decryption is lossless and offline; conversion and downloads need ffmpeg.",
    }


def tool_probe_audio(args: dict) -> dict:
    path = _require_path(args.get("path"))
    info: dict[str, Any] = {"path": path, "size_bytes": os.path.getsize(path)}
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path)
    except Exception as exc:  # pragma: no cover - depends on file content
        info["probe_error"] = f"{type(exc).__name__}: {exc}"
        return info
    if audio is None:
        info["probe_error"] = "Unrecognised or unsupported audio container."
        return info

    stream = getattr(audio, "info", None)
    if stream is not None:
        info["duration_seconds"] = round(float(getattr(stream, "length", 0.0) or 0.0), 2)
        bitrate = int(getattr(stream, "bitrate", 0) or 0)
        if bitrate:
            info["bitrate_kbps"] = round(bitrate / 1000)
        sample_rate = int(getattr(stream, "sample_rate", 0) or 0)
        if sample_rate:
            info["sample_rate"] = sample_rate
        channels = int(getattr(stream, "channels", 0) or 0)
        if channels:
            info["channels"] = channels

    tags = getattr(audio, "tags", None)
    if tags:
        def first(*names: str) -> str:
            for name in names:
                value = tags.get(name)
                if value:
                    text = value[0] if isinstance(value, (list, tuple)) else value
                    return str(text).strip()
            return ""

        title = first("title", "TIT2", "\xa9nam")
        artist = first("artist", "TPE1", "\xa9ART")
        album = first("album", "TALB", "\xa9alb")
        if title:
            info["title"] = title
        if artist:
            info["artist"] = artist
        if album:
            info["album"] = album
    info["container"] = type(audio).__name__
    return info


def _decrypt_one(src: str, out_dir: str, fmt: str, reserved: set[str]) -> dict:
    ext = os.path.splitext(src)[1].lower()
    if ext == ".ncm":
        target = ncm_decrypt.make_output_path(src, out_dir, reserved)
        raw, meta = ncm_decrypt.decrypt_file(src, target, ffmpeg=converter.find_ffmpeg())
    elif ext in qmc_decrypt.QMC_EXTS:
        target = qmc_decrypt.make_output_path(src, out_dir, reserved)
        raw, meta = qmc_decrypt.decrypt_file(src, target)
    else:
        raise ToolError(
            f"Unsupported encrypted file {ext!r}; supported: .ncm and QMC "
            f"({', '.join(sorted(qmc_decrypt.QMC_EXTS))})."
        )

    result: dict[str, Any] = {
        "source": src,
        "decrypted": raw,
        "engine": "ncm" if ext == ".ncm" else "qmc",
        "title": (getattr(meta, "music_name", "") or "").strip(),
    }
    artist = (getattr(meta, "artist", "") or "").strip()
    if artist:
        result["artist"] = artist

    if fmt != "original":
        stem = os.path.splitext(os.path.basename(raw))[0]
        target = _unique_path(out_dir, stem, f".{fmt}", reserved)
        converted = converter.convert_file(raw, target, fmt, ffmpeg=converter.find_ffmpeg())
        result["output"] = converted
        result["format"] = fmt
        if os.path.normcase(converted) != os.path.normcase(raw):
            result["decrypted_kept"] = raw
    else:
        result["output"] = raw
        result["format"] = "original"
    return result


def tool_decrypt_audio(args: dict) -> dict:
    src = _require_path(args.get("path"))
    if os.path.isdir(src):
        raise ToolError("`path` must be a file; use decrypt_many for directories.")
    fmt = _check_format(args.get("output_format"), _OUTPUT_FORMATS, "output_format")
    out_dir = _resolve_out_dir(args.get("output_dir")) if args.get("output_dir") else os.path.dirname(src)
    return _decrypt_one(src, out_dir, fmt, set())


def tool_decrypt_many(args: dict) -> dict:
    fmt = _check_format(args.get("output_format"), _OUTPUT_FORMATS, "output_format")
    out_dir = _resolve_out_dir(args.get("output_dir")) if args.get("output_dir") else None
    sources = _collect_encrypted(args.get("paths"))
    reserved: set[str] = set()
    items = []
    for src in sources:
        target_dir = out_dir or os.path.dirname(src)
        # 偶发失败(例如首次调用 ffmpeg 的瞬时问题)自动重试一次, 仍失败才记为错误。
        last_error: Optional[Exception] = None
        for _attempt in range(2):
            try:
                items.append(_decrypt_one(src, target_dir, fmt, reserved))
                last_error = None
                break
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            items.append(
                {"source": src, "error": f"{type(last_error).__name__}: {last_error}"}
            )
    failed = [item for item in items if "error" in item]
    return {
        "total": len(items),
        "succeeded": len(items) - len(failed),
        "failed": len(failed),
        "items": items,
    }


def tool_convert_audio(args: dict) -> dict:
    src = _require_path(args.get("path"))
    if os.path.isdir(src):
        raise ToolError("`path` must be a file.")
    fmt = _check_format(args.get("output_format"), ("flac", "wav", "mp3"), "output_format")
    out_dir = _resolve_out_dir(args.get("output_dir")) if args.get("output_dir") else os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    reserved: set[str] = {os.path.normcase(os.path.abspath(src))}
    target = _unique_path(out_dir, stem, f".{fmt}", reserved)
    produced = converter.convert_file(src, target, fmt, ffmpeg=converter.find_ffmpeg())
    return {"source": src, "output": produced, "format": fmt}


def _ytdlp_probe(url: str) -> dict:
    import yt_dlp

    options = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise ToolError("Could not extract any information for that URL.")
    entries = info.get("entries")
    if entries:
        info = next((entry for entry in entries if entry), info)
    return {
        "url": url,
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration_seconds": int(info.get("duration") or 0),
        "is_live": bool(info.get("is_live")),
        "platform": downloader.detect_platform(url),
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
    }


def tool_probe_url(args: dict) -> dict:
    url = args.get("url")
    if not isinstance(url, str) or not downloader.is_valid_url(url):
        raise ToolError(f"Invalid or unsupported URL: {url!r}")
    return _ytdlp_probe(url)


def tool_download_audio(args: dict) -> dict:
    url = args.get("url")
    if not isinstance(url, str) or not downloader.is_valid_url(url):
        raise ToolError(f"Invalid or unsupported URL: {url!r}")
    fmt = _check_format(args.get("output_format"), _OUTPUT_FORMATS, "output_format")
    out_dir = _resolve_out_dir(args.get("output_dir"))
    produced = downloader.download_url(
        url,
        out_dir,
        fmt=_DOWNLOAD_FORMATS[fmt],
        ffmpeg_location=converter.find_ffmpeg(),
        allow_playlist=bool(args.get("allow_playlist", False)),
    )
    return {"url": url, "output": produced, "format": fmt, "output_dir": out_dir}


# --------------------------------------------------------------------------
# tool registry
# --------------------------------------------------------------------------

_TOOL_SPECS: list[tuple[dict, Callable[[dict], dict]]] = [
    (
        {
            "name": "get_capabilities",
            "description": (
                "Report the environment this music tool runs in: version, platform, "
                "whether ffmpeg/ffprobe are available, supported encrypted formats "
                "(NCM / QQ Music QMC), conversion targets and the default output directory. "
                "Call this first to learn what the other tools can do here."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        tool_get_capabilities,
    ),
    (
        {
            "name": "probe_audio",
            "description": (
                "Read metadata of a local audio file without modifying it: duration, bitrate, "
                "sample rate, channels, container and tags (title/artist/album). "
                "Use it to inspect results before or after decrypting/converting."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Absolute path to an audio file."}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        tool_probe_audio,
    ),
    (
        {
            "name": "decrypt_audio",
            "description": (
                "Decrypt one encrypted Chinese music file (NetEase .ncm or QQ Music QMC: "
                ".qmc0/.qmc3/.qmcflac/.qmcogg/.mflac/.mgg/.mflac0/.mgg1/.mmp4 ...). "
                "Fully offline and lossless. Optionally convert the decrypted result to "
                "flac/wav/mp3 in the same step (requires ffmpeg). "
                "Use decrypt_many for batches or directories."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the encrypted file."},
                    "output_dir": {
                        "type": "string",
                        "description": "Directory for the output files. Defaults to the source file's directory.",
                    },
                    "output_format": {
                        "type": "string",
                        "enum": list(_OUTPUT_FORMATS),
                        "description": "original (keep decrypted format), flac, wav or mp3. Default: original.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        tool_decrypt_audio,
    ),
    (
        {
            "name": "decrypt_many",
            "description": (
                "Decrypt many encrypted files at once. Accepts files and directories "
                "(directories are searched recursively for .ncm and QMC files). "
                "Each item is reported separately, so partial failures are visible."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of files and/or directories.",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Directory for all outputs. Defaults to each source file's directory.",
                    },
                    "output_format": {
                        "type": "string",
                        "enum": list(_OUTPUT_FORMATS),
                        "description": "original, flac, wav or mp3. Default: original.",
                    },
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
        },
        tool_decrypt_many,
    ),
    (
        {
            "name": "convert_audio",
            "description": (
                "Convert a local audio file to flac, wav or mp3 with ffmpeg. "
                "Never overwrites the source file."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the source audio file."},
                    "output_format": {"type": "string", "enum": ["flac", "wav", "mp3"]},
                    "output_dir": {
                        "type": "string",
                        "description": "Directory for the output file. Defaults to the source directory.",
                    },
                },
                "required": ["path", "output_format"],
                "additionalProperties": False,
            },
        },
        tool_convert_audio,
    ),
    (
        {
            "name": "probe_url",
            "description": (
                "Resolve a public media URL (YouTube, Bilibili, SoundCloud ...) without "
                "downloading: returns title, uploader, duration and platform. "
                "Call it first to confirm the right track before downloading."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Public media page URL."}},
                "required": ["url"],
                "additionalProperties": False,
            },
        },
        tool_probe_url,
    ),
    (
        {
            "name": "download_audio",
            "description": (
                "Download the audio of a public media URL as flac / wav / mp3 / original "
                "through yt-dlp. Slow for long files — call probe_url first, and prefer "
                "single tracks over playlists."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "output_dir": {
                        "type": "string",
                        "description": "Directory for the download. Defaults to ~/Music/ShadowMusicGrabber.",
                    },
                    "output_format": {
                        "type": "string",
                        "enum": list(_OUTPUT_FORMATS),
                        "description": "original, flac, wav or mp3. Default: original.",
                    },
                    "allow_playlist": {
                        "type": "boolean",
                        "description": "Allow playlists (default false: only the single track).",
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
        tool_download_audio,
    ),
]

_TOOLS_BY_NAME: dict[str, Callable[[dict], dict]] = {
    spec["name"]: impl for spec, impl in _TOOL_SPECS
}


def _list_tools() -> list[dict]:
    return [spec for spec, _impl in _TOOL_SPECS]


# --------------------------------------------------------------------------
# JSON-RPC over stdio
# --------------------------------------------------------------------------

def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _send_result(msg_id: Any, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _send_error(msg_id: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def _content(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _call_tool(name: str, arguments: Any) -> dict:
    impl = _TOOLS_BY_NAME.get(name)
    if impl is None:
        return _content(f"Unknown tool: {name}", is_error=True)
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _content("`arguments` must be an object.", is_error=True)
    try:
        result = impl(arguments)
    except ToolError as exc:
        return _content(str(exc), is_error=True)
    except Exception as exc:  # surface the real reason instead of a bare failure
        detail = traceback.format_exc(limit=4)
        return _content(f"{type(exc).__name__}: {exc}\n{detail}", is_error=True)
    return _content(json.dumps(result, ensure_ascii=False, indent=2))


def _handle(message: dict) -> None:
    method = message.get("method")
    msg_id = message.get("id")

    if method == "initialize":
        _send_result(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
        return

    if method in ("notifications/initialized", "notifications/cancelled"):
        return  # notifications carry no id and expect no reply

    if method == "ping":
        _send_result(msg_id, {})
        return

    if method == "tools/list":
        _send_result(msg_id, {"tools": _list_tools()})
        return

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name") or ""
        _send_result(msg_id, _call_tool(name, params.get("arguments")))
        return

    if msg_id is not None:
        _send_error(msg_id, -32601, f"Method not found: {method}")


def main() -> int:
    # MCP stdio uses UTF-8; Windows consoles default to a legacy code page.
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # detached/redirected streams
                pass

    while True:
        try:
            line = sys.stdin.readline()
        except (KeyboardInterrupt, EOFError):
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            _handle(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
