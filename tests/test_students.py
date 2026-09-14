"""Unit tests for per-student routing in goddard_sync.py — stdlib unittest
only, no network. Feed/gphotos network calls are monkeypatched exactly the
way tests/test_sync.py and tests/test_gphotos.py already do.

Run with:  python3 -m unittest discover tests
"""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import goddard_sync as gs
import goddard_gphotos as gp

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 50


def _moment(mid, student_ids, date="2026-01-01T12:00:00.000Z"):
    """A feed "moment" result carrying one image, tagged with `student_ids`
    (possibly empty, possibly more than one — see FEED FACTS)."""
    return {
        "type": "moment", "studentIds": student_ids, "date": date, "caption": "",
        "moments": [{"_id": mid, "type": "image",
                     "thumbnailTransformed": f"https://cdn.example/{mid}_thumb.jpg"}],
    }


def _dailysheet(student_ids, label):
    """A dailysheet result — the only place a child's possessive name (e.g.
    "Ada's") appears in the feed."""
    return {"type": "dailysheet", "studentIds": student_ids, "studentLabel": label}


class FakeGP:
    """Stand-in for goddard_gphotos used where we only care that
    goddard_sync routes calls correctly, not what goddard_gphotos itself
    does with them (that's covered by tests/test_gphotos.py)."""
    class AuthError(Exception):
        pass

    def __init__(self):
        self.calls = []

    def upload_pending(self, cfg, save_cfg, state, out_dir, checkpoint, mode=None,
                        album_title=None, album_id=None, workers=3, limit=None,
                        dry_run=False, cache_get=None, cache_set=None):
        self.calls.append({"out_dir": out_dir, "album_title": album_title,
                            "album_id": album_id, "mode": mode})
        return {"uploaded": 0, "failed": 0, "album_id": None, "candidates": 0}


# --- Name derivation ---------------------------------------------------------
class TestNameDerivation(unittest.TestCase):
    def test_config_override_wins_over_dailysheet_label(self):
        cfg = {"students": {"s1": {"name": "Ada"}}}
        self.assertEqual(gs._student_name_and_source("s1", cfg, {"s1": "Someone's"}),
                          ("Ada", "config"))

    def test_dailysheet_label_strips_possessive_both_apostrophes(self):
        for label in ("Ada's", "Ada’s"):
            self.assertEqual(gs._student_name_and_source("s1", {}, {"s1": label}),
                              ("Ada", "daily sheet"))

    def test_label_with_extra_whitespace_is_trimmed(self):
        self.assertEqual(gs._student_name_and_source("s1", {}, {"s1": "  Ada's  "}),
                          ("Ada", "daily sheet"))

    def test_fallback_uses_last_6_of_id(self):
        self.assertEqual(gs._student_name_and_source("abcdef123456", {}, {}),
                          ("student-123456", "fallback"))

    def test_sanitize_replaces_unsafe_characters(self):
        self.assertEqual(gs._sanitize_name("Jo/hn:Doe*?"), "Jo-hn-Doe--")
        self.assertEqual(gs._sanitize_name("Anne_Marie-2"), "Anne_Marie-2")  # untouched

    def test_config_name_is_sanitized_too(self):
        cfg = {"students": {"s1": {"name": "Anne / Marie"}}}
        self.assertEqual(gs._student_name_and_source("s1", cfg, {}), ("Anne - Marie", "config"))


# --- {name} templating --------------------------------------------------------
class TestTemplating(unittest.TestCase):
    def test_output_dir_with_placeholder_is_formatted(self):
        cfg = {"output_dir": "~/Pictures/Goddard-{name}"}
        self.assertEqual(gs._student_output_dir(cfg, "s1", "Ada"),
                          os.path.expanduser("~/Pictures/Goddard-Ada"))

    def test_output_dir_without_placeholder_auto_appends(self):
        cfg = {"output_dir": "~/Pictures/Goddard"}
        self.assertEqual(gs._student_output_dir(cfg, "s1", "Ada"),
                          os.path.expanduser("~/Pictures/Goddard-Ada"))

    def test_album_title_without_placeholder_auto_appends(self):
        cfg = {"gphotos_album": "Goddard School"}
        self.assertEqual(gs._student_album_title(cfg, "s1", "Ada"), "Goddard School - Ada")

    def test_album_title_with_placeholder_is_formatted(self):
        cfg = {"gphotos_album": "Goddard - {name}"}
        self.assertEqual(gs._student_album_title(cfg, "s1", "Ada"), "Goddard - Ada")

    def test_two_children_never_collide_when_template_lacks_placeholder(self):
        cfg = {"output_dir": "~/Pictures/Goddard", "gphotos_album": "Goddard"}
        ada_dir = gs._student_output_dir(cfg, "s1", "Ada")
        ben_dir = gs._student_output_dir(cfg, "s2", "Ben")
        self.assertNotEqual(ada_dir, ben_dir)
        ada_album = gs._student_album_title(cfg, "s1", "Ada")
        ben_album = gs._student_album_title(cfg, "s2", "Ben")
        self.assertNotEqual(ada_album, ben_album)

    def test_per_child_override_used_verbatim_without_placeholder(self):
        cfg = {"output_dir": "~/Pictures/Goddard", "students": {"s1": {"output_dir": "/custom/path"}}}
        self.assertEqual(gs._student_output_dir(cfg, "s1", "Ada"), "/custom/path")

    def test_per_child_override_formats_placeholder_if_present(self):
        cfg = {"gphotos_album": "Goddard", "students": {"s1": {"gphotos_album": "Album for {name}"}}}
        self.assertEqual(gs._student_album_title(cfg, "s1", "Ada"), "Album for Ada")


# --- Grouping ------------------------------------------------------------------
class TestGrouping(unittest.TestCase):
    def test_single_id_goes_to_that_child_only(self):
        items = [{"id": "m1", "student_ids": ["s1"]}]
        groups = gs._group_by_student(items, ["s1", "s2"])
        self.assertEqual([it["id"] for it in groups["s1"]], ["m1"])
        self.assertEqual(groups["s2"], [])

    def test_two_ids_go_to_both_children(self):
        items = [{"id": "m1", "student_ids": ["s1", "s2"]}]
        groups = gs._group_by_student(items, ["s1", "s2"])
        self.assertEqual([it["id"] for it in groups["s1"]], ["m1"])
        self.assertEqual([it["id"] for it in groups["s2"]], ["m1"])

    def test_no_ids_go_to_every_child(self):
        items = [{"id": "m1", "student_ids": []}]
        groups = gs._group_by_student(items, ["s1", "s2"])
        self.assertEqual([it["id"] for it in groups["s1"]], ["m1"])
        self.assertEqual([it["id"] for it in groups["s2"]], ["m1"])

    def test_missing_student_ids_key_also_goes_to_everyone(self):
        items = [{"id": "m1"}]
        groups = gs._group_by_student(items, ["s1", "s2"])
        self.assertEqual([it["id"] for it in groups["s1"]], ["m1"])
        self.assertEqual([it["id"] for it in groups["s2"]], ["m1"])


# --- Per-child album-id caching ------------------------------------------------
class TestPerChildAlbumCache(unittest.TestCase):
    def test_cache_targets_students_dict_not_top_level_key(self):
        cfg = {"students": {}, "gphotos_album_id": "SHOULD-NOT-CHANGE",
               "gphotos_client_id": "cid", "gphotos_client_secret": "secret",
               "gphotos_refresh_token": "reftok"}
        get, set_ = gs._student_album_cache(cfg, "s1")
        self.assertIsNone(get())

        def fake_request(method, url, headers=None, body=None, timeout=120):
            if url == gp.TOKEN_URL:
                return 200, json.dumps({"access_token": "AT"}).encode()
            if method == "GET" and url.startswith(gp.ALBUMS_URL):
                return 200, json.dumps({"albums": []}).encode()
            if method == "POST" and url == gp.ALBUMS_URL:
                return 200, json.dumps({"id": "new-album-id"}).encode()
            raise AssertionError(f"unexpected call {method} {url}")

        with patch.object(gp, "request", side_effect=fake_request):
            result = gp.resolve_album(cfg, gp._Cache(), lambda c: None,
                                       title="Ada", cache_get=get, cache_set=set_)
        self.assertEqual(result, "new-album-id")
        self.assertEqual(cfg["students"]["s1"]["gphotos_album_id"], "new-album-id")
        self.assertEqual(cfg["gphotos_album_id"], "SHOULD-NOT-CHANGE")
        # A second child's cache is independent of the first's.
        get2, _ = gs._student_album_cache(cfg, "s2")
        self.assertIsNone(get2())

    def test_cached_value_short_circuits_with_no_network(self):
        cfg = {"students": {"s1": {"gphotos_album_id": "cached-id"}}}
        get, set_ = gs._student_album_cache(cfg, "s1")
        with patch.object(gp, "request", side_effect=AssertionError("no network expected")):
            result = gp.resolve_album(cfg, gp._Cache(), lambda c: None,
                                       cache_get=get, cache_set=set_)
        self.assertEqual(result, "cached-id")


# --- `upload --student` --------------------------------------------------------
class TestUploadStudentFiltering(unittest.TestCase):
    def _args(self, **over):
        base = dict(config="/nonexistent-config.json", mode=None, album=None, album_id=None,
                    dry_run=True, limit=None, workers=3, output_dir=None, student=None)
        base.update(over)
        return types.SimpleNamespace(**base)

    def _cfg(self, tmp):
        return {"per_student": True, "gphotos_mode": "library", "token": "tok",
                "output_dir": os.path.join(tmp, "Goddard-{name}"), "students": {}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_student_by_name_restricts_to_one_child(self):
        results = [_dailysheet(["s1"], "Ada's"), _dailysheet(["s2"], "Ben's")]
        fake_gp = FakeGP()
        with patch.object(gs, "load_config", return_value=self._cfg(self.tmp)), \
             patch.object(gs, "_import_gphotos", return_value=fake_gp), \
             patch.object(gs, "fetch_feed", return_value=results):
            rc = gs.cmd_upload(self._args(student="Ada"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake_gp.calls), 1)
        self.assertIn("Goddard-Ada", fake_gp.calls[0]["out_dir"])

    def test_student_by_id_also_matches(self):
        results = [_dailysheet(["s1"], "Ada's"), _dailysheet(["s2"], "Ben's")]
        fake_gp = FakeGP()
        with patch.object(gs, "load_config", return_value=self._cfg(self.tmp)), \
             patch.object(gs, "_import_gphotos", return_value=fake_gp), \
             patch.object(gs, "fetch_feed", return_value=results):
            rc = gs.cmd_upload(self._args(student="s2"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake_gp.calls), 1)
        self.assertIn("Goddard-Ben", fake_gp.calls[0]["out_dir"])

    def test_album_flags_without_student_are_rejected(self):
        with patch.object(gs, "load_config", return_value=self._cfg(self.tmp)), \
             patch.object(gs, "_import_gphotos", return_value=FakeGP()):
            rc = gs.cmd_upload(self._args(mode="album", album="Custom Album"))
        self.assertEqual(rc, 2)

    def test_album_flag_with_student_is_used(self):
        results = [_dailysheet(["s1"], "Ada's")]
        fake_gp = FakeGP()
        with patch.object(gs, "load_config", return_value=self._cfg(self.tmp)), \
             patch.object(gs, "_import_gphotos", return_value=fake_gp), \
             patch.object(gs, "fetch_feed", return_value=results):
            rc = gs.cmd_upload(self._args(mode="album", student="Ada", album="Custom Album"))
        self.assertEqual(rc, 0)
        self.assertEqual(fake_gp.calls[0]["album_title"], "Custom Album")

    def test_unknown_student_is_an_error(self):
        results = [_dailysheet(["s1"], "Ada's")]
        with patch.object(gs, "load_config", return_value=self._cfg(self.tmp)), \
             patch.object(gs, "_import_gphotos", return_value=FakeGP()), \
             patch.object(gs, "fetch_feed", return_value=results):
            rc = gs.cmd_upload(self._args(student="Nobody"))
        self.assertEqual(rc, 2)


# --- Combined per-run exit code / notification --------------------------------
class TestCombinedExitCode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self):
        return {"per_student": True, "students": {}, "ntfy_topic": "t", "ntfy_server": "https://ntfy.sh",
                "output_dir": os.path.join(self.tmp, "Goddard-{name}"), "gphotos_album": "Goddard"}

    def _run(self, canned):
        results = [_dailysheet(["s1"], "Ada's"), _dailysheet(["s2"], "Ben's")]

        def fake_sync_folder(args, cfg, out_dir, items, token, prefix="", album_title=None,
                              cache_get=None, cache_set=None):
            sid = "s1" if prefix.startswith("Ada") else "s2"
            return dict(canned[sid])

        notified = []
        with patch.object(gs, "_sync_folder", side_effect=fake_sync_folder), \
             patch.object(gs, "_notify",
                          side_effect=lambda cfg, title, msg, priority=None: notified.append(title)):
            args = types.SimpleNamespace(quiet=True, workers=4, no_upload=False)
            rc = gs._run_sync_per_student(args, self._cfg(), "token", results, [])
        return rc, notified

    def test_all_ok_notifies_once_omitting_children_with_nothing_new(self):
        canned = {
            "s1": {"ok": 3, "upgraded": 0, "already": 940, "err": 0,
                   "gp_uploaded": 0, "gp_auth_failed": False, "summary": "s1"},
            "s2": {"ok": 12, "upgraded": 0, "already": 0, "err": 0,
                   "gp_uploaded": 0, "gp_auth_failed": False, "summary": "s2"},
        }
        rc, notified = self._run(canned)
        self.assertEqual(rc, 0)
        self.assertEqual(len(notified), 1)
        self.assertIn("Ada 3 new", notified[0])
        self.assertIn("Ben 12 new", notified[0])

    def test_nothing_new_sends_no_notification(self):
        canned = {
            "s1": {"ok": 0, "upgraded": 0, "already": 940, "err": 0,
                   "gp_uploaded": 0, "gp_auth_failed": False, "summary": "s1"},
            "s2": {"ok": 0, "upgraded": 0, "already": 12, "err": 0,
                   "gp_uploaded": 0, "gp_auth_failed": False, "summary": "s2"},
        }
        rc, notified = self._run(canned)
        self.assertEqual(rc, 0)
        self.assertEqual(notified, [])

    def test_one_child_failing_makes_exit_code_1(self):
        canned = {
            "s1": {"ok": 1, "upgraded": 0, "already": 0, "err": 2,
                   "gp_uploaded": 0, "gp_auth_failed": False, "summary": "s1"},
            "s2": {"ok": 0, "upgraded": 0, "already": 0, "err": 0,
                   "gp_uploaded": 0, "gp_auth_failed": False, "summary": "s2"},
        }
        rc, notified = self._run(canned)
        self.assertEqual(rc, 1)
        self.assertEqual(len(notified), 1)
        self.assertIn("2 failed", notified[0])

    def test_auth_failure_beats_plain_failure_for_exit_code(self):
        canned = {
            "s1": {"ok": 0, "upgraded": 0, "already": 0, "err": 1,
                   "gp_uploaded": 0, "gp_auth_failed": False, "summary": "s1"},
            "s2": {"ok": 0, "upgraded": 0, "already": 0, "err": 0,
                   "gp_uploaded": 0, "gp_auth_failed": True, "summary": "s2"},
        }
        rc, notified = self._run(canned)
        self.assertEqual(rc, 2)  # 2 (auth) beats 1 (plain failure)
        self.assertTrue(any("login expired" in t for t in notified))


# --- End-to-end: two students, two folders, two state files -------------------
class TestEndToEndPerStudentSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_two_students_produce_two_folders_and_state_files(self):
        cfg_path = os.path.join(self.tmp, "config.json")
        cfg = {"token": "tok", "per_student": True,
               "output_dir": os.path.join(self.tmp, "Goddard-{name}"),
               "gphotos_album": "Goddard"}
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)

        results = [
            _dailysheet(["s1"], "Ada's"),
            _dailysheet(["s2"], "Ben's"),
            _moment("m1", ["s1"]),
            _moment("m2", ["s2"]),
            _moment("m3", ["s1", "s2"]),  # photo of both siblings
            _moment("m4", []),            # untagged -> everyone
        ]

        with patch.object(gs, "fetch_feed", return_value=results), \
             patch.object(gs, "_fetch_with_retry", return_value=(JPEG, False)):
            rc = gs.main(["--config", cfg_path, "sync", "--quiet"])

        self.assertEqual(rc, 0)
        ada_dir = os.path.join(self.tmp, "Goddard-Ada")
        ben_dir = os.path.join(self.tmp, "Goddard-Ben")
        self.assertTrue(os.path.isdir(ada_dir))
        self.assertTrue(os.path.isdir(ben_dir))

        with open(gs._state_path(ada_dir)) as f:
            maya_state = json.load(f)
        with open(gs._state_path(ben_dir)) as f:
            max_state = json.load(f)

        self.assertEqual(set(maya_state["items"].keys()), {"m1", "m3", "m4"})
        self.assertEqual(set(max_state["items"].keys()), {"m2", "m3", "m4"})
        # Each folder's copy of the shared items is independent on disk.
        for state, out_dir in ((maya_state, ada_dir), (max_state, ben_dir)):
            for entry in state["items"].values():
                self.assertTrue(os.path.isfile(os.path.join(out_dir, entry["file"])))


if __name__ == "__main__":
    unittest.main()


class TestUploadStudentDefaultAlbum(unittest.TestCase):
    """upload --student NAME without --album must use that child's resolved
    album title, not fall through to the raw {name} template."""
    def test_uses_child_album_when_no_album_flag(self):
        import tempfile, types
        d = tempfile.mkdtemp()
        cfg = {"per_student": True, "token": "t", "output_dir": os.path.join(d, "G-{name}"),
               "gphotos_album": "School - {name}", "gphotos_mode": "album",
               "gphotos_refresh_token": "r", "gphotos_client_id": "c", "gphotos_client_secret": "s"}
        results = [{"type": "dailysheet", "studentIds": ["sid1"], "studentLabel": "Ada's"}]
        seen = {}
        class FakeGP:
            AuthError = Exception
            def upload_pending(self, cfg, save_cfg, state, out_dir, checkpoint, **kw):
                seen.update(kw)
                return {"uploaded": 0, "failed": 0, "album_id": None, "candidates": 0}
        orig_fetch, orig_load = gs.fetch_feed, gs.load_config
        gs.fetch_feed = lambda tok: results
        gs.load_config = lambda p: dict(cfg)
        try:
            args = types.SimpleNamespace(config="x", student="Ada", album=None, album_id=None,
                                         output_dir=None, workers=1, limit=None, dry_run=True, mode="album")
            gs._upload_per_student(args, dict(cfg), FakeGP(), "album")
        finally:
            gs.fetch_feed, gs.load_config = orig_fetch, orig_load
        self.assertEqual(seen.get("album_title"), "School - Ada")


class TestDeferUnnamedStudent(unittest.TestCase):
    """A child with only a fallback (id-derived) name is not synced yet: no
    folder is created, a notification goes out, and named children still sync."""
    def test_fallback_child_deferred(self):
        import tempfile, types
        d = tempfile.mkdtemp()
        cfg = {"per_student": True, "token": "t", "output_dir": os.path.join(d, "G-{name}"),
               "gphotos_album": "School - {name}", "gphotos_mode": "off", "ntfy_topic": "x"}
        results = [
            {"type": "dailysheet", "studentIds": ["sid1"], "studentLabel": "Ada's"},
            {"type": "moment", "studentIds": ["sid2"], "date": "2026-09-14T15:00:00Z",
             "moments": [{"_id": "m1", "type": "image", "thumbnailTransformed": "/x/y_thumb.jpg"}]},
        ]
        notes = []
        orig_notify, orig_folder = gs._notify, gs._sync_folder
        gs._notify = lambda cfg, title, msg, priority=None: notes.append(title)
        synced = []
        gs._sync_folder = lambda args, cfg, out_dir, items, token, **kw: (synced.append(out_dir) or
            {"ok": 0, "upgraded": 0, "already": 0, "err": 0, "gp_uploaded": 0,
             "gp_auth_failed": False, "summary": ""})
        try:
            args = types.SimpleNamespace(quiet=True, workers=1, no_upload=True, output_dir=None)
            rc = gs._run_sync_per_student(args, cfg, "t", results, gs._media_items(results))
        finally:
            gs._notify, gs._sync_folder = orig_notify, orig_folder
        self.assertEqual(rc, 0)
        self.assertEqual(synced, [os.path.join(d, "G-Ada")])
        self.assertFalse(os.path.exists(os.path.join(d, "G-student-" + "sid2"[-6:])))
        self.assertTrue(any("waiting for a name" in t for t in notes))
