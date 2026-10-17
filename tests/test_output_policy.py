# -*- coding: utf-8 -*-
"""输出路径策略：同名文件处理、可移动盘暂存、原子落盘。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import converter  # noqa: E402
import ncm_decrypt  # noqa: E402
import qmc_decrypt  # noqa: E402


class UniquePathTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def test_returns_same_path_when_free(self):
        path = os.path.join(self.dir, "song.flac")
        self.assertEqual(converter.unique_path(path), path)

    def test_appends_suffix_when_taken(self):
        first = os.path.join(self.dir, "song.flac")
        open(first, "wb").close()
        second = converter.unique_path(first)
        self.assertTrue(second.endswith("song_1.flac"))

    def test_place_file_copies_and_removes_source(self):
        src = os.path.join(self.dir, "staged.flac")
        with open(src, "wb") as fh:
            fh.write(b"audio")
        dest = os.path.join(self.dir, "out")

        final = converter.place_file(src, dest)

        self.assertEqual(final, os.path.join(dest, "staged.flac"))
        self.assertFalse(os.path.exists(src))
        with open(final, "rb") as fh:
            self.assertEqual(fh.read(), b"audio")

    def test_place_file_overwrite_replaces_target(self):
        src = os.path.join(self.dir, "staged.flac")
        with open(src, "wb") as fh:
            fh.write(b"new")
        dest = os.path.join(self.dir, "out")
        os.makedirs(dest)
        target = os.path.join(dest, "staged.flac")
        with open(target, "wb") as fh:
            fh.write(b"old")

        final = converter.place_file(src, dest, overwrite=True)

        self.assertEqual(final, target)
        with open(final, "rb") as fh:
            self.assertEqual(fh.read(), b"new")

    def test_place_file_keeps_existing_by_default(self):
        src = os.path.join(self.dir, "staged.flac")
        with open(src, "wb") as fh:
            fh.write(b"new")
        dest = os.path.join(self.dir, "out")
        os.makedirs(dest)
        with open(os.path.join(dest, "staged.flac"), "wb") as fh:
            fh.write(b"old")

        final = converter.place_file(src, dest)

        self.assertTrue(final.endswith("staged_1.flac"))

    def test_local_path_is_not_removable(self):
        self.assertFalse(converter.is_removable_path(os.getcwd()))


class OutputPathPolicyTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name
        self.src = os.path.join(self.dir, "song.ncm")
        open(self.src, "wb").close()
        open(os.path.join(self.dir, "song.flac"), "wb").close()

    def tearDown(self):
        self._td.cleanup()

    def test_ncm_avoid_existing_true_adds_suffix(self):
        out = ncm_decrypt.make_output_path(self.src, self.dir, avoid_existing=True)
        self.assertTrue(out.endswith("song_1.flac"))

    def test_ncm_avoid_existing_false_keeps_natural_name(self):
        out = ncm_decrypt.make_output_path(self.src, self.dir, avoid_existing=False)
        self.assertEqual(out, os.path.join(self.dir, "song.flac"))

    def test_qmc_avoid_existing_false_keeps_natural_name(self):
        src = os.path.join(self.dir, "track.qmcflac")
        open(src, "wb").close()
        open(os.path.join(self.dir, "track.flac"), "wb").close()
        out = qmc_decrypt.make_output_path(src, self.dir, avoid_existing=False)
        self.assertEqual(out, os.path.join(self.dir, "track.flac"))

    def test_track_filename_filters_illegal_chars(self):
        self.assertEqual(
            qmc_decrypt.track_filename("A/B", "C:D"), "A_B - C_D"
        )
        self.assertIsNone(qmc_decrypt.track_filename("A", ""))
