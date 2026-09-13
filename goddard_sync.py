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
    public CDN (no auth needed once you know the path).
  * "Draft" graphics (newsletter/invitation art under a `/drafts/` path) have no
    full original; for those we fall back to the `_display.jpg` rendition.

Stdlib only — no third-party dependencies.
"""
from __future__ import annotations
import argparse, concurrent.futures as cf, csv, getpass, json, os, re, sys, time
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


# --- Download --------------------------------------------------------------
def _dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _filename(date, moment_id):
    d = _dt(date)
    prefix = d.strftime("%Y-%m-%d_%H%M%S") if d else "nodate"
    return f"{prefix}_{moment_id}.jpg"


def _image_items(results):
    items = {}
    for r in results:
        if r.get("type") != "moment":
            continue
        for m in r.get("moments", []):
            if m.get("type") != "image":
                continue
            url = m.get("thumbnailTransformed") or m.get("thumbnail") or ""
            if not url.startswith("http"):
                url = CDN + url
            items[m["_id"]] = {
                "id": m["_id"],
                "base": re.sub(r"_thumb(\.\w+)$", "", url),
                "is_draft": "/drafts/" in url,
                "date": r.get("date", ""),
                "caption": (r.get("caption") or m.get("caption") or "").strip(),
            }
    return list(items.values())


def _download_one(item, out_dir):
    path = os.path.join(out_dir, _filename(item["date"], item["id"]))
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return ("skip", path, 0)
    # priority: full-res original, then display rendition, then thumbnail
    candidates = ([] if item["is_draft"] else [item["base"] + ".jpg"])
    candidates += [item["base"] + "_display.jpg", item["base"] + "_thumb.jpg"]
    for url in candidates:
        for attempt in range(5):
            try:
                data = _http(url, timeout=120)
                if isinstance(data, (bytes, bytearray)) and len(data) > 1000:
                    tmp = path + ".part"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, path)
                    return ("ok", path, len(data))
                break  # too small — try next candidate
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503):  # transient/throttle — retry
                    time.sleep(0.6 * (attempt + 1))
                    continue
                # 403 (often S3 "InvalidObjectState": the full-res original has
                # been lifecycled into Glacier Deep Archive and can't be fetched
                # directly) or 404 — permanent, so don't retry; fall through to
                # the next candidate (display rendition, then thumbnail).
                break
            except Exception:
                time.sleep(0.6 * (attempt + 1))
    return ("err", path, 0)


def cmd_sync(args):
    cfg = load_config(args.config)
    token = cfg.get("token")
    if not token:
        print("No token found. Run `goddard_sync.py login` first.", file=sys.stderr)
        return 2
    out_dir = os.path.expanduser(args.output_dir or cfg["output_dir"])
    os.makedirs(out_dir, exist_ok=True)

    try:
        results = fetch_feed(token)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print("Token rejected (expired?). Re-run `goddard_sync.py login`.",
                  file=sys.stderr)
            return 2
        raise

    items = _image_items(results)
    print(f"{len(items)} images in feed -> {out_dir}")
    ok = skip = err = 0
    new_bytes = 0
    workers = max(1, args.workers)
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_download_one, it, out_dir) for it in items]
        for i, fut in enumerate(cf.as_completed(futs), 1):
            status, _path, size = fut.result()
            if status == "ok":
                ok += 1; new_bytes += size
            elif status == "skip":
                skip += 1
            else:
                err += 1
            if i % 100 == 0 or i == len(items):
                print(f"  {i}/{len(items)}  new={ok} existing={skip} failed={err}",
                      flush=True)

    _write_manifest(out_dir, items)
    summary = f"{ok} new photo(s), {skip} already present, {err} failed. Total in library: {len(items)}."
    print("Sync complete:", summary)
    if ok and cfg.get("ntfy_topic"):
        _notify(cfg, f"Goddard: {ok} new photo(s) synced", summary)
    return 1 if err and not ok else 0


def _write_manifest(out_dir, items):
    with open(os.path.join(out_dir, "manifest.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "moment_id", "date", "caption"])
        for it in items:
            w.writerow([_filename(it["date"], it["id"]), it["id"], it["date"], it["caption"]])


def _notify(cfg, title, message):
    try:
        url = cfg["ntfy_server"].rstrip("/") + "/" + cfg["ntfy_topic"]
        req = urllib.request.Request(url, data=message.encode(),
                                     headers={"Title": title, "User-Agent": USER_AGENT})
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print("(ntfy notification failed:", e, ")", file=sys.stderr)


def cmd_status(args):
    cfg = load_config(args.config)
    out_dir = os.path.expanduser(cfg["output_dir"])
    n = len([f for f in os.listdir(out_dir) if f.endswith(".jpg")]) if os.path.isdir(out_dir) else 0
    print(f"config file : {args.config} ({'exists' if os.path.exists(args.config) else 'missing'})")
    print(f"username    : {cfg.get('username') or '(not set)'}")
    print(f"token       : {'present' if cfg.get('token') else '(not set — run login)'}")
    print(f"output dir  : {out_dir}")
    print(f"photos      : {n}")
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
    ps.set_defaults(func=cmd_sync)

    pt = sub.add_parser("status", parents=[common],
                        help="show current configuration and photo count")
    pt.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
