#!/usr/bin/env python3
"""
goddard_sync — bulk / incremental photo export for the Goddard Family Hub app.

The Goddard Family Hub Android/iOS app is a React Native front-end over Kaymbu's
REST API. This tool talks to that same API to download every photo ("moment")
the account can see, at full resolution, and to keep a local folder in sync.

Auth is passwordless: you enter your phone or email, Kaymbu texts/emails a
4-digit code, and you exchange it for a long-lived bearer token. You do that
once with `login`; the token is stored locally and reused by `sync`, which is
what you schedule (e.g. a daily systemd timer or cron job).

Only your own account's data is accessed — the same photos the app shows you.

Design notes / how the media URLs work:
  * The feed (`POST /feed`) returns "moment" results, each with image entries
    whose `thumbnailTransformed` URL ends in `_thumb.jpg`.
  * Dropping the `_thumb` suffix yields the full-resolution original on the
    public CDN (no auth needed once you know the path) — *when* it's still
    available; Kaymbu lifecycles older originals into Glacier, so the same URL
    can start returning the original weeks after only a lower rendition was
    fetchable. A per-item state file lets `sync` notice and upgrade later.
  * "Draft" graphics (newsletter/invitation art under a `/drafts/` path) have no
    full original; for those we fall back to the `_display.jpg` rendition.
  * Some "full-res" originals are actually served as HEIC bytes at the `.jpg`
    URL (content-type image/heic). We sniff the downloaded bytes and name the
    file accordingly instead of trusting the URL's extension.

A hidden `.goddard-state.json` file in the output directory is the source of
truth for what's been downloaded and at what rendition — see `_load_state`.

Stdlib only — no third-party dependencies.
"""
from __future__ import annotations
import argparse, concurrent.futures as cf, csv, json, os, re, sys, time
import urllib.request, urllib.error
from datetime import datetime, timezone

# --- Constants describing the app/API -------------------------------------
API_BASE = "https://hq.kaymbu.com/kaymbu-parentapp/api"
AUTH_URL = "https://hq.kaymbu.com/auth/authenticate"
CDN      = "https://d2k9f6tk478nyp.cloudfront.net"
APP_VERSION = "3.4.1.28"

# OAuth client credentials shipped *inside* the public Goddard Family Hub app
# (flavor "goddard"). These are the same for every user and are trivially
# extractable from the APK, so they are not secret. They are overridable via
# config in case Goddard rotates them. Your personal token is NOT stored here.
DEFAULT_CLIENT_ID     = "goddard-family-hub"
DEFAULT_CLIENT_SECRET = "9Odd4rd-f4mIlY-hu8!"

DEFAULT_CONFIG = os.path.expanduser("~/.config/goddard-photo-sync/config.json")
USER_AGENT = "okhttp/4.9"

# State file lives inside the output dir (hidden so photo apps ignore it).
STATE_FILENAME = ".goddard-state.json"


# --- Config ----------------------------------------------------------------
def load_config(path: str) -> dict:
    cfg = {}
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
    # environment overrides (handy for CI / containers)
    for key, env in (("username", "GODDARD_USERNAME"),
                     ("token", "GODDARD_TOKEN"),
                     ("output_dir", "GODDARD_OUTPUT_DIR"),
                     ("client_id", "GODDARD_CLIENT_ID"),
                     ("client_secret", "GODDARD_CLIENT_SECRET"),
                     ("ntfy_topic", "GODDARD_NTFY_TOPIC"),
                     ("ntfy_server", "GODDARD_NTFY_SERVER")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg.setdefault("client_id", DEFAULT_CLIENT_ID)
    cfg.setdefault("client_secret", DEFAULT_CLIENT_SECRET)
    cfg.setdefault("output_dir", "~/Pictures/Goddard")
    cfg.setdefault("ntfy_server", "https://ntfy.sh")
    cfg.setdefault("ntfy_topic", None)
    return cfg


def save_config(path: str, cfg: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # do not persist defaults for client creds unless explicitly customized
    out = dict(cfg)
    if out.get("client_id") == DEFAULT_CLIENT_ID:
        out.pop("client_id", None)
    if out.get("client_secret") == DEFAULT_CLIENT_SECRET:
        out.pop("client_secret", None)
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    os.chmod(path, 0o600)  # token lives here — keep it private


# --- HTTP helpers ----------------------------------------------------------
def _http(url, method="GET", body=None, token=None, timeout=60):
    headers = {"x-parentapp-version": APP_VERSION, "accept": "application/json",
               "User-Agent": USER_AGENT}
    data = None
    if body is not None:
        headers["content-type"] = "application/json;charset=utf-8"
        data = json.dumps(body).encode()
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        ctype = resp.headers.get("content-type", "")
        return json.loads(raw) if raw and ctype.startswith("application/json") else raw


def _http_head(url, timeout=30, attempts=3):
    """HEAD a media URL and return {"length": <int|None>}. Raises HTTPError on
    a non-2xx status (403/404 in particular, which callers treat specially);
    transient errors (429/5xx, network) are retried a few times first."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                cl = resp.headers.get("Content-Length")
                return {"length": int(cl) if cl is not None else None}
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503) or attempt == attempts - 1:
                raise
        except Exception:
            if attempt == attempts - 1:
                raise
        time.sleep(0.6 * (attempt + 1))


def _fetch_with_retry(url, attempts=5, timeout=120, min_size=1000):
    """GET url, retrying on transient errors. Returns (data, permanent):
      data      -- bytes on success, else None
      permanent -- True iff failure was an explicit 403/404. Notably, a
                   Glacier Deep Archive original can HEAD 200 with a real
                   Content-Length yet still GET 403 "InvalidObjectState"
                   (S3 serves cached metadata via HEAD without needing a
                   restore); callers use this flag to tell "truly
                   unavailable right now" apart from a network hiccup.
    """
    for attempt in range(attempts):
        try:
            data = _http(url, timeout=timeout)
            if isinstance(data, (bytes, bytearray)) and len(data) > min_size:
                return data, False
            return None, False  # too small — treat as unavailable, don't retry
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):  # transient/throttle — retry
                time.sleep(0.6 * (attempt + 1))
                continue
            # 403 (often S3 "InvalidObjectState": lifecycled into Glacier Deep
            # Archive) or 404 — permanent, don't retry.
            return None, True
        except Exception:
            time.sleep(0.6 * (attempt + 1))
    return None, False


# --- Auth ------------------------------------------------------------------
def cmd_login(args):
    cfg = load_config(args.config)
    username = args.user or cfg.get("username") or input("Phone or email: ").strip()
    print(f"Requesting a verification code for {username} ...")
    _http(f"{API_BASE}/auth/code", "POST", {"username": username})
    code = args.code or input("Enter the code you received: ").strip()
    print("Exchanging code for a token ...")
    resp = _http(AUTH_URL, "POST", {
        "username": username, "password": code, "grant_type": "password",
        "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
    })
    token = resp.get("access_token")
    if not token:
        print("Login failed: no access_token in response:", resp, file=sys.stderr)
        return 1
    cfg["username"] = username
    cfg["token"] = token
    if args.output_dir:
        cfg["output_dir"] = args.output_dir
    save_config(args.config, cfg)
    print(f"Logged in. Token saved to {args.config}")
    print(f"Photos will sync to: {cfg['output_dir']}")
    print("You can now run:  goddard_sync.py sync")
    return 0


# --- Feed ------------------------------------------------------------------
def fetch_feed(token):
    """Page through the whole feed (newest first) and return all results."""
    results, after = [], None
    while True:
        path = "/feed" + (f"?after={after}" if after else "")
        body = {"types": [], "students": []}
        if after:
            body["after"] = after
        data = _http(API_BASE + path, "POST", body, token=token)
        page = data.get("results", [])
        results.extend(page)
        if not page or not data.get("moreResults"):
            break
        after = page[-1].get("searchAfter")
        if not after:
            break
        time.sleep(0.2)
    return results


# --- Filenames / dates ------------------------------------------------------
def _dt(s):
    """Parse a feed ISO date string to an aware UTC datetime, or None."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _local_dt(s):
    """Same as _dt, converted to the machine's local timezone. Feed dates are
    UTC; EXIF capture times (and how a parent would think of "that evening's
    photos") are local, so filenames should sort/group by local date."""
    d = _dt(s)
    return d.astimezone() if d else None


def _filename(date, moment_id, ext="jpg"):
    d = _local_dt(date)
    prefix = d.strftime("%Y-%m-%d_%H%M%S") if d else "nodate"
    return f"{prefix}_{moment_id}.{ext}"


def _sniff_ext(data, default="jpg"):
    """Determine a media file's real extension from its bytes, since Kaymbu
    sometimes serves HEIC (or other) bytes at a URL that ends in .jpg."""
    if not data or len(data) < 12:
        return default
    if data[0:2] == b"\xff\xd8":
        return "jpg"
    if data[0:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"heif", b"hevc"):
        return "heic"
    return default


def _atomic_write(path, data):
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _set_mtime(path, date_iso):
    """Set a downloaded file's mtime to its feed date, so the filesystem
    timestamp matches reality even though we wrote the bytes just now."""
    d = _dt(date_iso)
    if d:
        ts = d.timestamp()
        os.utime(path, (ts, ts))


# --- State file --------------------------------------------------------------
def _state_path(out_dir):
    return os.path.join(out_dir, STATE_FILENAME)


def _load_state(out_dir):
    """Returns (state, existed). `existed` is False the very first time this
    runs against a given output dir — that's when the one-time migration of
    pre-state-file downloads happens."""
    path = _state_path(out_dir)
    if not os.path.exists(path):
        return {"version": 1, "items": {}}, False
    with open(path) as f:
        state = json.load(f)
    state.setdefault("version", 1)
    state.setdefault("items", {})
    return state, True


def _save_state_atomic(out_dir, state):
    path = _state_path(out_dir)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _present_ids(state, out_dir):
    """Ids the state file says we already have *and* that still exist on
    disk (a user may have deleted a file — then we re-download it)."""
    present = set()
    for mid, entry in state["items"].items():
        f = entry.get("file")
        if f and os.path.isfile(os.path.join(out_dir, f)):
            present.add(mid)
        elif entry.get("rendition") == "unavailable":
            present.add(mid)  # nothing on disk yet; the upgrade pass retries it
    return present


def _migrate_existing_files(out_dir, items, state):
    """One-time migration for folders downloaded before the state file existed:
    for every feed item whose id matches a file already on disk (any name
    ending in ``..._<id>.<ext>``), rename it to the canonical local-time name
    (sniffing the real bytes for the true extension — this is what fixes
    HEIC-as-.jpg files) and record a state entry with rendition "unknown" so
    the upgrade pass revisits it. Files on disk that aren't in the feed are
    left completely alone."""
    if not os.path.isdir(out_dir):
        return 0
    by_id = {}
    for name in os.listdir(out_dir):
        stem, ext = os.path.splitext(name)
        if ext.lower() in (".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov") and "_" in stem:
            by_id[stem.rsplit("_", 1)[-1]] = name
    migrated = 0
    for it in items:
        old_name = by_id.get(it["id"])
        if not old_name:
            continue
        old_path = os.path.join(out_dir, old_name)
        if not os.path.isfile(old_path):
            continue
        with open(old_path, "rb") as f:
            head = f.read(12)
        size = os.path.getsize(old_path)
        ext = "mp4" if it["type"] == "video" else _sniff_ext(head)
        new_name = _filename(it["date"], it["id"], ext)
        new_path = os.path.join(out_dir, new_name)
        if new_path != old_path:
            os.replace(old_path, new_path)
        _set_mtime(new_path, it["date"])
        state["items"][it["id"]] = {
            "file": new_name, "type": it["type"], "rendition": "unknown",
            "draft": it["is_draft"], "date": it["date"], "caption": it["caption"],
            "bytes": size,
        }
        migrated += 1
    return migrated


# --- Feed items --------------------------------------------------------------
def _media_items(results):
    """Collect both image and video moments. Videos have no usable `base`
    URL from the feed (only a `_thumb.jpg` still) — the actual file has to
    come from the detail endpoint, done in `_download_video`."""
    items = {}
    for r in results:
        if r.get("type") != "moment":
            continue
        for m in r.get("moments", []):
            mtype = m.get("type")
            if mtype not in ("image", "video"):
                continue
            url = m.get("thumbnailTransformed") or m.get("thumbnail") or ""
            if not url.startswith("http"):
                url = CDN + url
            items[m["_id"]] = {
                "id": m["_id"],
                "type": mtype,
                "base": re.sub(r"_thumb(\.\w+)$", "", url),
                "is_draft": "/drafts/" in url,
                "date": r.get("date", ""),
                "caption": (r.get("caption") or m.get("caption") or "").strip(),
            }
    return list(items.values())


# --- Download ----------------------------------------------------------------
def _download_one(item, out_dir, token):
    """Download one new (not-yet-present) media item. Returns a dict:
    {"status": "ok"|"unavailable"|"err", "file", "rendition", "bytes", "type"}
    — the caller uses this to build the item's state entry. "unavailable"
    means the CDN answered a definitive 403/404 for every candidate (e.g. a
    video whose only file was archived): it is recorded in the state so the
    upgrade pass quietly retries it on later runs, rather than being reported
    as a failure every day."""
    if item["type"] == "video":
        return _download_video(item, out_dir, token)
    return _download_image(item, out_dir)


def _download_image(item, out_dir):
    candidates = ([] if item["is_draft"] else [("original", item["base"] + ".jpg")])
    candidates += [("display", item["base"] + "_display.jpg"),
                   ("thumb", item["base"] + "_thumb.jpg")]
    permanent = True
    for rendition, url in candidates:
        data, perm = _fetch_with_retry(url)
        if data is None:
            permanent = permanent and perm
            continue
        ext = _sniff_ext(data)
        fname = _filename(item["date"], item["id"], ext)
        path = os.path.join(out_dir, fname)
        _atomic_write(path, data)
        _set_mtime(path, item["date"])
        return {"status": "ok", "file": fname, "rendition": rendition,
                "bytes": len(data), "type": "image"}
    return {"status": "unavailable" if permanent else "err", "type": "image"}


def _download_video(item, out_dir, token):
    """Videos aren't in the feed directly — call the detail endpoint for the
    moment id to get the actual CDN path, then download it like a photo (no
    auth needed for the CDN itself, and no lower-rendition fallback exists;
    a 403 — e.g. a lifecycled original — is recorded as a plain failure)."""
    try:
        d = _http(f"{API_BASE}/feed/details/moment/{item['id']}", token=token)
    except Exception:
        return {"status": "err"}
    src = (d.get("videoSource") or d.get("videoLowRendition")) if isinstance(d, dict) else None
    if not src:
        return {"status": "err"}
    url = src if src.startswith("http") else CDN + src
    data, permanent = _fetch_with_retry(url, timeout=300)
    if data is None:
        return {"status": "unavailable" if permanent else "err", "type": "video"}
    fname = _filename(item["date"], item["id"], "mp4")
    path = os.path.join(out_dir, fname)
    _atomic_write(path, data)
    _set_mtime(path, item["date"])
    return {"status": "ok", "file": fname, "rendition": "original",
            "bytes": len(data), "type": "video"}


def _upgrade_one(mid, entry, item, out_dir, token):
    """Check (and if needed, fetch) whether a lower-rendition item now has a
    full-res original available. Returns (kind, changes):
      kind    -- "upgraded" | "nochange" | "skip" | "err"
      changes -- dict of state fields to merge into the entry, or None
    "skip" means the original is still unavailable (403/404) — rendition is
    left as-is so it's retried again next run."""
    if entry.get("rendition") == "unavailable":
        # Nothing on disk at all (every candidate 403/404'd last time, or an
        # archived video): just try the normal download again.
        res = _download_one(item, out_dir, token)
        if res["status"] == "ok":
            return ("upgraded", {"file": res["file"], "rendition": res["rendition"],
                                 "bytes": res["bytes"]})
        return ("skip", None) if res["status"] == "unavailable" else ("err", None)
    url = item["base"] + ".jpg"
    old_path = os.path.join(out_dir, entry.get("file", ""))
    old_size = os.path.getsize(old_path) if os.path.isfile(old_path) else -1
    try:
        head = _http_head(url)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return ("skip", None)
        return ("err", None)
    except Exception:
        return ("err", None)
    if head["length"] is not None and head["length"] == old_size:
        # Already full-res on disk — just a mislabeled ("unknown") entry.
        return ("nochange", {"rendition": "original"})
    data, permanent = _fetch_with_retry(url, timeout=180)
    if data is None:
        # A HEAD 200 doesn't guarantee a GET succeeds: a Deep Archive object
        # still 403s "InvalidObjectState" on GET even though HEAD reports its
        # (cached) metadata. Treat that the same as a HEAD 403/404 — leave
        # rendition as-is and retry next run.
        return ("skip", None) if permanent else ("err", None)
    ext = _sniff_ext(data)
    new_name = _filename(item["date"], mid, ext)
    new_path = os.path.join(out_dir, new_name)
    _atomic_write(new_path, data)
    _set_mtime(new_path, item["date"])
    if new_path != old_path and os.path.isfile(old_path):
        os.remove(old_path)
    return ("upgraded", {"file": new_name, "rendition": "original", "bytes": len(data)})


def cmd_sync(args):
    cfg = load_config(args.config)
    token = cfg.get("token")
    if not token:
        print("No token found. Run `goddard_sync.py login` first.", file=sys.stderr)
        return 2
    out_dir = os.path.expanduser(args.output_dir or cfg["output_dir"])
    os.makedirs(out_dir, exist_ok=True)

    try:
        return _run_sync(args, cfg, token, out_dir)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print("Token rejected (expired?). Re-run `goddard_sync.py login`.",
                  file=sys.stderr)
            _notify(cfg, "Goddard sync FAILED", f"Token rejected ({e.code}) fetching "
                    "the feed. Re-run `goddard_sync.py login`.", priority="high")
            return 2
        _notify(cfg, "Goddard sync FAILED", f"Unexpected HTTP error: {e}", priority="high")
        raise
    except Exception as e:
        _notify(cfg, "Goddard sync FAILED", f"Unexpected error: {e}", priority="high")
        raise


def _run_sync(args, cfg, token, out_dir):
    results = fetch_feed(token)
    items = _media_items(results)
    state, existed = _load_state(out_dir)

    if not existed:
        migrated = _migrate_existing_files(out_dir, items, state)
        if migrated:
            print(f"Migrated {migrated} existing file(s) to the local-time naming scheme.")
            _save_state_atomic(out_dir, state)

    present = _present_ids(state, out_dir)
    new_items = [it for it in items if it["id"] not in present]

    print(f"{len(items)} media item(s) in feed -> {out_dir}")
    ok = err = unavailable = 0
    new_bytes = 0
    workers = max(1, args.workers)
    checkpoint_every = 100
    done_since_save = 0

    def _checkpoint():
        nonlocal done_since_save
        done_since_save += 1
        if done_since_save >= checkpoint_every:
            _save_state_atomic(out_dir, state)
            done_since_save = 0

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_download_one, it, out_dir, token): it for it in new_items}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            it = futs[fut]
            res = fut.result()
            if res["status"] == "ok":
                ok += 1
                new_bytes += res["bytes"]
                state["items"][it["id"]] = {
                    "file": res["file"], "type": res["type"], "rendition": res["rendition"],
                    "draft": it["is_draft"], "date": it["date"], "caption": it["caption"],
                    "bytes": res["bytes"],
                }
                _checkpoint()
            elif res["status"] == "unavailable":
                unavailable += 1
                state["items"][it["id"]] = {
                    "file": None, "type": res["type"], "rendition": "unavailable",
                    "draft": it["is_draft"], "date": it["date"], "caption": it["caption"],
                    "bytes": 0,
                }
            else:
                err += 1
            if not args.quiet and (i % 100 == 0 or i == len(new_items)):
                print(f"  new {i}/{len(new_items)}  ok={ok} failed={err}", flush=True)

        # Upgrade pass: revisit anything not already known to be full-res
        # (including "unknown" entries from migration) now that a Glacier
        # original may have thawed, or so a migrated file gets fixed up.
        items_by_id = {it["id"]: it for it in items}
        upgrade_targets = [
            (mid, e) for mid, e in state["items"].items()
            if mid in items_by_id and e.get("rendition") != "original"
            and (e.get("rendition") == "unavailable"
                 or (e.get("type") == "image" and not e.get("draft")))
        ]
        upgraded = 0
        ufuts = {ex.submit(_upgrade_one, mid, e, items_by_id[mid], out_dir, token): mid
                 for mid, e in upgrade_targets}
        for i, fut in enumerate(cf.as_completed(ufuts), 1):
            mid = ufuts[fut]
            kind, changes = fut.result()
            if kind == "upgraded":
                state["items"][mid].update(changes)
                upgraded += 1
                _checkpoint()
            elif kind == "nochange":
                state["items"][mid].update(changes)
            elif kind == "err":
                err += 1
            # "skip" (still 403/404): leave rendition as-is, retried next run.
            if not args.quiet and (i % 100 == 0 or i == len(upgrade_targets)):
                print(f"  upgrade {i}/{len(upgrade_targets)}  upgraded={upgraded}", flush=True)

    _write_manifest(out_dir, state)
    _save_state_atomic(out_dir, state)

    n_photos = sum(1 for e in state["items"].values() if e.get("type") == "image")
    n_videos = sum(1 for e in state["items"].values() if e.get("type") == "video")
    already = len(items) - len(new_items)
    n_unavail = sum(1 for e in state["items"].values() if e.get("rendition") == "unavailable")
    summary = (f"{ok} new, {upgraded} upgraded to full-res, {already} already present, "
               f"{err} failed. Total in library: {n_photos} photos, {n_videos} videos"
               + (f" ({n_unavail} still archived upstream)." if n_unavail else "."))
    print("Sync complete:", summary)
    if err:
        _notify(cfg, f"Goddard sync: {err} failed", summary, priority="high")
    elif ok or upgraded:
        _notify(cfg, f"Goddard: {ok} new, {upgraded} upgraded", summary)
    return 1 if err else 0


def _write_manifest(out_dir, state):
    rows = sorted(state["items"].items(), key=lambda kv: kv[1].get("date", ""))
    with open(os.path.join(out_dir, "manifest.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "moment_id", "type", "date", "rendition", "caption"])
        for mid, e in rows:
            w.writerow([e.get("file", ""), mid, e.get("type", ""), e.get("date", ""),
                        e.get("rendition", ""), e.get("caption", "")])


def _notify(cfg, title, message, priority=None):
    if not cfg.get("ntfy_topic"):
        return
    try:
        url = cfg["ntfy_server"].rstrip("/") + "/" + cfg["ntfy_topic"]
        headers = {"Title": title, "User-Agent": USER_AGENT}
        if priority:
            headers["Priority"] = priority
        req = urllib.request.Request(url, data=message.encode(), headers=headers)
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print("(ntfy notification failed:", e, ")", file=sys.stderr)


def cmd_status(args):
    cfg = load_config(args.config)
    out_dir = os.path.expanduser(cfg["output_dir"])
    state, existed = _load_state(out_dir) if os.path.isdir(out_dir) else ({"items": {}}, False)
    photos = sum(1 for e in state["items"].values() if e.get("type") == "image")
    videos = sum(1 for e in state["items"].values() if e.get("type") == "video")
    not_full = sum(1 for e in state["items"].values()
                   if e.get("type") == "image" and not e.get("draft")
                   and e.get("rendition") != "original")
    print(f"config file : {args.config} ({'exists' if os.path.exists(args.config) else 'missing'})")
    print(f"username    : {cfg.get('username') or '(not set)'}")
    print(f"token       : {'present' if cfg.get('token') else '(not set — run login)'}")
    print(f"output dir  : {out_dir}")
    unavail = sum(1 for e in state["items"].values() if e.get("rendition") == "unavailable")
    print(f"photos      : {photos} ({not_full} not full-res)")
    print(f"videos      : {videos}" + (f" ({unavail} item(s) archived upstream, not yet downloadable)" if unavail else ""))
    print(f"state file  : {_state_path(out_dir)} ({'exists' if existed else 'missing'})")
    print(f"ntfy topic  : {cfg.get('ntfy_topic') or '(disabled)'}")
    return 0


def main(argv=None):
    # --config is shared so it works both before and after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"config file path (default: {DEFAULT_CONFIG})")

    p = argparse.ArgumentParser(
        prog="goddard_sync", parents=[common],
        description="Bulk/incremental photo export for the Goddard Family Hub (Kaymbu) app.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("login", parents=[common],
                        help="interactive one-time login (stores a token)")
    pl.add_argument("--user", help="phone number or email")
    pl.add_argument("--code", help="verification code (skip the interactive prompt)")
    pl.add_argument("--output-dir", help="where photos should be saved")
    pl.set_defaults(func=cmd_login)

    ps = sub.add_parser("sync", parents=[common],
                        help="download any new full-resolution photos")
    ps.add_argument("--output-dir", help="override the configured output dir")
    ps.add_argument("--workers", type=int, default=4, help="parallel downloads (default 4)")
    ps.add_argument("--quiet", action="store_true",
                    help="suppress per-100 progress lines (handy under systemd)")
    ps.set_defaults(func=cmd_sync)

    pt = sub.add_parser("status", parents=[common],
                        help="show current configuration and photo count")
    pt.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
