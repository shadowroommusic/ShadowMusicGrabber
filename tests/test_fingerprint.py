# -*- coding: utf-8 -*-
"""fingerprint 模块测试:全部离线(mock fpcalc 与 HTTP),不依赖真实网络或二进制。"""

import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fingerprint  # noqa: E402


class TestFindFpcalc(unittest.TestCase):
    def test_env_var_wins(self):
        with tempfile_dir() as td:
            exe = os.path.join(td, "myfpcalc.exe")
            open(exe, "wb").close()
            with mock.patch.dict(os.environ, {"FPCALC": exe}, clear=False):
                with mock.patch.object(fingerprint.shutil, "which", return_value=None):
                    self.assertEqual(fingerprint.find_fpcalc(), os.path.abspath(exe))

    def test_found_in_bin_subdir(self):
        name = "fpcalc.exe" if os.name == "nt" else "fpcalc"
        with tempfile_dir() as td:
            os.makedirs(os.path.join(td, "bin"), exist_ok=True)
            exe = os.path.join(td, "bin", name)
            open(exe, "wb").close()
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(fingerprint.shutil, "which", return_value=None), \
                     mock.patch.object(fingerprint.os.path, "dirname",
                                       return_value=td):
                    self.assertEqual(fingerprint.find_fpcalc(), os.path.abspath(exe))

    def test_returns_none_when_absent(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(fingerprint.shutil, "which", return_value=None), \
                 mock.patch.object(fingerprint.os.path, "isfile", return_value=False):
                self.assertIsNone(fingerprint.find_fpcalc())


class TestComputeFingerprint(unittest.TestCase):
    def _fake_run(self, payload, rc=0):
        return mock.Mock(
            returncode=rc,
            stdout=json.dumps(payload) if rc == 0 else "",
            stderr="" if rc == 0 else "boom",
        )

    def test_parses_json_output(self):
        with mock.patch.object(fingerprint, "find_fpcalc", return_value="fpcalc.exe"), \
             mock.patch.object(fingerprint.subprocess, "run",
                               return_value=self._fake_run({"duration": 180.5,
                                                            "fingerprint": "AQAB"})):
            duration, fp = fingerprint.compute_fingerprint("x.flac")
        self.assertEqual(duration, 180)
        self.assertEqual(fp, "AQAB")

    def test_missing_backend_raises(self):
        with mock.patch.object(fingerprint, "find_fpcalc", return_value=None):
            with self.assertRaises(fingerprint.MissingBackend):
                fingerprint.compute_fingerprint("x.flac")

    def test_nonzero_exit_raises(self):
        with mock.patch.object(fingerprint, "find_fpcalc", return_value="fpcalc.exe"), \
             mock.patch.object(fingerprint.subprocess, "run",
                               return_value=self._fake_run({}, rc=1)):
            with self.assertRaises(fingerprint.FingerprintError):
                fingerprint.compute_fingerprint("x.flac")

    def test_empty_fingerprint_raises(self):
        with mock.patch.object(fingerprint, "find_fpcalc", return_value="fpcalc.exe"), \
             mock.patch.object(fingerprint.subprocess, "run",
                               return_value=self._fake_run({"duration": 1,
                                                            "fingerprint": None})):
            with self.assertRaises(fingerprint.FingerprintError):
                fingerprint.compute_fingerprint("x.flac")

    def test_bad_json_raises(self):
        bad = mock.Mock(returncode=0, stdout="not json", stderr="")
        with mock.patch.object(fingerprint, "find_fpcalc", return_value="fpcalc.exe"), \
             mock.patch.object(fingerprint.subprocess, "run", return_value=bad):
            with self.assertRaises(fingerprint.FingerprintError):
                fingerprint.compute_fingerprint("x.flac")


class TestLookupAcoustid(unittest.TestCase):
    def test_missing_key_raises(self):
        with self.assertRaises(fingerprint.MissingApiKey):
            fingerprint.lookup_acoustid("AQAB", 100, "")

    def test_filters_by_score_and_sorts(self):
        payload = {
            "status": "ok",
            "results": [
                {"score": 0.3, "recordings": [{"id": "low", "title": "低分"}]},
                {"score": 0.95, "recordings": [{"id": "high", "title": "高分"}]},
            ],
        }
        with mock.patch.object(fingerprint, "_http_get_json", return_value=payload):
            out = fingerprint.lookup_acoustid("AQAB", 100, "key", min_score=0.5)
        self.assertEqual([r["id"] for r in out], ["high"])

    def test_error_status_raises(self):
        with mock.patch.object(fingerprint, "_http_get_json",
                               return_value={"status": "error",
                                             "error": {"message": "bad key"}}):
            with self.assertRaises(fingerprint.FingerprintError):
                fingerprint.lookup_acoustid("AQAB", 100, "key")

    def test_empty_results_is_empty_list(self):
        with mock.patch.object(fingerprint, "_http_get_json",
                               return_value={"status": "ok", "results": []}):
            self.assertEqual(fingerprint.lookup_acoustid("AQAB", 100, "key"), [])


class TestFetchRecording(unittest.TestCase):
    def test_parses_title_artist_album(self):
        payload = {
            "title": "稻香",
            "artist-credit": [
                {"artist": {"name": "周杰伦"}, "joinphrase": " & "},
                {"artist": {"name": "费玉清"}, "joinphrase": ""},
            ],
            "releases": [{"id": "rel-1", "title": "魔杰座"}],
        }
        with mock.patch.object(fingerprint, "_http_get_json", return_value=payload):
            info = fingerprint.fetch_recording("mbid-1")
        self.assertEqual(info["title"], "稻香")
        self.assertEqual(info["artist"], "周杰伦 & 费玉清")
        self.assertEqual(info["album"], "魔杰座")
        self.assertEqual(info["release_id"], "rel-1")


class TestIdentify(unittest.TestCase):
    def test_full_hit(self):
        with mock.patch.object(fingerprint, "compute_fingerprint",
                               return_value=(200, "AQAB")), \
             mock.patch.object(fingerprint, "lookup_acoustid",
                               return_value=[{"score": 0.99, "id": "mbid-1",
                                              "title": "稻香",
                                              "releases": [{"id": "rel-1",
                                                            "title": "魔杰座"}]}]), \
             mock.patch.object(fingerprint, "fetch_recording",
                               return_value={"title": "稻香", "artist": "周杰伦",
                                             "album": "魔杰座",
                                             "release_id": "rel-1"}):
            match = fingerprint.identify("x.flac", "key")
        self.assertTrue(match.found)
        self.assertEqual(match.title, "稻香")
        self.assertEqual(match.artist, "周杰伦")
        self.assertIn("rel-1", match.cover_url)

    def test_no_match_returns_empty_without_raising(self):
        with mock.patch.object(fingerprint, "compute_fingerprint",
                               return_value=(200, "AQAB")), \
             mock.patch.object(fingerprint, "lookup_acoustid", return_value=[]):
            match = fingerprint.identify("x.flac", "key")
        self.assertFalse(match.found)
        self.assertEqual(match.title, "")

    def test_musicbrainz_failure_still_returns_acoustid_data(self):
        with mock.patch.object(fingerprint, "compute_fingerprint",
                               return_value=(200, "AQAB")), \
             mock.patch.object(fingerprint, "lookup_acoustid",
                               return_value=[{"score": 0.9, "id": "mbid-x",
                                              "title": "备用标题",
                                              "releases": []}]), \
             mock.patch.object(fingerprint, "fetch_recording",
                               side_effect=fingerprint.FingerprintError("网络炸了")):
            match = fingerprint.identify("x.flac", "key")
        self.assertEqual(match.title, "备用标题")

    def test_on_line_receives_progress(self):
        lines = []
        with mock.patch.object(fingerprint, "compute_fingerprint",
                               return_value=(200, "AQAB")), \
             mock.patch.object(fingerprint, "lookup_acoustid", return_value=[]):
            fingerprint.identify("x.flac", "key", on_line=lines.append)
        self.assertTrue(any("AcoustID" in s for s in lines))


class TestDownloadCover(unittest.TestCase):
    def test_empty_url_returns_empty(self):
        self.assertEqual(fingerprint.download_cover(""), b"")

    def test_failure_returns_empty(self):
        import urllib.error
        with mock.patch.object(fingerprint.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("down")):
            self.assertEqual(fingerprint.download_cover("http://x/1.jpg"), b"")

    def test_success_returns_bytes(self):
        class Resp:
            def read(self_inner):
                return b"IMAGEDATA"

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        with mock.patch.object(fingerprint.urllib.request, "urlopen", return_value=Resp()):
            self.assertEqual(fingerprint.download_cover("http://x/1.jpg"), b"IMAGEDATA")


def tempfile_dir():
    import tempfile as _t
    return _t.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
