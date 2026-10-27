# -*- coding: utf-8 -*-
"""v1.15 UI 功能的验收测试(全部无界面,不依赖 Tk 显示)。

覆盖:设置持久化(与语言键共存)、可取消的并行批次执行器
(取消对账、统计、失败标签、end_job 计数)。
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import appsettings  # noqa: E402
import batching  # noqa: E402
from batching import BatchCancelled, BatchExecutor  # noqa: E402


class _Task:
    def __init__(self, name):
        self.name = name
        self.status = "排队中"
        self.error = ""

    def __repr__(self):
        return f"<Task {self.name}>"


class AppSettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._tmp.name, "settings.json")
        patcher = mock.patch.object(appsettings, "settings_path", return_value=self._path)
        self._mocked = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_roundtrip_and_defaults(self):
        self.assertIsNone(appsettings.get("dl_out_dir"))
        self.assertEqual(appsettings.get("dl_out_dir", "D:/x"), "D:/x")
        self.assertTrue(appsettings.set("dl_out_dir", "D:/Music"))
        self.assertEqual(appsettings.get("dl_out_dir"), "D:/Music")

    def test_preserves_foreign_keys(self):
        # i18n 的 language 键不能被 UI 设置写掉
        with open(self._path, "w", encoding="utf-8") as fh:
            json.dump({"language": "en"}, fh)
        appsettings.set("cv_fmt", "wav")
        data = json.load(open(self._path, encoding="utf-8"))
        self.assertEqual(data["language"], "en")
        self.assertEqual(data["cv_fmt"], "wav")

    def test_corrupt_file_reads_as_empty(self):
        with open(self._path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(appsettings.get("cv_fmt"))
        self.assertTrue(appsettings.set("cv_fmt", "flac"))
        self.assertEqual(appsettings.get("cv_fmt"), "flac")


class BatchExecutorTests(unittest.TestCase):
    def _run(self, jobs, run_one, **kw):
        events = []
        end_jobs = []
        executor = BatchExecutor(
            name="test",
            workers=kw.pop("workers", 2),
            publish=events.append,
            end_job=lambda name: end_jobs.append(name),
            total_kind="t_total",
            refresh_kind="t_refresh",
        )
        for key, value in kw.items():
            setattr(executor, key, value)
        stats = executor.run(jobs, run_one, label_fn=lambda t: t.name)
        return executor, stats, events, end_jobs

    def test_all_succeed(self):
        jobs = [_Task(i) for i in range(5)]

        def run_one(task):
            task.status = "处理中…"

        executor, stats, events, end_jobs = self._run(jobs, run_one)
        self.assertEqual(stats["succeeded"], 5)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["cancelled"], 0)
        self.assertEqual(end_jobs, ["test"] * 5, "每个任务必须对账一次 end_job")
        self.assertTrue(all(t.status == "完成" for t in jobs))
        summaries = [e for e in events if e[0] == "batch_summary"]
        self.assertEqual(len(summaries), 1)
        totals = [e for e in events if e[0] == "t_total"]
        self.assertEqual(sorted(p for _, (p, _) in totals), [1, 2, 3, 4, 5])

    def test_failures_are_counted_and_labelled(self):
        jobs = [_Task(i) for i in range(3)]

        def run_one(task):
            if task.name != 1:
                raise RuntimeError("boom")

        _, stats, events, _ = self._run(jobs, run_one)
        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(stats["failed"], 2)
        self.assertEqual(stats["failed_labels"], ["0", "2"])
        failed = [t for t in jobs if t.status == "失败"]
        self.assertEqual(len(failed), 2)
        self.assertIn("boom", failed[0].error)

    def test_cancel_between_tasks_marks_rest_cancelled(self):
        jobs = [_Task(i) for i in range(6)]
        started = []

        def run_one(task):
            started.append(task.name)
            if len(started) == 2:
                executor.request_cancel()

        executor = BatchExecutor(
            name="test", workers=1, publish=lambda e: None,
            end_job=lambda n: None, total_kind="t_total", refresh_kind="t_refresh",
        )
        stats = executor.run(jobs, run_one, label_fn=lambda t: t.name)
        self.assertEqual(stats["succeeded"], 2)
        self.assertEqual(stats["cancelled"], 4, "未开始的任务应全部标记为已取消")
        self.assertEqual(stats["failed"], 0)
        cancelled = [t for t in jobs if t.status == "已取消"]
        self.assertEqual(len(cancelled), 4)

    def test_cancel_inside_task_via_batch_cancelled(self):
        jobs = [_Task(i) for i in range(4)]

        def run_one(task):
            if task.name == 0:
                raise BatchCancelled()

        executor = BatchExecutor(
            name="test", workers=1, publish=lambda e: None,
            end_job=lambda n: None, total_kind="t_total", refresh_kind="t_refresh",
        )
        stats = executor.run(jobs, run_one, label_fn=lambda t: t.name)
        self.assertEqual(stats["cancelled"], 4, "首个任务取消后,整批停止派新任务")
        self.assertEqual(stats["succeeded"], 0)

    def test_end_job_accounting_on_cancel(self):
        jobs = [_Task(i) for i in range(5)]
        end_jobs = []

        def run_one(task):
            executor.request_cancel()

        executor = BatchExecutor(
            name="test", workers=1, publish=lambda e: None,
            end_job=end_jobs.append, total_kind="t_total", refresh_kind="t_refresh",
        )
        executor.run(jobs, run_one, label_fn=lambda t: t.name)
        self.assertEqual(len(end_jobs), 5, "取消路径同样每个任务对账一次")


class TotalProgressTextTests(unittest.TestCase):
    """总进度标签文字:双语、失败计数。"""

    def setUp(self):
        self._original = __import__("i18n").current_language()

    def tearDown(self):
        __import__("i18n").set_language(self._original)

    def test_zh_text(self):
        import i18n
        import main

        i18n.set_language(i18n.ZH)
        self.assertEqual(main.format_total_text(3, 10, 0), "3/10 · 30%")
        self.assertEqual(main.format_total_text(3, 10, 2), "3/10 · 30% · 失败 2")
        self.assertEqual(main.format_total_text(0, 0, 0), "0/0 · 0%")

    def test_en_text(self):
        import i18n
        import main

        i18n.set_language(i18n.EN)
        self.assertEqual(main.format_total_text(3, 10, 0), "3/10 · 30%")
        self.assertEqual(main.format_total_text(3, 10, 2), "3/10 · 30% · failed 2")


if __name__ == "__main__":
    unittest.main()
