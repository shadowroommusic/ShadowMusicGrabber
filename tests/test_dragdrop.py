# -*- coding: utf-8 -*-
"""拖放模块测试：目标收集逻辑（不需要真实 Tk）与降级行为。"""
import unittest
from unittest import mock

import dragdrop


class FakeWidget:
    """最小替身：支持子控件与任意私有属性（模拟 CTk 的倒置结构）。"""

    def __init__(self, *children, **attrs):
        self._children = list(children)
        for key, value in attrs.items():
            setattr(self, key, value)

    def winfo_children(self):
        return list(self._children)


class DropTargetTests(unittest.TestCase):
    def test_collects_children_and_ctk_inner_windows(self):
        """必须覆盖 CTkScrollableFrame 的倒置结构：内容层/ canvas / 外层 frame / 任务行。"""
        leaf = FakeWidget()
        inner = FakeWidget(leaf)          # 队列里的任务行
        canvas = FakeWidget()             # 空队列时鼠标实际命中的窗口
        outer_frame = FakeWidget()        # canvas 的父容器
        scrollable = FakeWidget(
            inner, _parent_canvas=canvas, _parent_frame=outer_frame
        )

        targets = dragdrop._drop_targets(scrollable)

        self.assertIn(scrollable, targets)
        self.assertIn(canvas, targets)
        self.assertIn(outer_frame, targets)
        self.assertIn(inner, targets)
        self.assertIn(leaf, targets)

    def test_does_not_loop_on_self_references(self):
        """属性指回自身/互相引用时不能无限递归。"""
        a = FakeWidget()
        b = FakeWidget()
        a._parent_canvas = b
        b._parent_frame = a

        targets = dragdrop._drop_targets(a)

        self.assertEqual(targets.count(a), 1)
        self.assertEqual(targets.count(b), 1)

    def test_tolerates_objects_without_children_api(self):
        class Opaque:
            pass

        node = Opaque()
        self.assertEqual(dragdrop._drop_targets(node), [node])

    def test_tolerates_widgets_that_raise_on_children(self):
        class Angry(FakeWidget):
            def winfo_children(self):
                raise RuntimeError("已销毁")

        node = Angry()
        self.assertEqual(dragdrop._drop_targets(node), [node])


class AvailabilityTests(unittest.TestCase):
    def test_available_is_false_without_tkinterdnd2(self):
        with mock.patch.dict("sys.modules", {"tkinterdnd2": None}):
            self.assertFalse(dragdrop.available())

    def test_expand_paths_recurses_directories_and_filters(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            wanted = os.path.join(tmp, "a.ncm")
            ignored = os.path.join(tmp, "b.txt")
            open(wanted, "w").close()
            open(ignored, "w").close()

            found = dragdrop.expand_paths([tmp], {".ncm"})

            self.assertEqual(found, [wanted])


if __name__ == "__main__":
    unittest.main()
