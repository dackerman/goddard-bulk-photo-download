"""Unit tests for goddard_sync.py — stdlib unittest only, no network.

Run with:  python3 -m unittest discover tests
"""
import os
import sys
import time
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import goddard_sync as gs


def _set_tz(tz):
    """Point the process at a fixed timezone for a deterministic
    astimezone() conversion. Unix-only (time.tzset), which is fine — this
    tool targets Linux/macOS."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = tz
    if hasattr(time, "tzset"):
        time.tzset()
    return old


def _restore_tz(old):
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    if hasattr(time, "tzset"):
        time.tzset()


@unittest.skipUnless(hasattr(time, "tzset"), "requires a Unix-like OS (time.tzset)")
class TestFilename(unittest.TestCase):
    def setUp(self):
        self._old_tz = _set_tz("America/New_York")

    def tearDown(self):
        _restore_tz(self._old_tz)

    def test_local_time_conversion_edt(self):
        # 2026-09-12T01:45:33Z is EDT (UTC-4): 2026-09-11 21:45:33 local.
        # This is the exact "evening post gets the next UTC day" case the
        # rewrite fixes: the UTC date rolls over to the 12th, but locally
        # it's still the 11th.
        name = gs._filename("2026-09-12T01:45:33.725Z", "abc123", "jpg")
        self.assertEqual(name, "2026-09-11_214533_abc123.jpg")

    def test_local_time_conversion_est(self):
        # 2026-01-09T23:08:27Z is EST (UTC-5): 2026-01-09 18:08:27 local.
        name = gs._filename("2026-01-09T23:08:27.866Z", "def456", "heic")
        self.assertEqual(name, "2026-01-09_180827_def456.heic")

    def test_nodate_fallback(self):
        self.assertEqual(gs._filename("", "abc123", "jpg"), "nodate_abc123.jpg")
        self.assertEqual(gs._filename("not-a-date", "abc123"), "nodate_abc123.jpg")


class TestSniffExt(unittest.TestCase):
    def test_jpeg(self):
        self.assertEqual(gs._sniff_ext(b"\xff\xd8\xff\xe0" + b"\0" * 20), "jpg")

    def test_png(self):
        self.assertEqual(gs._sniff_ext(b"\x89PNG\r\n\x1a\n" + b"\0" * 10), "png")

    def test_heic(self):
        data = b"\x00\x00\x00\x18ftypheic" + b"\0" * 20
        self.assertEqual(gs._sniff_ext(data), "heic")

    def test_heix_and_mif1_brands_also_heic(self):
        for brand in (b"heix", b"mif1", b"heif", b"hevc"):
            data = b"\x00\x00\x00\x18ftyp" + brand + b"\0" * 20
            self.assertEqual(gs._sniff_ext(data), "heic", brand)

    def test_unknown_defaults_to_jpg(self):
        self.assertEqual(gs._sniff_ext(b"garbage-not-an-image"), "jpg")

    def test_too_short_defaults(self):
        self.assertEqual(gs._sniff_ext(b"\xff\xd8"), "jpg")
        self.assertEqual(gs._sniff_ext(b""), "jpg")


class TestState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_state_reports_not_existed(self):
        state, existed = gs._load_state(self.tmp)
        self.assertFalse(existed)
        self.assertEqual(state, {"version": 1, "items": {}})

    def test_round_trip_preserves_unknown_keys(self):
        state, _ = gs._load_state(self.tmp)
        state["items"]["m1"] = {
            "file": "x.jpg", "type": "image", "rendition": "original",
            "draft": False, "date": "2026-01-01T00:00:00Z", "caption": "",
            "bytes": 123,
            "gphotos": {"uploaded": True, "album": "2026"},  # a foreign tool's key
        }
        gs._save_state_atomic(self.tmp, state)

        # No .tmp file left behind (atomic replace).
        self.assertFalse(os.path.exists(gs._state_path(self.tmp) + ".tmp"))

        state2, existed2 = gs._load_state(self.tmp)
        self.assertTrue(existed2)
        self.assertEqual(state2["items"]["m1"]["gphotos"], {"uploaded": True, "album": "2026"})

        # Simulate a merge-style rewrite (what the upgrade pass does) and
        # confirm the foreign key survives.
        state2["items"]["m1"].update({"rendition": "original", "bytes": 456})
        gs._save_state_atomic(self.tmp, state2)
        state3, _ = gs._load_state(self.tmp)
        self.assertEqual(state3["items"]["m1"]["gphotos"], {"uploaded": True, "album": "2026"})
        self.assertEqual(state3["items"]["m1"]["bytes"], 456)


@unittest.skipUnless(hasattr(time, "tzset"), "requires a Unix-like OS (time.tzset)")
class TestMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_tz = _set_tz("America/New_York")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        _restore_tz(self._old_tz)

    def _write(self, name, data):
        with open(os.path.join(self.tmp, name), "wb") as f:
            f.write(data)

    def test_migrate_renames_and_fixes_heic_extension(self):
        # Old-style file: UTC-dated name, genuinely JPEG bytes.
        self._write("2026-09-12_014533_abc123.jpg", b"\xff\xd8\xff\xe0" + b"\0" * 50)
        # Old-style file: UTC-dated name, but secretly HEIC bytes under .jpg.
        self._write("2026-09-10_120000_def456.jpg", b"\x00\x00\x00\x18ftypheic" + b"\0" * 50)
        # A file on disk that isn't in the feed at all — must be left alone.
        self._write("not_in_feed_zzz999.jpg", b"\xff\xd8\xff\xe0" + b"\0" * 10)

        items = [
            {"id": "abc123", "type": "image", "base": "http://x/abc123", "is_draft": False,
             "date": "2026-09-12T01:45:33.725Z", "caption": "hi"},
            {"id": "def456", "type": "image", "base": "http://x/def456", "is_draft": False,
             "date": "2026-09-10T12:00:00.000Z", "caption": ""},
        ]
        state = {"version": 1, "items": {}}
        migrated = gs._migrate_existing_files(self.tmp, items, state)
        self.assertEqual(migrated, 2)

        # JPEG file renamed to the local-time name; extension unchanged.
        self.assertEqual(state["items"]["abc123"]["file"], "2026-09-11_214533_abc123.jpg")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "2026-09-11_214533_abc123.jpg")))
        self.assertEqual(state["items"]["abc123"]["rendition"], "unknown")
        self.assertEqual(state["items"]["abc123"]["caption"], "hi")

        # HEIC-as-.jpg file renamed AND given the correct extension.
        self.assertEqual(state["items"]["def456"]["file"], "2026-09-10_080000_def456.heic")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "2026-09-10_080000_def456.heic")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "2026-09-10_120000_def456.jpg")))

        # File that was never in the feed is untouched and not in state.
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "not_in_feed_zzz999.jpg")))
        self.assertNotIn("zzz999", state["items"])

    def test_no_matching_file_on_disk_is_skipped(self):
        items = [{"id": "nope", "type": "image", "base": "http://x/nope", "is_draft": False,
                  "date": "2026-01-01T00:00:00Z", "caption": ""}]
        state = {"version": 1, "items": {}}
        migrated = gs._migrate_existing_files(self.tmp, items, state)
        self.assertEqual(migrated, 0)
        self.assertEqual(state["items"], {})


if __name__ == "__main__":
    unittest.main()


class TestConfigArgPosition(unittest.TestCase):
    """--config must work both before and after the subcommand."""
    def _parse(self, argv):
        captured = {}
        orig = gs.cmd_status
        gs.cmd_status = lambda a: captured.setdefault("config", a.config) or 0
        try:
            gs.main(argv)
        finally:
            gs.cmd_status = orig
        return captured["config"]

    def test_before_subcommand(self):
        self.assertEqual(self._parse(["--config", "/tmp/x.json", "status"]), "/tmp/x.json")

    def test_after_subcommand(self):
        self.assertEqual(self._parse(["status", "--config", "/tmp/y.json"]), "/tmp/y.json")

    def test_default(self):
        self.assertEqual(self._parse(["status"]), gs.DEFAULT_CONFIG)
