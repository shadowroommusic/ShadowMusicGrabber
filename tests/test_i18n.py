# -*- coding: utf-8 -*-
"""界面翻译：对照表完整性、回读、语言切换，以及英文模式下的界面构建测试。"""

import ast
import pathlib
import unittest

import i18n

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_SOURCES = ["main.py", "downloader.py", "converter.py", "ncm_decrypt.py", "premium.py", "updater.py"]


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _translatable_literals(path: pathlib.Path):
    """列出源码里应当有英文译文的界面文案（跳过文档字符串与 f-string 片段）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    skip.add(id(body[0].value))
        elif isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    skip.add(id(value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            if _has_cjk(node.value):
                yield node.lineno, node.value


class TranslateTests(unittest.TestCase):
    def setUp(self):
        self._original = i18n.current_language()

    def tearDown(self):
        i18n.set_language(self._original)

    def test_chinese_mode_returns_source(self):
        i18n.set_language(i18n.ZH)
        self.assertEqual(i18n.tr("检查更新"), "检查更新")

    def test_english_mode_translates_known_text(self):
        i18n.set_language(i18n.EN)
        self.assertEqual(i18n.tr("检查更新"), "Check for updates")
        self.assertEqual(i18n.tr("下载 · 转码 · 本地还原"), "Download · Convert · Decrypt")

    def test_unknown_text_is_returned_unchanged(self):
        i18n.set_language(i18n.EN)
        self.assertEqual(i18n.tr("C:/Music/track.flac"), "C:/Music/track.flac")

    def test_dynamic_messages_keep_their_variables(self):
        i18n.set_language(i18n.EN)
        self.assertEqual(i18n.tr("✓ 转码完成: C:/a.flac"), "✓ Converted: C:/a.flac")
        self.assertEqual(i18n.tr("已加入 3 个任务"), "Queued 3 task(s)")
        self.assertEqual(i18n.tr("下载中 42%"), "Downloading 42%")
        self.assertEqual(
            i18n.tr("✓ [YouTube] Some Song | 时长 210s | 3 个可用格式"),
            "✓ [YouTube] Some Song | duration 210s | 3 format(s) available",
        )

    def test_untr_maps_display_text_back_to_source(self):
        i18n.set_language(i18n.EN)
        self.assertEqual(i18n.untr("Check for updates"), "检查更新")
        self.assertEqual(i18n.untr("untranslated"), "untranslated")
        i18n.set_language(i18n.ZH)
        self.assertEqual(i18n.untr("检查更新"), "检查更新")


class TableCoverageTests(unittest.TestCase):
    """保证界面里的中文文案都有英文译文，新增文案忘了翻译会在这里失败。"""

    def setUp(self):
        self._original = i18n.current_language()
        i18n.set_language(i18n.EN)

    def tearDown(self):
        i18n.set_language(self._original)

    def test_every_ui_string_has_a_translation(self):
        missing = []
        for name in UI_SOURCES:
            path = PROJECT_ROOT / name
            if not path.is_file():
                continue
            for lineno, text in _translatable_literals(path):
                if i18n.tr(text) == text:
                    missing.append(f"{name}:{lineno}: {text[:60]}")
        self.assertEqual(missing, [], "以下文案缺少英文翻译：\n" + "\n".join(missing))

    def test_table_values_are_non_empty(self):
        for key, value in i18n._EN.items():
            self.assertTrue(key.strip(), "存在空的中文键")
            self.assertTrue(value.strip(), f"{key!r} 的译文为空")


class BilingualStartupTests(unittest.TestCase):
    """英文模式下界面必须能正常构建（下拉框取值曾误用翻译后的标签而崩溃）。"""

    def setUp(self):
        self._original = i18n.current_language()

    def tearDown(self):
        i18n.set_language(self._original)

    @staticmethod
    def _tk_available() -> bool:
        try:
            import tkinter

            root = tkinter.Tk()
            root.destroy()
            return True
        except Exception:  # noqa: BLE001 - 无显示环境时跳过
            return False

    def _build_app(self):
        if not self._tk_available():
            self.skipTest("当前环境没有可用的 Tk 显示")
        import main

        i18n.set_language(i18n.EN)
        app = main.App()
        app.withdraw()
        return app, main

    def test_app_builds_in_english_mode(self):
        app, main = self._build_app()
        try:
            expected_format = i18n.tr(main.FORMAT_LABELS[main.downloader.FormatKind.FLAC])
            self.assertEqual(app.fmt_var.get(), expected_format)
            self.assertEqual(app.lang_btn.cget("text"), "中文")
            self.assertTrue(app.am_codec_var.get())
            self.assertTrue(app.bp_quality_var.get())
        finally:
            app.destroy()

    def test_selected_format_round_trips_through_translation(self):
        app, main = self._build_app()
        try:
            app.fmt_var.set(i18n.tr(main.FORMAT_LABELS[main.downloader.FormatKind.MP3]))
            self.assertEqual(app._selected_download_format(), main.downloader.FormatKind.MP3)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
