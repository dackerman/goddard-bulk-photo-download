"""Unit tests for goddard_gphotos.py — stdlib unittest only, no network.

Every test replaces `goddard_gphotos.request` (the single HTTP seam) with a
small fake, so nothing here ever makes a real connection.

Run with:  python3 -m unittest discover tests
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import goddard_gphotos as gp


def _cfg(**over):
    base = {
        "gphotos_client_id": "cid", "gphotos_client_secret": "secret",
        "gphotos_refresh_token": "reftok", "gphotos_mode": "library",
        "gphotos_album": "Goddard",
    }
    base.update(over)
    return base


class _SavedCfgs(list):
    """Callable stand-in for save_cfg that just records each call."""
    def __call__(self, cfg):
        self.append(dict(cfg))


# --- pending_items -----------------------------------------------------------
class TestPendingItems(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, name):
        with open(os.path.join(self.tmp, name), "wb") as f:
            f.write(b"x")

    def test_never_uploaded_is_pending(self):
        self._touch("a.jpg")
        state = {"items": {"m1": {"file": "a.jpg", "rendition": "original", "date": "2026-01-01"}}}
        pending = gp.pending_items(state, self.tmp)
        self.assertEqual([mid for mid, _ in pending], ["m1"])

    def test_reupload_after_rendition_upgrade(self):
        self._touch("a.jpg")
        state = {"items": {"m1": {
            "file": "a.jpg", "rendition": "original", "date": "2026-01-01",
            "gphotos": {"id": "g1", "rendition": "display", "album_id": None, "at": "x"},
        }}}
        pending = gp.pending_items(state, self.tmp)
        self.assertEqual([mid for mid, _ in pending], ["m1"])

    def test_up_to_date_is_skipped(self):
        self._touch("a.jpg")
        state = {"items": {"m1": {
            "file": "a.jpg", "rendition": "original", "date": "2026-01-01",
            "gphotos": {"id": "g1", "rendition": "original", "album_id": None, "at": "x"},
        }}}
        self.assertEqual(gp.pending_items(state, self.tmp), [])

    def test_missing_file_is_skipped(self):
        # No file ever written for "a.jpg" — item is skipped even though
        # it's otherwise a fresh (never-uploaded) entry.
        state = {"items": {"m1": {"file": "a.jpg", "rendition": "original", "date": "2026-01-01"}}}
        self.assertEqual(gp.pending_items(state, self.tmp), [])

    def test_no_file_at_all_is_skipped(self):
        state = {"items": {"m1": {"file": None, "rendition": "unavailable", "date": "2026-01-01"}}}
        self.assertEqual(gp.pending_items(state, self.tmp), [])

    def test_sorted_oldest_first(self):
        self._touch("a.jpg")
        self._touch("b.jpg")
        state = {"items": {
            "new": {"file": "b.jpg", "rendition": "original", "date": "2026-06-01T00:00:00Z"},
            "old": {"file": "a.jpg", "rendition": "original", "date": "2026-01-01T00:00:00Z"},
        }}
        pending = gp.pending_items(state, self.tmp)
        self.assertEqual([mid for mid, _ in pending], ["old", "new"])


# --- Album resolution ----------------------------------------------------------
class TestResolveAlbum(unittest.TestCase):
    def test_cached_id_short_circuits_with_no_network(self):
        cfg = _cfg(gphotos_album_id="cached-id")
        saved = _SavedCfgs()
        with patch.object(gp, "request", side_effect=AssertionError("should not hit network")):
            result = gp.resolve_album(cfg, gp._Cache(), saved, allow_network=True)
        self.assertEqual(result, "cached-id")
        self.assertEqual(saved, [])

    def test_explicit_album_id_wins_over_everything(self):
        cfg = _cfg(gphotos_album_id="cached-id")
        saved = _SavedCfgs()
        with patch.object(gp, "request", side_effect=AssertionError("should not hit network")):
            result = gp.resolve_album(cfg, gp._Cache(), saved, album_id="explicit-id")
        self.assertEqual(result, "explicit-id")

    def test_found_by_title(self):
        cfg = _cfg()
        saved = _SavedCfgs()

        def fake(method, url, headers=None, body=None, timeout=120):
            if url == gp.TOKEN_URL:
                return 200, json.dumps({"access_token": "AT"}).encode()
            if url.startswith(gp.ALBUMS_URL) and method == "GET":
                return 200, json.dumps({"albums": [
                    {"id": "other", "title": "Not It"},
                    {"id": "found-id", "title": "Goddard"},
                ]}).encode()
            raise AssertionError(f"unexpected call {method} {url}")

        with patch.object(gp, "request", side_effect=fake):
            result = gp.resolve_album(cfg, gp._Cache(), saved)
        self.assertEqual(result, "found-id")
        self.assertEqual(cfg["gphotos_album_id"], "found-id")
        self.assertEqual(saved[-1]["gphotos_album_id"], "found-id")

    def test_created_when_not_found(self):
        cfg = _cfg()
        saved = _SavedCfgs()

        def fake(method, url, headers=None, body=None, timeout=120):
            if url == gp.TOKEN_URL:
                return 200, json.dumps({"access_token": "AT"}).encode()
            if url.startswith(gp.ALBUMS_URL) and method == "GET":
                return 200, json.dumps({"albums": []}).encode()
            if url == gp.ALBUMS_URL and method == "POST":
                payload = json.loads(body)
                self.assertEqual(payload["album"]["title"], "Goddard")
                return 200, json.dumps({"id": "new-id", "title": "Goddard"}).encode()
            raise AssertionError(f"unexpected call {method} {url}")

        with patch.object(gp, "request", side_effect=fake):
            result = gp.resolve_album(cfg, gp._Cache(), saved)
        self.assertEqual(result, "new-id")
        self.assertEqual(cfg["gphotos_album_id"], "new-id")

    def test_dry_run_offline_returns_none_without_network(self):
        cfg = _cfg()
        saved = _SavedCfgs()
        with patch.object(gp, "request", side_effect=AssertionError("should not hit network")):
            result = gp.resolve_album(cfg, gp._Cache(), saved, allow_network=False)
        self.assertIsNone(result)
        self.assertEqual(saved, [])


# --- Token refresh -------------------------------------------------------------
class TestTokenRefresh(unittest.TestCase):
    def test_401_triggers_one_refresh_and_retry(self):
        cfg = _cfg()
        cache = gp._Cache()
        calls = []
        tokens_issued = {"n": 0}

        def fake(method, url, headers=None, body=None, timeout=120):
            calls.append((method, url, dict(headers or {})))
            if url == gp.TOKEN_URL:
                tokens_issued["n"] += 1
                return 200, json.dumps({"access_token": f"AT{tokens_issued['n']}"}).encode()
            if url.startswith(gp.ALBUMS_URL):
                if headers.get("Authorization") == "Bearer AT2":
                    return 200, json.dumps({"albums": []}).encode()
                return 401, b'{"error": "invalid token"}'
            raise AssertionError("unexpected call")

        with patch.object(gp, "request", side_effect=fake):
            status, raw = gp._api(cfg, cache, "GET", gp.ALBUMS_URL)
        self.assertEqual(status, 200)
        self.assertEqual(tokens_issued["n"], 2)  # first token, then one refresh

    def test_refresh_failure_raises_autherror(self):
        cfg = _cfg()
        cache = gp._Cache()

        def fake(method, url, headers=None, body=None, timeout=120):
            if url == gp.TOKEN_URL:
                return 400, b'{"error": "invalid_grant"}'
            raise AssertionError("should not reach the API call")

        with patch.object(gp, "request", side_effect=fake):
            with self.assertRaises(gp.AuthError):
                gp._api(cfg, cache, "GET", gp.ALBUMS_URL)

    def test_missing_refresh_token_raises_autherror_immediately(self):
        cfg = _cfg(gphotos_refresh_token=None)
        cache = gp._Cache()
        with patch.object(gp, "request", side_effect=AssertionError("no network expected")):
            with self.assertRaises(gp.AuthError):
                gp._access_token(cfg, cache)


# --- upload_pending: batching, state marking, dry-run --------------------------
class TestUploadPending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_state(self, n, bad_mids=()):
        """n pending image items, each with a small unique file on disk."""
        state = {"items": {}}
        for i in range(n):
            mid = f"m{i:03d}"
            fname = f"2026-01-{(i % 28) + 1:02d}_000000_{mid}.jpg"
            content = f"BADFILE-{mid}" if mid in bad_mids else f"content-{mid}"
            with open(os.path.join(self.tmp, fname), "w") as f:
                f.write(content)
            state["items"][mid] = {
                "file": fname, "type": "image", "rendition": "original",
                "draft": False, "date": f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
                "caption": "hi", "bytes": len(content),
            }
        return state

    def _fake_ok(self, calls, fail_tokens=()):
        """A fake request() that: refreshes tokens, echoes upload bytes back
        as the upload token, and answers batchCreate per-item, failing any
        item whose token is in `fail_tokens`."""
        def fake(method, url, headers=None, body=None, timeout=120):
            calls.append((method, url))
            if url == gp.TOKEN_URL:
                return 200, json.dumps({"access_token": "AT"}).encode()
            if url == gp.UPLOAD_URL:
                return 200, body  # echo file bytes back as the "upload token"
            if url == gp.BATCH_CREATE_URL:
                payload = json.loads(body)
                results = []
                for i, nmi in enumerate(payload["newMediaItems"]):
                    tok = nmi["simpleMediaItem"]["uploadToken"]
                    if tok in fail_tokens:
                        results.append({"uploadToken": tok,
                                        "status": {"code": 3, "message": "Invalid argument"}})
                    else:
                        results.append({"uploadToken": tok, "status": {"message": "Success"},
                                        "mediaItem": {"id": "gid-" + tok}})
                return 200, json.dumps({"newMediaItemResults": results}).encode()
            raise AssertionError(f"unexpected call {method} {url}")
        return fake

    def test_batches_at_50_and_checkpoints_per_batch(self):
        n = 120
        state = self._make_state(n)
        calls = []
        checkpoints = []
        with patch.object(gp, "request", side_effect=self._fake_ok(calls)):
            result = gp.upload_pending(_cfg(), _SavedCfgs(), state, self.tmp,
                                       lambda: checkpoints.append(1), workers=4)
        self.assertEqual(result["uploaded"], n)
        self.assertEqual(result["failed"], 0)
        batch_create_calls = [c for c in calls if c[1] == gp.BATCH_CREATE_URL]
        self.assertEqual(len(batch_create_calls), 3)  # ceil(120/50)
        self.assertEqual(len(checkpoints), 3)
        for mid, e in state["items"].items():
            self.assertIn("gphotos", e)
            self.assertEqual(e["gphotos"]["rendition"], "original")

    def test_last_batch_size_is_remainder(self):
        sizes = []

        def fake_capture(method, url, headers=None, body=None, timeout=120):
            if url == gp.TOKEN_URL:
                return 200, json.dumps({"access_token": "AT"}).encode()
            if url == gp.UPLOAD_URL:
                return 200, body
            if url == gp.BATCH_CREATE_URL:
                payload = json.loads(body)
                sizes.append(len(payload["newMediaItems"]))
                results = [{"uploadToken": nmi["simpleMediaItem"]["uploadToken"],
                           "status": {"message": "Success"}, "mediaItem": {"id": "g"}}
                          for nmi in payload["newMediaItems"]]
                return 200, json.dumps({"newMediaItemResults": results}).encode()
            raise AssertionError("unexpected")

        state = self._make_state(70)
        with patch.object(gp, "request", side_effect=fake_capture):
            gp.upload_pending(_cfg(), _SavedCfgs(), state, self.tmp, lambda: None)
        self.assertEqual(sizes, [50, 20])

    def test_partial_batch_failure_marks_only_successes(self):
        state = self._make_state(3, bad_mids=("m001",))
        calls = []
        with patch.object(gp, "request", side_effect=self._fake_ok(calls, fail_tokens=("BADFILE-m001",))):
            result = gp.upload_pending(_cfg(), _SavedCfgs(), state, self.tmp, lambda: None)
        self.assertEqual(result["uploaded"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertIn("gphotos", state["items"]["m000"])
        self.assertIn("gphotos", state["items"]["m002"])
        self.assertNotIn("gphotos", state["items"]["m001"])

    def test_byte_upload_failure_excludes_item_from_batchcreate(self):
        state = self._make_state(2, bad_mids=("m001",))

        def fake(method, url, headers=None, body=None, timeout=120):
            if url == gp.TOKEN_URL:
                return 200, json.dumps({"access_token": "AT"}).encode()
            if url == gp.UPLOAD_URL:
                if body.startswith(b"BADFILE"):
                    return 400, b"bad request"  # permanent, no retry
                return 200, body
            if url == gp.BATCH_CREATE_URL:
                payload = json.loads(body)
                # Only the good file's token should ever appear here.
                self.assertEqual(len(payload["newMediaItems"]), 1)
                tok = payload["newMediaItems"][0]["simpleMediaItem"]["uploadToken"]
                return 200, json.dumps({"newMediaItemResults": [
                    {"uploadToken": tok, "status": {"message": "Success"},
                     "mediaItem": {"id": "gid"}}]}).encode()
            raise AssertionError("unexpected")

        with patch.object(gp, "request", side_effect=fake):
            result = gp.upload_pending(_cfg(), _SavedCfgs(), state, self.tmp, lambda: None)
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(result["failed"], 1)

    def test_dry_run_makes_no_network_calls_or_writes(self):
        state = self._make_state(5)
        saved = _SavedCfgs()
        checkpoints = []
        with patch.object(gp, "request", side_effect=AssertionError("dry-run must not touch network")):
            result = gp.upload_pending(_cfg(gphotos_mode="album"), saved, state, self.tmp,
                                       lambda: checkpoints.append(1), dry_run=True)
        self.assertEqual(result["candidates"], 5)
        self.assertEqual(result["uploaded"], 0)
        self.assertIsNone(result["album_id"])  # nothing cached, network disallowed
        self.assertEqual(saved, [])
        self.assertEqual(checkpoints, [])
        for e in state["items"].values():
            self.assertNotIn("gphotos", e)

    def test_dry_run_reports_cached_album_id_without_network(self):
        state = self._make_state(2)
        with patch.object(gp, "request", side_effect=AssertionError("no network")):
            result = gp.upload_pending(_cfg(gphotos_mode="album", gphotos_album_id="cached"),
                                       _SavedCfgs(), state, self.tmp, lambda: None, dry_run=True)
        self.assertEqual(result["album_id"], "cached")

    def test_mode_off_is_a_noop(self):
        state = self._make_state(3)
        with patch.object(gp, "request", side_effect=AssertionError("no network")):
            result = gp.upload_pending(_cfg(gphotos_mode="off"), _SavedCfgs(), state,
                                       self.tmp, lambda: None)
        self.assertEqual(result, {"uploaded": 0, "failed": 0, "album_id": None, "candidates": 0})

    def test_limit_caps_candidates(self):
        state = self._make_state(10)
        with patch.object(gp, "request", side_effect=self._fake_ok([])):
            result = gp.upload_pending(_cfg(), _SavedCfgs(), state, self.tmp, lambda: None, limit=3)
        self.assertEqual(result["candidates"], 3)
        self.assertEqual(result["uploaded"], 3)

    def test_missing_credentials_raise_autherror_before_any_upload(self):
        state = self._make_state(3)
        cfg = _cfg(gphotos_refresh_token=None)
        with patch.object(gp, "request", side_effect=AssertionError("no network expected")):
            with self.assertRaises(gp.AuthError):
                gp.upload_pending(cfg, _SavedCfgs(), state, self.tmp, lambda: None)
        for e in state["items"].values():
            self.assertNotIn("gphotos", e)

    def test_album_mode_passes_album_id_to_batch_create(self):
        state = self._make_state(1)
        captured = {}

        def fake(method, url, headers=None, body=None, timeout=120):
            if url == gp.TOKEN_URL:
                return 200, json.dumps({"access_token": "AT"}).encode()
            if url == gp.UPLOAD_URL:
                return 200, body
            if url == gp.BATCH_CREATE_URL:
                payload = json.loads(body)
                captured["albumId"] = payload.get("albumId")
                tok = payload["newMediaItems"][0]["simpleMediaItem"]["uploadToken"]
                return 200, json.dumps({"newMediaItemResults": [
                    {"uploadToken": tok, "status": {"message": "Success"},
                     "mediaItem": {"id": "gid"}}]}).encode()
            raise AssertionError("unexpected")

        cfg = _cfg(gphotos_mode="album", gphotos_album_id="album-xyz")
        with patch.object(gp, "request", side_effect=fake):
            result = gp.upload_pending(cfg, _SavedCfgs(), state, self.tmp, lambda: None)
        self.assertEqual(captured["albumId"], "album-xyz")
        self.assertEqual(state["items"]["m000"]["gphotos"]["album_id"], "album-xyz")


if __name__ == "__main__":
    unittest.main()
