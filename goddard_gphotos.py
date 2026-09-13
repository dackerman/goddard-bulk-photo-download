#!/usr/bin/env python3
"""
goddard_gphotos — optional Google Photos upload support for goddard_sync.

Uploads files that `sync` has already downloaded into your local library up
to Google Photos, using the Google Photos Library API (the post-March-2025
API, which can only see/manage *data this app itself created* — see below).

Two modes (`gphotos_mode` in config):
  * "library" — items land in your main Google Photos library, no album.
  * "album"   — items land in a single app-created album (default title
    "Goddard"). Google Photos Library API restriction: this app can only see
    and add to albums *it created*; it cannot see or write to an album you
    made by hand in the Photos app, or one another app created. If you want
    the photos in a hand-made album, move them there yourself afterward —
    the API gives no way to automate that step.

Auth is the installed-app "loopback" OAuth flow: `gphotos-login` opens your
browser, you sign in and consent, Google redirects to a one-shot local HTTP
server on 127.0.0.1, and the resulting long-lived refresh token is stored in
config (mode 600, same as the Kaymbu token). Access tokens are derived from
it on demand and kept in memory only, for the life of one process.

Google offers no "replace an existing media item"' API: once an item has been
uploaded, uploading a better rendition later creates a *second*, separate
media item rather than updating the first. `upload` re-uploads an item when
its downloaded rendition has improved since the last upload (e.g. a
`_display` copy later replaced by the full-resolution original) — the older,
lower-resolution copy is left in your Google Photos library; delete it by
hand if you don't want the duplicate.

Quotas: 10,000 API requests/day per project. `request()` retries 429/5xx
with backoff, and refreshes the access token once on a 401 before retrying;
if the refresh itself fails, callers get `AuthError` and should tell the
user to re-run `gphotos-login`. Tokens are never logged.

The HTTP layer is the single `request()` function below, deliberately kept
tiny so tests can monkeypatch it and never touch the network.

Stdlib only — no third-party dependencies.
"""
from __future__ import annotations
import concurrent.futures as cf, http.server, json, os, secrets, threading, time
import urllib.error, urllib.parse, urllib.request, webbrowser
from datetime import datetime, timezone

# --- Constants ---------------------------------------------------------------
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://photoslibrary.googleapis.com/v1"
UPLOAD_URL = API_BASE + "/uploads"
BATCH_CREATE_URL = API_BASE + "/mediaItems:batchCreate"
ALBUMS_URL = API_BASE + "/albums"

# appendonly: upload/create media items. readonly.appcreateddata: list/read
# back the albums *this app* created (needed to find "Goddard" on a second
# run without re-creating it every time).
SCOPES = ("https://www.googleapis.com/auth/photoslibrary.appendonly "
          "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata")

DEFAULT_ALBUM_TITLE = "Goddard"
MAX_BATCH = 50  # mediaItems:batchCreate accepts at most this many per call

MIME_BY_EXT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "heic": "image/heic",
               "png": "image/png", "mp4": "video/mp4", "mov": "video/quicktime"}


class AuthError(Exception):
    """No usable Google Photos credentials, or the refresh token was
    rejected. Callers should tell the user to run `gphotos-login`."""


class UploadError(Exception):
    """A Google Photos API call failed persistently (not an auth problem)."""


# --- HTTP layer (the one thing tests replace) --------------------------------
def request(method, url, headers=None, body=None, timeout=120):
    """POST/GET `url` and return (status_code, response_bytes). Never raises
    for a non-2xx status — callers inspect the status themselves, the way
    `_http_head` etc. do in goddard_sync. This is the single seam tests
    monkeypatch to avoid all real network access."""
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# --- Access token (in-memory only; refresh token is what's persisted) -------
class _Cache:
    """Per-invocation access-token cache, shared (with a lock) across the
    upload thread pool so concurrent workers don't each refresh separately."""
    def __init__(self):
        self.access_token = None
        self.lock = threading.Lock()


def _refresh_access_token(cfg):
    if not cfg.get("gphotos_refresh_token"):
        raise AuthError("Not logged in to Google Photos — run `gphotos-login`.")
    client_id = cfg.get("gphotos_client_id")
    client_secret = cfg.get("gphotos_client_secret")
    if not client_id or not client_secret:
        raise AuthError("Missing Google OAuth client id/secret — run `gphotos-login`.")
    body = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": cfg["gphotos_refresh_token"], "grant_type": "refresh_token",
    }).encode()
    status, raw = request("POST", TOKEN_URL,
                          {"Content-Type": "application/x-www-form-urlencoded"}, body)
    if status != 200:
        raise AuthError("Google Photos login expired — run `gphotos-login`.")
    token = json.loads(raw).get("access_token")
    if not token:
        raise AuthError("Google Photos login expired — run `gphotos-login`.")
    return token


def _access_token(cfg, cache):
    with cache.lock:
        if not cache.access_token:
            cache.access_token = _refresh_access_token(cfg)
        return cache.access_token


def _invalidate(cache):
    with cache.lock:
        cache.access_token = None


def _api(cfg, cache, method, url, headers=None, body=None, timeout=120, attempts=5):
    """Authenticated call to a Photos API endpoint: attaches a bearer token,
    refreshes it once on a 401 and retries, and retries 429/5xx with
    backoff. Returns (status, raw_bytes) — 200 is the only success case for
    every endpoint used here, even batchCreate (per-item failures come back
    inside a 200 body)."""
    headers = dict(headers or {})
    refreshed = False
    status, raw = None, b""
    for attempt in range(attempts):
        headers["Authorization"] = "Bearer " + _access_token(cfg, cache)
        status, raw = request(method, url, headers, body, timeout=timeout)
        if status == 401 and not refreshed:
            _invalidate(cache)
            refreshed = True
            continue
        if status in (429, 500, 502, 503) and attempt < attempts - 1:
            time.sleep(0.6 * (attempt + 1))
            continue
        break
    return status, raw


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- OAuth login (installed-app loopback flow) -------------------------------
class _OAuthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in qs or "error" in qs:
            self.server.oauth_result = qs  # ignore stray requests (e.g. favicon)
        ok = "code" in qs
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = ("Signed in — you can close this tab and return to the terminal."
               if ok else "Google Photos sign-in failed — check the terminal.")
        self.wfile.write(f"<html><body>{msg}</body></html>".encode())

    def log_message(self, *a):
        pass  # keep the CLI's stdout clean


def run_oauth_flow(cfg, save_cfg, open_browser=True):
    """Interactively authorize this tool for Google Photos and store the
    refresh token in `cfg` (persisted via `save_cfg`). Mutates and returns
    `cfg`. Raises AuthError on any failure (state mismatch, denied consent,
    failed token exchange, or no refresh_token in the response — Google only
    issues one the *first* time a client is granted consent; if you've
    authorized this app before, revoke it at
    https://myaccount.google.com/permissions and try again)."""
    client_id = cfg.get("gphotos_client_id") or input("Google OAuth client id: ").strip()
    client_secret = cfg.get("gphotos_client_secret") or input("Google OAuth client secret: ").strip()
    cfg["gphotos_client_id"] = client_id
    cfg["gphotos_client_secret"] = client_secret

    server = http.server.HTTPServer(("127.0.0.1", 0), _OAuthHandler)
    server.oauth_result = None
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    state = secrets.token_urlsafe(16)
    params = {"client_id": client_id, "redirect_uri": redirect_uri,
              "response_type": "code", "access_type": "offline", "prompt": "consent",
              "scope": SCOPES, "state": state}
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)

    # flush=True: when stdout is a pipe/file (e.g. run under nohup or a
    # wrapper), block buffering would otherwise hide the URL until exit.
    print("Open this URL to authorize Google Photos access (or it should open "
          "automatically in your browser):", flush=True)
    print(url, flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print("Waiting for sign-in to complete ...", flush=True)
    while server.oauth_result is None:
        server.handle_request()  # blocks for exactly one HTTP request
    server.server_close()

    qs = server.oauth_result
    if qs.get("state", [None])[0] != state:
        raise AuthError("OAuth state mismatch (possible CSRF) — try again.")
    if "code" not in qs:
        raise AuthError(f"Google sign-in failed: {qs.get('error', ['unknown error'])[0]}")

    body = urllib.parse.urlencode({
        "code": qs["code"][0], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }).encode()
    status, raw = request("POST", TOKEN_URL,
                          {"Content-Type": "application/x-www-form-urlencoded"}, body)
    if status != 200:
        raise AuthError(f"Token exchange failed ({status}): {raw[:200]!r}")
    tok = json.loads(raw)
    refresh_token = tok.get("refresh_token")
    if not refresh_token:
        raise AuthError(
            "Google didn't return a refresh token. It only issues one the first "
            "time you grant consent to this client — if you've authorized it "
            "before, revoke access at https://myaccount.google.com/permissions "
            "and run `gphotos-login` again.")
    cfg["gphotos_refresh_token"] = refresh_token
    save_cfg(cfg)
    return cfg


# --- Albums ------------------------------------------------------------------
def _list_albums(cfg, cache):
    """Page through every album *this app created* (a Photos API
    restriction: it cannot see albums made by hand in the app, or by other
    apps — `excludeNonAppCreatedData` isn't even optional in practice)."""
    albums, page_token = [], None
    while True:
        url = ALBUMS_URL + "?pageSize=50&excludeNonAppCreatedData=true"
        if page_token:
            url += "&pageToken=" + urllib.parse.quote(page_token)
        status, raw = _api(cfg, cache, "GET", url)
        if status != 200:
            raise UploadError(f"listing albums failed ({status}): {raw[:200]!r}")
        data = json.loads(raw)
        albums.extend(data.get("albums", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return albums


def _create_album(cfg, cache, title):
    body = json.dumps({"album": {"title": title}}).encode()
    status, raw = _api(cfg, cache, "POST", ALBUMS_URL, {"Content-Type": "application/json"}, body)
    if status != 200:
        raise UploadError(f"creating album {title!r} failed ({status}): {raw[:200]!r}")
    return json.loads(raw)


def list_albums(cfg):
    """Public entry point for the `albums` CLI command (a one-shot call, so
    a fresh token cache is fine)."""
    return _list_albums(cfg, _Cache())


def resolve_album(cfg, cache, save_cfg, title=None, album_id=None, allow_network=True):
    """Return the album id to upload into for mode "album", in priority
    order: an explicit `album_id` > `gphotos_album_id` already cached in
    config > an existing app-created album with the wanted title > a newly
    created one. Any time a lookup/create actually happens, the result is
    cached into `cfg["gphotos_album_id"]` and persisted via `save_cfg` so
    later runs skip straight to the cached id.

    With `allow_network=False` (dry-run), only the first two — free —
    options are tried; if neither applies this returns None rather than
    making any API call, so dry-run performs zero requests and zero writes.
    """
    if album_id:
        return album_id
    if cfg.get("gphotos_album_id"):
        return cfg["gphotos_album_id"]
    if not allow_network:
        return None
    title = title or cfg.get("gphotos_album") or DEFAULT_ALBUM_TITLE
    for a in _list_albums(cfg, cache):
        if a.get("title") == title:
            cfg["gphotos_album_id"] = a["id"]
            save_cfg(cfg)
            return a["id"]
    created = _create_album(cfg, cache, title)
    cfg["gphotos_album_id"] = created["id"]
    save_cfg(cfg)
    return created["id"]


# --- Pending-item selection ----------------------------------------------------
def pending_items(state, out_dir):
    """Items eligible for upload: a file on disk, and either never uploaded
    or uploaded at a rendition that's since been superseded (an "upgraded"
    item is uploaded again as a new media item — see module docstring).
    Returns a list of (moment_id, entry) tuples, oldest date first."""
    pending = []
    for mid, e in state["items"].items():
        f = e.get("file")
        if not f or not os.path.isfile(os.path.join(out_dir, f)):
            continue
        g = e.get("gphotos")
        if g and g.get("rendition") == e.get("rendition"):
            continue  # already uploaded at the current rendition
        pending.append((mid, e))
    pending.sort(key=lambda kv: kv[1].get("date", ""))
    return pending


# --- Upload --------------------------------------------------------------------
def _upload_one_bytes(cfg, cache, out_dir, mid, entry):
    path = os.path.join(out_dir, entry["file"])
    ext = os.path.splitext(entry["file"])[1].lstrip(".").lower()
    mime = MIME_BY_EXT.get(ext, "application/octet-stream")
    with open(path, "rb") as f:
        data = f.read()
    headers = {"Content-Type": "application/octet-stream",
               "X-Goog-Upload-Content-Type": mime, "X-Goog-Upload-Protocol": "raw"}
    status, raw = _api(cfg, cache, "POST", UPLOAD_URL, headers, data, timeout=300)
    if status != 200:
        raise UploadError(f"byte upload of {entry['file']} failed ({status}): {raw[:200]!r}")
    return raw.decode()


def _batch_create(cfg, cache, new_media_items, album_id=None):
    payload = {"newMediaItems": new_media_items}
    if album_id:
        payload["albumId"] = album_id
    body = json.dumps(payload).encode()
    status, raw = _api(cfg, cache, "POST", BATCH_CREATE_URL,
                       {"Content-Type": "application/json"}, body)
    if status != 200:
        raise UploadError(f"batchCreate failed ({status}): {raw[:200]!r}")
    return json.loads(raw).get("newMediaItemResults", [])


def upload_pending(cfg, save_cfg, state, out_dir, checkpoint, mode=None,
                    album_title=None, album_id=None, workers=3, limit=None, dry_run=False):
    """Upload every pending item (see `pending_items`) to Google Photos.
    Mutates `state` in place (adding/replacing each uploaded item's
    "gphotos" key) and calls `checkpoint()` after every batch of up to
    `MAX_BATCH` items so progress survives an interruption. Byte uploads run
    in a thread pool of `workers`; each batchCreate call happens on the
    calling thread. `limit` caps how many candidates are considered (handy
    for a first cautious run). `dry_run=True` performs zero network calls
    and zero writes.

    Returns {"uploaded": N, "failed": N, "album_id": id-or-None,
    "candidates": N}. Raises AuthError if credentials are missing/invalid —
    the caller should stop and point the user at `gphotos-login`.
    """
    mode = mode or cfg.get("gphotos_mode", "off")
    if mode == "off":
        return {"uploaded": 0, "failed": 0, "album_id": None, "candidates": 0}

    items = pending_items(state, out_dir)
    if limit is not None:
        items = items[:limit]

    if dry_run:
        resolved = (resolve_album(cfg, _Cache(), save_cfg, album_title, album_id,
                                   allow_network=False)
                    if mode == "album" else None)
        return {"uploaded": 0, "failed": 0, "album_id": resolved, "candidates": len(items)}

    cache = _Cache()
    _access_token(cfg, cache)  # fail fast (AuthError) before doing any work
    resolved_album_id = (resolve_album(cfg, cache, save_cfg, album_title, album_id)
                          if mode == "album" else None)

    uploaded = failed = 0
    for start in range(0, len(items), MAX_BATCH):
        batch = items[start:start + MAX_BATCH]
        by_mid = dict(batch)
        tokens = {}
        with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(_upload_one_bytes, cfg, cache, out_dir, mid, e): mid
                    for mid, e in batch}
            for fut in cf.as_completed(futs):
                mid = futs[fut]
                try:
                    tokens[mid] = fut.result()
                except AuthError:
                    raise
                except Exception:
                    failed += 1

        ok_mids = [mid for mid, _ in batch if mid in tokens]
        if ok_mids:
            new_media_items = []
            for mid in ok_mids:
                e = by_mid[mid]
                nmi = {"simpleMediaItem": {"fileName": os.path.basename(e["file"]),
                                            "uploadToken": tokens[mid]}}
                desc = (e.get("caption") or "").strip()[:1000]
                if desc:
                    nmi["description"] = desc
                new_media_items.append(nmi)
            results = _batch_create(cfg, cache, new_media_items, resolved_album_id)
            for mid, res in zip(ok_mids, results):
                item_id = res.get("mediaItem", {}).get("id")
                if res.get("status", {}).get("message") == "Success" or item_id:
                    state["items"][mid]["gphotos"] = {
                        "id": item_id, "rendition": by_mid[mid].get("rendition"),
                        "album_id": resolved_album_id, "at": _now_iso(),
                    }
                    uploaded += 1
                else:
                    failed += 1
        checkpoint()

    return {"uploaded": uploaded, "failed": failed, "album_id": resolved_album_id,
            "candidates": len(items)}


def status_summary(cfg, state, out_dir):
    """Summary used by the `status` CLI command."""
    return {
        "mode": cfg.get("gphotos_mode", "off"),
        "album": cfg.get("gphotos_album") or DEFAULT_ALBUM_TITLE,
        "logged_in": bool(cfg.get("gphotos_refresh_token")),
        "pending": len(pending_items(state, out_dir)),
    }
