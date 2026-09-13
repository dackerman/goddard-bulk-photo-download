# goddard-bulk-photo-download

Download every photo *and video* of your child from the **Goddard Family Hub**
app, at full resolution, and keep a local folder in sync automatically.

The Goddard Family Hub app shows you photos your daycare posts, but gives you no
way to bulk-export them — you can only save pictures one at a time. This tool
talks to the same backend API the app uses and downloads them all, then runs on
a schedule to pull each new day's photos.

It only ever accesses **your own account's** data: the exact photos the app
already shows you, using your own login.

> Unofficial. Not affiliated with, endorsed by, or supported by Goddard Systems,
> The Goddard School, or Kaymbu. Use it with your own account and your own data.

## How it works

The app is a React Native front-end over [Kaymbu](https://kaymbu.com)'s REST
API. This tool reproduces the calls that matter:

1. **Login (passwordless).** You submit your phone or email; Kaymbu sends a
   4-digit code; you exchange the code for a bearer token. The token is
   long-lived, so you do this once.
2. **Feed.** `POST /feed` returns your timeline of "moments" (photo/video
   posts), paged with a cursor. Each image comes back as a `..._thumb.jpg` URL
   on a public CDN; dropping the `_thumb` suffix *usually* yields the
   full-resolution original — though Kaymbu can serve some originals as HEIC
   bytes at that same `.jpg` URL, and older originals can be archived (see
   below), so `sync` sniffs the downloaded bytes rather than trusting the URL.
3. **Video detail endpoint.** A video moment only exposes a thumbnail still in
   the feed; `GET /feed/details/moment/<id>` returns the actual `.mp4` path.

`login` stores the token locally; `sync` reads the feed and downloads any
photos/videos you don't already have, at the best rendition currently
available. `sync` is what you schedule.

## Requirements

- Python 3.9+ (standard library only — no `pip install` needed)
- A Goddard Family Hub account

## Setup

```bash
git clone <this-repo> ~/code/goddard-bulk-photo-download
cd ~/code/goddard-bulk-photo-download

# One-time interactive login. Sends a code to your phone/email, then stores a token.
./goddard_sync.py login --user 555-123-4567 --output-dir ~/Pictures/Goddard
```

`login` writes a private config file to `~/.config/goddard-photo-sync/config.json`
(mode `600`) containing your username and token. Nothing sensitive is ever
written inside this repo.

## Usage

```bash
# Download everything new (safe to run repeatedly — it skips what you already have)
./goddard_sync.py sync

# See where things stand
./goddard_sync.py status
```

The first run downloads your entire history (this can be several GB). Files
are named `YYYY-MM-DD_HHMMSS_<id>.<ext>`, using the capture time converted to
**your machine's local timezone** (the feed reports UTC, which otherwise
pushes evening posts onto the next day's date) — so they sort chronologically
and roughly match the photo's actual EXIF time. `<ext>` is `jpg`, `heic`, `png`,
or `mp4`, determined by sniffing the downloaded bytes, not by guessing from the
URL. A `manifest.csv` (file, moment id, type, date, rendition, caption) is
written alongside them, and each file's mtime is set to its feed date.

A typical run looks like:

```
943 media item(s) in feed -> /home/you/Pictures/Goddard
  new 3/3  ok=3 failed=0
  upgrade 392/392  upgraded=12
Sync complete: 3 new, 12 upgraded to full-res, 928 already present, 0 failed. Total in library: 940 photos, 3 videos.
```

- **new** — items not seen before.
- **upgraded to full-res** — items previously saved at a lower rendition
  (`_display`/`_thumb`) whose full-resolution original has since become
  available and was re-downloaded in place of the old file. This runs on
  *every* sync, so a photo that was archived on day one can be silently
  upgraded weeks later once it thaws (see Limitations).
- **already present** — everything else: already full-res, a draft graphic (no
  original exists for those), or an archived original that's still
  unavailable (it'll be retried again on the next run).

Add `--quiet` to suppress the per-100 progress lines (handy under systemd/cron
— the final summary line always prints).

### Upgrading from an older version

If you're updating from a version of this tool that predates the state file,
the **first** `sync` after upgrading does a one-time migration: it matches
files already on disk to feed items by their id, renames them to the
local-time naming scheme above, and fixes any HEIC-bytes-named-.jpg files —
all before downloading anything new. You'll see a line like:

```
Migrated 940 existing file(s) to the local-time naming scheme.
```

Files on disk that Kaymbu no longer reports in the feed are left completely
untouched.

## State file

`sync` keeps a hidden `.goddard-state.json` inside your output directory —
this is the source of truth for what's been downloaded, at what rendition,
and whether it's a draft. It's written atomically and safe to inspect, but
don't hand-edit it while a sync is running. It's also designed to be a stable
extension point: other tools can add their own keys to an item's entry (e.g. a
future Google Photos uploader tracking an `"uploaded"` flag), and `sync` never
drops keys it doesn't recognize when it rewrites an entry.

Progress is checkpointed to this file every ~100 completed
downloads/upgrades, so an interrupted first-time backfill picks up close to
where it left off instead of re-scanning everything.

## Scheduling (every weekday at 7pm)

A user-level **systemd timer** is included. On a machine whose timezone is set to
US Eastern, `19:00` local is 7pm ET and follows daylight saving automatically.

```bash
./install.sh
```

This installs and enables `goddard-photo-sync.timer` (Mon–Fri 19:00). To run even
while you're logged out, enable lingering once:

```bash
sudo loginctl enable-linger "$USER"
```

Useful commands:

```bash
systemctl --user list-timers goddard-photo-sync.timer   # when it next runs
systemctl --user start goddard-photo-sync.service        # run a sync now
journalctl --user -u goddard-photo-sync.service -f       # watch the logs
```

### cron alternative

```cron
0 19 * * 1-5  /usr/bin/python3 /path/to/goddard_sync.py sync >> ~/.local/state/goddard-sync.log 2>&1
```

## Configuration

Config lives at `~/.config/goddard-photo-sync/config.json` (see
`config.example.json`). Every field can also be set via environment variable,
which takes precedence — handy for containers or CI.

| Field           | Env var                 | Meaning                                              |
|-----------------|-------------------------|------------------------------------------------------|
| `username`      | `GODDARD_USERNAME`      | Phone or email used to log in                         |
| `token`         | `GODDARD_TOKEN`         | Bearer token (written by `login`)                     |
| `output_dir`    | `GODDARD_OUTPUT_DIR`    | Where photos are saved (default `~/Pictures/Goddard`) |
| `ntfy_topic`    | `GODDARD_NTFY_TOPIC`    | If set, a push notification is sent after new photos  |
| `ntfy_server`   | `GODDARD_NTFY_SERVER`   | ntfy server (default `https://ntfy.sh`)               |
| `client_id`     | `GODDARD_CLIENT_ID`     | App OAuth client id (sensible default built in)       |
| `client_secret` | `GODDARD_CLIENT_SECRET` | App OAuth client secret (sensible default built in)   |

### Push notifications

Set `ntfy_topic` (and optionally `ntfy_server`) to get a
[ntfy](https://ntfy.sh) push:

```json
{ "ntfy_topic": "my-goddard-photos" }
```

- **Success** — sent only when a run actually downloaded something new or
  upgraded a rendition; a no-op sync stays silent.
- **Failure** — sent (with ntfy's `Priority: high`) whenever the token is
  rejected, an unexpected error interrupts the sync, or any item failed to
  download. Titled `Goddard sync FAILED` (auth/unexpected errors) or
  `Goddard sync: N failed` (download failures).

`sync`'s exit code reflects the same thing: `2` for an auth problem
(re-run `login`), `1` if anything failed to download, `0` otherwise — useful
for alerting from systemd/cron on its own even without ntfy configured.

## Security & privacy

- **No personal data in the repo.** Your token, username, and photos are stored
  outside the repo and are covered by `.gitignore`. The config file is `600`.
- **The built-in `client_id`/`client_secret`** are the OAuth credentials shipped
  inside the public Goddard app — identical for every user and trivially
  extractable from the APK. They are *app* credentials, not *your* credentials,
  and let the tool work out of the box. Override them via config if Goddard
  rotates them.
- The tool talks only to Kaymbu's official hosts over HTTPS and downloads only
  what your account can see.

## Limitations

- **Photos and videos only.** The feed also contains daily sheets and
  storyboards (newsletters); these are not downloaded by `sync`. Storyboard
  covers are recoverable via a separate detail endpoint — see
  [docs/SYNCING_VIDEOS_AND_STORYBOARDS.md](docs/SYNCING_VIDEOS_AND_STORYBOARDS.md)
  for the method if you want to add that too.
- **Older originals get archived.** Kaymbu lifecycles some full-resolution
  originals into AWS Glacier Deep Archive. Downloading one returns
  `403 InvalidObjectState`, so `sync` saves the next-best `_display` rendition
  (~1080px) instead — note that a `HEAD` request on an archived original can
  still succeed with the real file size even though the actual download
  (`GET`) 403s, since S3 serves cached metadata without needing a restore.
  `sync`'s upgrade pass revisits every non-full-res item on each run and
  swaps in the original automatically once/if it becomes fetchable again, with
  no need to re-run anything manually.
- **Draft graphics** (newsletter/invitation art) have no full-resolution
  original, so the medium `_display` rendition is saved for those, and they're
  never included in the upgrade pass.
- A lifecycled video has no lower-resolution fallback (unlike photos), so it's
  recorded in the state file as *unavailable* (not as a failure, so it won't
  page you every day) and quietly retried by the upgrade pass on each run.
- If your token ever stops working, `sync` will tell you to re-run `login`.

## Tests

```bash
python3 -m unittest discover tests
```

Covers filename generation (local-time conversion), byte-sniffing for
extensions, state file load/save (including preserving unknown keys other
tools may add), and the one-time migration of pre-state-file libraries. No
network access required.

## License

MIT — see [LICENSE](LICENSE).
