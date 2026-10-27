# -*- coding: utf-8 -*-
"""可取消的并行批次执行器。

从 GUI 的 worker 里抽出来,不依赖任何界面组件:
- 事件元组通过 ``publish`` 回调发给调用方(GUI 里就是 ui_queue.put);
- 每个任务收尾(完成/失败/取消)都会调用一次 ``end_job``,
  与 App._begin_job 的计数严格对账;
- 取消后:进行中的任务跑完当前文件抛 :class:`BatchCancelled`,
  尚未开始的任务直接标记为已取消,批次立即收尾并给出统计。
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

# 任务对象是鸭子类型: 只要求有 status / error 属性(与 *_Task 数据类一致)。
STATUS_DONE = "完成"
STATUS_FAILED = "失败"
STATUS_CANCELLED = "已取消"


class BatchCancelled(Exception):
    """run_one 内部检测到取消标志时抛出,由执行器统一转为"已取消"。"""


class BatchExecutor:
    """一批任务 + 一个取消开关 + N 个 daemon 工作线程。

    workers 由调用方给出;publish(event_tuple) 用于向 UI 线程投递事件,
    典型事件: ``(total_kind, (done, total))`` / ``("batch_summary", name, stats)``。
    """

    def __init__(
        self,
        *,
        name: str,
        workers: int,
        publish: Callable[[tuple], None],
        end_job: Callable[[str], None],
        total_kind: str = "",
        refresh_kind: str = "",
        logger=None,
    ):
        self.name = name
        self.workers = max(1, workers)
        self._publish = publish
        self._end_job = end_job
        # GUI 事件前缀由调用方显式给出(如 convert 任务用 cv_total/cv_refresh)。
        self._total_kind = total_kind or f"{name}_total"
        self._refresh_kind = refresh_kind
        self._logger = logger
        self.cancel_event = threading.Event()
        self.stats = {"total": 0, "succeeded": 0, "failed": 0, "cancelled": 0}

    def request_cancel(self) -> None:
        self.cancel_event.set()

    def run(self, jobs, run_one: Callable, label_fn: Optional[Callable] = None) -> dict:
        """阻塞执行到整批收尾(含取消收尾),返回统计 dict。"""
        jobs = list(jobs)
        total = len(jobs)
        self.stats = {
            "total": total, "succeeded": 0, "failed": 0, "cancelled": 0,
            "failed_labels": [],
        }
        work: "queue.Queue" = queue.Queue()
        for job in jobs:
            work.put(job)
        lock = threading.Lock()
        state = {"done": 0}

        def finish_one(task, status: str, error: str = ""):
            task.status = status
            task.error = error
            if self._refresh_kind:
                self._publish((self._refresh_kind, task))
            with lock:
                state["done"] += 1
                done = state["done"]
            self._publish((self._total_kind, (done, total)))
            self._end_job(self.name)

        def take_one():
            while True:
                if self.cancel_event.is_set():
                    # 把剩余任务全部标记为取消(每个都要对账一次 end_job)。
                    while True:
                        try:
                            task = work.get_nowait()
                        except queue.Empty:
                            return
                        with lock:
                            self.stats["cancelled"] += 1
                        finish_one(task, STATUS_CANCELLED)
                try:
                    task = work.get_nowait()
                except queue.Empty:
                    return
                if self.cancel_event.is_set():
                    with lock:
                        self.stats["cancelled"] += 1
                    finish_one(task, STATUS_CANCELLED)
                    continue
                try:
                    run_one(task)
                    with lock:
                        self.stats["succeeded"] += 1
                    finish_one(task, STATUS_DONE)
                except BatchCancelled:
                    with lock:
                        self.stats["cancelled"] += 1
                    finish_one(task, STATUS_CANCELLED)
                    self.request_cancel()
                except Exception as exc:  # noqa: BLE001 - 单任务异常不拖垮整批
                    if self._logger:
                        try:
                            self._logger.exception("batch task failed: %r", task)
                        except Exception:  # noqa: BLE001
                            pass
                    with lock:
                        self.stats["failed"] += 1
                        if label_fn is not None:
                            try:
                                self.stats["failed_labels"].append(str(label_fn(task)))
                            except Exception:  # noqa: BLE001
                                pass
                    finish_one(task, STATUS_FAILED, str(exc))

        threads = [
            threading.Thread(target=take_one, daemon=True, name=f"batch-{self.name}-{i}")
            for i in range(min(self.workers, max(1, total)))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self._publish(("batch_summary", self.name, dict(self.stats)))
        return dict(self.stats)
