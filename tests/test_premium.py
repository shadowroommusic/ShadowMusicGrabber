# -*- coding: utf-8 -*-
"""premium 模块测试:URL 识别、参数校验、错误路径。"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import premium  # noqa: E402


class TestUrlDetection(unittest.TestCase):
    def test_apple_music(self):
        self.assertTrue(premium.is_apple_music_url("https://music.apple.com/us/album/x/123?i=456"))
        self.assertFalse(premium.is_apple_music_url("https://www.beatport.com/track/x/1"))
        self.assertFalse(premium.is_apple_music_url("https://soundcloud.com/a/b"))

    def test_beatport(self):
        self.assertTrue(premium.is_beatport_url("https://www.beatport.com/track/strobe/1696999"))
        self.assertTrue(premium.is_beatport_url("https://www.beatport.com/release/x/1"))
        self.assertFalse(premium.is_beatport_url("https://music.apple.com/us/song/1"))

    def test_case_insensitive(self):
        self.assertTrue(premium.is_apple_music_url("HTTPS://MUSIC.APPLE.COM/US/SONG/1"))

    def test_reject_lookalike_hosts(self):
        self.assertFalse(premium.is_apple_music_url("https://music.apple.com.attacker.example/song/1"))
        self.assertFalse(premium.is_beatport_url("https://beatport.com.attacker.example/track/x/1"))


class TestBeatportdlDiscovery(unittest.TestCase):
    @unittest.skipUnless(
        os.path.isfile(
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "bin",
                "beatportdl.exe",
            )
        ),
        "仓库不包含第三方 beatportdl.exe，从源码树找不到时跳过",
    )
    def test_bundled_bin_exists(self):
        exe = premium._find_beatportdl()
        self.assertIsNotNone(exe, "应能找到 beatportdl")
        self.assertTrue(os.path.exists(exe), f"路径应存在: {exe}")


class TestGamdlErrors(unittest.TestCase):
    def test_non_apple_url(self):
        with self.assertRaises(premium.PremiumError):
            premium.gamdl_download("https://example.com/x", "cookies.txt", "out")

    def test_missing_cookies(self):
        with self.assertRaises(premium.PremiumError) as ctx:
            premium.gamdl_download(
                "https://music.apple.com/us/song/1", "/nonexistent/cookies.txt", "out")
        self.assertIn("cookies", str(ctx.exception))


class TestBeatportErrors(unittest.TestCase):
    def test_non_beatport_url(self):
        with self.assertRaises(premium.PremiumError):
            premium.beatportdl_download("https://example.com/x", "u", "p", "out")

    def test_missing_credentials(self):
        with self.assertRaises(premium.PremiumError):
            premium.beatportdl_download(
                "https://www.beatport.com/track/x/1", "", "", "out")

    def test_quality_map(self):
        # 显示名 -> 值 -> 订阅档 映射完整
        self.assertEqual(premium.BEATPORT_QUALITIES["FLAC 无损 (44.1kHz)"], ("lossless", "Professional"))
        self.assertEqual(premium.GAMDL_CODECS["AAC 256kbps (推荐, 无需额外配置)"], "aac-web")
        self.assertEqual(premium.GAMDL_CODECS["ALAC 无损 24bit/192kHz (需 wrapper 服务)"], "alac")


class TestBeatportProcess(unittest.TestCase):
    def test_uses_quit_mode_and_unblocks_fatal_pause(self):
        class FakeStdin:
            def __init__(self):
                self.writes = []
                self.flushed = False
                self.closed = False

            def write(self, value):
                self.writes.append(value)

            def flush(self):
                self.flushed = True

            def close(self):
                self.closed = True

        class FakeProcess:
            def __init__(self):
                self.stdin = FakeStdin()
                self.stdout = FakeStdout()

            def wait(self):
                return 1

        class FakeStdout:
            def __iter__(self):
                return iter(())

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as td:
            proc = FakeProcess()
            with patch.object(premium, "_find_beatportdl", return_value="beatportdl.exe"), \
                 patch.object(premium, "_app_data_dir", return_value=td), \
                 patch.object(premium.subprocess, "Popen", return_value=proc) as popen:
                with self.assertRaises(premium.PremiumError):
                    premium.beatportdl_download(
                        "https://www.beatport.com/track/strobe/1696999",
                        "user",
                        "password",
                        os.path.join(td, "downloads"),
                    )

        self.assertEqual(popen.call_args.args[0], ["beatportdl.exe", "-q", "https://www.beatport.com/track/strobe/1696999"])
        self.assertEqual(proc.stdin.writes, ["\n"])
        self.assertTrue(proc.stdin.flushed)
        self.assertTrue(proc.stdin.closed)


class _FakeResponse:
    """凭据自检测试用的假 HTTP 响应。"""

    def __init__(self, status_code=200, text="", payload=None, cookies=()):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.cookies = list(cookies)

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeCookie:
    def __init__(self, name, value):
        self.name = name
        self.value = value


def _write_cookies_file(path, include_token=True):
    lines = ["# Netscape HTTP Cookie File"]
    if include_token:
        lines.append(".music.apple.com\tTRUE\t/\tTRUE\t1999999999\tmedia-user-token\tTOKEN-VALUE-123")
    lines.append(".music.apple.com\tTRUE\t/\tTRUE\t1999999999\titspod\tXYZ")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def _apple_fake_get(account_response, calls=None):
    def fake_get(url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs))
        if url == premium._APPLE_HOMEPAGE_URL:
            return _FakeResponse(200, text='<script src="/assets/index~abc123.js"></script>')
        if "/assets/" in url:
            return _FakeResponse(200, text='"eyJhbGciOiJFUzI1NiJ9.eyJpc3MiOiJ0ZXN0In0.c2lnbmF0dXJl"')
        if url == premium._APPLE_ACCOUNT_INFO_API:
            return account_response
        raise AssertionError(f"unexpected url: {url}")

    return fake_get


class TestAppleCredentialCheck(unittest.TestCase):
    def test_missing_cookies_file(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(premium.PremiumError):
                premium.check_apple_music_credentials(os.path.join(td, "nope.txt"))

    def test_cookies_without_media_user_token(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_cookies_file(os.path.join(td, "cookies.txt"), include_token=False)
            with self.assertRaises(premium.PremiumError) as ctx:
                premium.check_apple_music_credentials(path)
            self.assertIn("media-user-token", str(ctx.exception))

    def test_expired_cookies(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_cookies_file(os.path.join(td, "cookies.txt"))
            with patch("premium.requests.get", side_effect=_apple_fake_get(_FakeResponse(401, payload={}))):
                with self.assertRaises(premium.PremiumError) as ctx:
                    premium.check_apple_music_credentials(path)
            self.assertIn("失效", str(ctx.exception))

    def test_no_active_subscription(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_cookies_file(os.path.join(td, "cookies.txt"))
            account = _FakeResponse(200, payload={"meta": {"subscription": {"active": False}}})
            with patch("premium.requests.get", side_effect=_apple_fake_get(account)):
                with self.assertRaises(premium.PremiumError) as ctx:
                    premium.check_apple_music_credentials(path)
            self.assertIn("订阅", str(ctx.exception))

    def test_success_sends_media_user_token(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_cookies_file(os.path.join(td, "cookies.txt"))
            account = _FakeResponse(
                200, payload={"meta": {"subscription": {"active": True, "storefront": "us"}}}
            )
            calls = []
            with patch("premium.requests.get", side_effect=_apple_fake_get(account, calls)):
                msg = premium.check_apple_music_credentials(path)
        self.assertIn("凭据有效", msg)
        self.assertIn("US", msg)
        account_calls = [kwargs for url, kwargs in calls if url == premium._APPLE_ACCOUNT_INFO_API]
        self.assertEqual(len(account_calls), 1)
        headers = account_calls[0]["headers"]
        self.assertEqual(headers["cookie"], "media-user-token=TOKEN-VALUE-123")
        self.assertTrue(headers["authorization"].startswith("Bearer eyJ"))


class TestBeatportCredentialCheck(unittest.TestCase):
    def test_empty_credentials(self):
        with self.assertRaises(premium.PremiumError):
            premium.check_beatport_credentials("", "")
        with self.assertRaises(premium.PremiumError):
            premium.check_beatport_credentials("user", "")

    def test_success(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return _FakeResponse(200, cookies=[_FakeCookie("sessionid", "s3cr3t")])

        with patch("premium.requests.post", side_effect=fake_post):
            msg = premium.check_beatport_credentials("user@example.com", "pw")
        self.assertIn("凭据有效", msg)
        self.assertEqual(captured["url"], premium._BEATPORT_LOGIN_API)
        self.assertEqual(captured["json"], {"username": "user@example.com", "password": "pw"})

    def test_wrong_password(self):
        with patch("premium.requests.post", return_value=_FakeResponse(400, payload={"detail": "bad"})):
            with self.assertRaises(premium.PremiumError) as ctx:
                premium.check_beatport_credentials("user", "pw")
        self.assertIn("账号或密码错误", str(ctx.exception))

    def test_rate_limited(self):
        with patch("premium.requests.post", return_value=_FakeResponse(429)):
            with self.assertRaises(premium.PremiumError):
                premium.check_beatport_credentials("user", "pw")

    def test_success_without_session_cookie_is_error(self):
        with patch("premium.requests.post", return_value=_FakeResponse(200, cookies=[])):
            with self.assertRaises(premium.PremiumError):
                premium.check_beatport_credentials("user", "pw")

    def test_network_error(self):
        with patch("premium.requests.post", side_effect=OSError("boom")):
            with self.assertRaises(premium.PremiumError):
                premium.check_beatport_credentials("user", "pw")


if __name__ == "__main__":
    unittest.main()
