# -*- coding: utf-8 -*-
"""MCP server 测试: 用真实子进程走 stdio 协议(initialize / tools/list / tools/call)。"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(PROJ, "mcp_server.py")


class McpProtocolTests(unittest.TestCase):
    """端到端: 启动 mcp_server.py, 按 MCP 的行分隔 JSON-RPC 协议对话。"""

    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=PROJ,
        )

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.stdin.close()
            cls.proc.wait(timeout=10)
        except Exception:
            cls.proc.kill()

    def _request(self, method, params=None, msg_id=1):
        payload = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        self.assertTrue(line, "server closed stdout unexpectedly")
        return json.loads(line)

    def _call_tool(self, name, arguments=None, msg_id=100):
        reply = self._request(
            "tools/call", {"name": name, "arguments": arguments or {}}, msg_id
        )
        self.assertNotIn("error", reply, reply)
        result = reply["result"]
        text = result["content"][0]["text"]
        return result, text

    # ------------------------------------------------------------ protocol

    def test_initialize_reports_server_info(self):
        reply = self._request("initialize", {"protocolVersion": "2024-11-05"})
        result = reply["result"]
        self.assertIn("protocolVersion", result)
        self.assertEqual(result["serverInfo"]["name"], "shadow-musicgrabber")
        self.assertIn("tools", result["capabilities"])

    def test_tools_list_exposes_core_tools(self):
        reply = self._request("tools/list")
        names = {tool["name"] for tool in reply["result"]["tools"]}
        for expected in (
            "get_capabilities",
            "probe_audio",
            "decrypt_audio",
            "decrypt_many",
            "convert_audio",
            "probe_url",
            "download_audio",
        ):
            self.assertIn(expected, names)
        for tool in reply["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_unknown_method_returns_jsonrpc_error(self):
        reply = self._request("no/such/method", msg_id=9)
        self.assertEqual(reply["error"]["code"], -32601)

    def test_notification_without_id_gets_no_reply(self):
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        self.proc.stdin.flush()
        # 紧跟一个正常请求: 若服务器为通知乱回消息, 这里拿到的就不是它。
        reply = self._request("tools/list", msg_id=11)
        self.assertEqual(reply["id"], 11)

    # ----------------------------------------------------------------- tools

    def test_get_capabilities(self):
        _result, text = self._call_tool("get_capabilities")
        info = json.loads(text)
        self.assertEqual(info["server"]["name"], "shadow-musicgrabber")
        self.assertIn(".ncm", info["encrypted_inputs"])
        self.assertIn(".mflac", info["encrypted_inputs"])
        for fmt in ("flac", "wav", "mp3"):
            self.assertIn(fmt, info["conversion_targets"])

    def test_probe_audio_missing_file_is_tool_error(self):
        result, text = self._call_tool(
            "probe_audio", {"path": os.path.join(PROJ, "no-such-file.flac")}
        )
        self.assertTrue(result.get("isError"))
        self.assertIn("does not exist", text)

    def test_decrypt_audio_rejects_unsupported_extension(self):
        _, text = self._call_tool(
            "decrypt_audio", {"path": os.path.join(PROJ, "README.md")}
        )
        self.assertIn("Unsupported encrypted file", text)

    def test_decrypt_audio_rejects_unknown_output_format(self):
        _, text = self._call_tool(
            "convert_audio",
            {"path": os.path.join(PROJ, "README.md"), "output_format": "aiff"},
        )
        self.assertIn("Unsupported output_format", text)

    def test_convert_audio_requires_concrete_format(self):
        _, text = self._call_tool(
            "convert_audio",
            {"path": os.path.join(PROJ, "README.md"), "output_format": "original"},
        )
        self.assertIn("output_format", text)


class SampleRateArgumentTests(unittest.TestCase):
    """sample_rate 参数：缺省保持原采样率，非法值必须报清晰的 ToolError。"""

    def test_omitted_means_keep_source(self):
        import mcp_server
        self.assertIsNone(mcp_server._check_sample_rate(None))

    def test_blank_and_zero_mean_keep_source(self):
        import mcp_server
        self.assertIsNone(mcp_server._check_sample_rate(""))
        self.assertIsNone(mcp_server._check_sample_rate(0))
        self.assertIsNone(mcp_server._check_sample_rate("0"))

    def test_all_supported_rates_accepted(self):
        import converter
        import mcp_server
        for rate in converter.SAMPLE_RATE_CHOICES:
            self.assertEqual(mcp_server._check_sample_rate(rate), rate)
            self.assertEqual(mcp_server._check_sample_rate(str(rate)), rate)

    def test_absurd_rate_is_rejected(self):
        import mcp_server
        for bad in (123, "abc", 999999, -44100, []):
            with self.assertRaises(mcp_server.ToolError, msg=f"应拒绝 {bad!r}"):
                mcp_server._check_sample_rate(bad)

    def test_tool_rejects_bad_rate_with_helpful_message(self):
        import mcp_server
        with self.assertRaises(mcp_server.ToolError) as ctx:
            mcp_server.tool_convert_audio(
                {
                    "path": os.path.join(PROJ, "README.md"),
                    "output_format": "flac",
                    "sample_rate": 12345,
                }
            )
        message = str(ctx.exception)
        self.assertIn("sample_rate", message)
        self.assertIn("44100", message, "报错应列出可选采样率")

    def test_schema_exposes_sample_rate_on_audio_tools(self):
        import mcp_server
        specs = {spec["name"]: spec for spec, _ in mcp_server._TOOL_SPECS}
        for name in ("convert_audio", "decrypt_audio", "decrypt_many", "download_audio"):
            props = specs[name]["inputSchema"]["properties"]
            self.assertIn("sample_rate", props, f"{name} 缺少 sample_rate")
            self.assertEqual(props["sample_rate"]["type"], "integer")


class DecryptRetryTests(unittest.TestCase):
    """批量解密的偶发失败重试(直接测函数, 不依赖真实文件)。"""

    def test_decrypt_many_retries_once_then_succeeds(self):
        import mcp_server

        calls = []

        def flaky(src, out_dir, fmt, reserved, sample_rate=None):
            calls.append(src)
            if len(calls) == 1:
                raise RuntimeError("transient ffmpeg failure")
            return {"source": src, "output": "ok"}

        with mock.patch.object(mcp_server, "_collect_encrypted", return_value=["a.ncm"]), \
             mock.patch.object(mcp_server, "_decrypt_one", side_effect=flaky):
            result = mcp_server.tool_decrypt_many({"paths": ["a.ncm"]})

        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(len(calls), 2, "应恰好重试一次")

    def test_decrypt_many_reports_error_after_two_failures(self):
        import mcp_server

        with mock.patch.object(mcp_server, "_collect_encrypted", return_value=["a.ncm"]), \
             mock.patch.object(
                 mcp_server, "_decrypt_one", side_effect=RuntimeError("boom")
             ) as patched:
            result = mcp_server.tool_decrypt_many({"paths": ["a.ncm"]})

        self.assertEqual(result["failed"], 1)
        self.assertEqual(patched.call_count, 2)
        self.assertIn("boom", result["items"][0]["error"])


if __name__ == "__main__":
    unittest.main()
