# Goddard School photo and document sync

![A dad at a laptop downloading photos of his toddler from a friendly cloud](docs/hero.png)

Are you a dad (or mom) and love your kid so much that you want to have EVERY photo your Goddard school teachers take
of your little one? Are you frustrated that all you can do is download individual images to your phone and aren't sure
whether you forgot to get some months ago? If so, you've come to the right place.

This script downloads every photo *and video* of your child from the **Goddard Family Hub**
app at the best available resolution and keeps a local folder in sync automatically.
It also saves daily sheets, lesson content, newsletters, and their attachments.
Optional uploads send photos and videos to **Google Photos** and document PDFs to
**Google Drive**, with separate destinations for each child.

The weekday sync can download new school posts and upload them to both services.
Documents are also available as an offline archive with their images saved locally.

It only accesses **your own account's** school data, using your login. Google
Drive access is limited to the folders you select and files the app creates.

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
4. **Documents.** Daily-sheet and newsletter detail endpoints return shared
   document URLs. `documents` saves the full HTML, images, lesson sections,
   and linked attachments for offline reading.
5. **Drive uploads.** `drive-upload` converts the archived HTML to PDFs and
   uploads to your selected folders. Checksums skip unchanged files; stable
   Drive IDs let changed documents update in place and interrupted runs resume.

`login` stores the token locally; `sync` reads the feed and downloads any
photos/videos you don't already have, at the best rendition currently
available. `sync` is what you schedule.

## Requirements

- Python 3.9+ (standard library only — no `pip install` needed)
- A Goddard Family Hub account
- Optional for Google Drive PDFs: Node.js, Playwright, and Chromium
  (see [Drive setup](#sync-documents-to-google-drive))
- Optional: Pillow + pillow-heif, only for `tools/import_from_export.py`
  (see [Limitations](#limitations))

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

### Command reference

Every command accepts `--config PATH` (default
`~/.config/goddard-photo-sync/config.json`), before or after the subcommand.

| Command | What it does | Useful flags |
|---|---|---|
| `login` | One-time Kaymbu login; stores the token | `--user`, `--code`, `--output-dir` |
| `sync` | Download photos/videos and upload to Google Photos; also sync documents to Drive when enabled | `--workers N`, `--quiet`, `--no-upload`, `--no-drive`, `--output-dir` |
| `documents` | Save offline daily sheets, lesson sections, newsletters, and attachments | `--refresh`, `--output-dir` |
| `drive-login` | Authorize selected Drive folders using Google Picker | `--no-browser`, `--url-file PATH` |
| `drive-upload` | Render document PDFs and upload documents/attachments to Drive | `--prepare-only`, `--dry-run` |
| `status` | Config, token, per-folder counts, Google Photos mode/login/pending | |
| `students` | Table of every child in the feed and how their name/folder/album resolve | |
| `gphotos-login` | One-time Google OAuth; stores the refresh token | `--client-id`, `--client-secret`, `--no-browser` |
| `albums` | List the Google Photos albums this tool created | |
| `upload` | Push pending items to Google Photos (also runs inside `sync`) | `--mode library\|album`, `--album TITLE`, `--album-id ID`, `--student NAME_OR_ID`, `--dry-run`, `--limit N`, `--workers N` |

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
  (`_display`/`_thumb`) whose full-resolution original could be fetched this
  time and was re-downloaded in place of the old file. This runs on *every*
  sync. In practice it mostly catches a photo that failed transiently on its
  first day; an original that Kaymbu has already archived does not come back
  on its own (see [Limitations](#limitations)).
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
extension point: other tools can add their own keys to an item's entry, and
`sync` never drops keys it doesn't recognize when it rewrites an entry. The
Google Photos uploader (below) is one such extension: it adds a `"gphotos"`
key, `{"id", "rendition", "album_id", "at"}`, once an item has been uploaded,
and `tools/import_from_export.py` sets `"source": "export"` on items it
swapped in.

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
`config.example.json`). Fields with an environment variable listed below can
also be overridden through the environment. Drive settings are configured in JSON.

| Field           | Env var                 | Meaning                                              |
|-----------------|-------------------------|------------------------------------------------------|
| `username`      | `GODDARD_USERNAME`      | Phone or email used to log in                         |
| `token`         | `GODDARD_TOKEN`         | Bearer token (written by `login`)                     |
| `output_dir`    | `GODDARD_OUTPUT_DIR`    | Where photos are saved (default `~/Pictures/Goddard`); may contain `{name}` — see [Multiple children](#multiple-children) |
| `ntfy_topic`    | `GODDARD_NTFY_TOPIC`    | If set, a push notification is sent after new photos  |
| `ntfy_server`   | `GODDARD_NTFY_SERVER`   | ntfy server (default `https://ntfy.sh`)               |
| `client_id`     | `GODDARD_CLIENT_ID`     | App OAuth client id (sensible default built in)       |
| `client_secret` | `GODDARD_CLIENT_SECRET` | App OAuth client secret (sensible default built in)   |
| `gphotos_client_id`     | `GODDARD_GPHOTOS_CLIENT_ID`     | Your Google Cloud OAuth client id (written by `gphotos-login`) |
| `gphotos_client_secret` | `GODDARD_GPHOTOS_CLIENT_SECRET` | Your Google Cloud OAuth client secret (written by `gphotos-login`) |
| `gphotos_refresh_token` | `GODDARD_GPHOTOS_REFRESH_TOKEN` | Long-lived token (written by `gphotos-login`)          |
| `gphotos_mode`          | `GODDARD_GPHOTOS_MODE`          | `off` (default) / `library` / `album` — see below      |
| `gphotos_album`         | `GODDARD_GPHOTOS_ALBUM`         | Album title for mode `album` (default `Goddard`); may contain `{name}` — see [Multiple children](#multiple-children) |
| `gphotos_album_id`      | `GODDARD_GPHOTOS_ALBUM_ID`      | Cached id of the app-created album (resolved automatically); ignored in per-student mode |
| `per_student`           | `GODDARD_PER_STUDENT`           | `false` (default) / `true` — route each child to its own folder/album, see [Multiple children](#multiple-children) |
| `students`              | —                                | Optional per-child overrides (`name`, `output_dir`, `gphotos_album`, `gphotos_album_id`, `gdrive_folder_id`), keyed by student id |

| Drive field | Meaning |
|---|---|
| `gdrive_folder_id` | Existing destination folder ID; set per child under `students`, or at the top level in single-folder mode |
| `gdrive_sync_enabled` | Set to `true` after setup to include documents and Drive uploads in `sync`; off by default |
| `gdrive_client_id`, `gdrive_client_secret` | Optional Desktop OAuth client; defaults to the Google Photos client during sign-in |
| `gdrive_refresh_token` | Written by `drive-login`; keep private |
| `gdrive_node` | Node executable; default `node` |
| `gdrive_node_modules` | Optional module directory passed as `NODE_PATH` to find Playwright |
| `gdrive_chromium` | Optional browser executable; otherwise uses Playwright's installed Chromium |

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
- **Google Photos login expired** — high priority, when the refresh token
  is rejected; re-run `gphotos-login`.
- **New student seen, waiting for a name** — high priority, in per-student
  mode when a child appears in the feed before any daily sheet names them
  (see [Multiple children](#multiple-children)).

`sync`'s exit code reflects the same thing: `2` for an auth problem
(re-run `login`), `1` if anything failed to download, `0` otherwise — useful
for alerting from systemd/cron on its own even without ntfy configured.

## Multiple children

Every feed result is tagged with `studentIds` — normally the one child it's
of, but a photo of two siblings can carry both ids, and some posts (e.g. a
classroom-wide announcement) carry none. Set `"per_student": true` to route
each child to its own folder and its own Google Photos album instead of
mixing everyone into one library:

```json
{ "per_student": true,
  "output_dir": "~/Pictures/Goddard-{name}",
  "gphotos_album": "Goddard School - {name}" }
```

- **`output_dir`/`gphotos_album` templates.** Either may contain a `{name}`
  placeholder, filled in per child (`~/Pictures/Goddard-{name}` ->
  `~/Pictures/Goddard-Ada`, `~/Pictures/Goddard-Ben`). If you turn on
  `per_student` but forget the placeholder, `-{name}` (folder) or `
  - {name}` (album) is appended automatically so two children's libraries
  can never collide.
- **A new child needs zero setup.** The first time a sibling's student id
  shows up in the feed, it gets its own folder/album automatically, named
  from a daily sheet's possessive label (e.g. "Ada's" -> `Ada`). Until a
  daily sheet has named the child (or you add a `students` entry), that
  child is *deferred*: nothing is downloaded or uploaded for them yet and a
  high-priority ntfy tells you the new id, so a placeholder folder/album
  never gets created. Daily sheets normally arrive the same afternoon, so
  in practice the first evening run already has the name.
- **`students` overrides** let you rename a child (moment posts never carry
  a name — only a daily sheet's `studentLabel` does — so the fallback name
  isn't always pretty) or point one at a custom folder/album:
  ```json
  { "students": { "<studentId>": { "name": "Ben",
                                    "output_dir": "~/Pictures/Ben-Goddard",
                                    "gphotos_album": "Ben at Goddard" } } }
  ```
  Every key is optional. Find a child's id with `./goddard_sync.py students`.
- **A post tagged with both kids** (e.g. a photo of two siblings together)
  is downloaded into *both* children's folders — dedup is per folder, so
  that's not a duplicate within either library. A post tagged with no
  student at all goes to every child.
- **One notification per run**, not one per child: a single ntfy titled
  e.g. `Goddard: Ada 3 new, Ben 12 new` (a child with nothing new is
  omitted from the title), and `sync`'s exit code is the worst across
  children.
- **`upload --student NAME_OR_ID`** restricts a manual upload to one child
  (by resolved name or by id); `--album`/`--album-id` are only accepted
  together with `--student` (otherwise it's ambiguous which child's album
  they mean).
- **`status`** shows a section per child (folder, photo/video counts,
  gphotos album, pending uploads) in per-student mode, in addition to the
  usual shared lines.
- **`./goddard_sync.py students`** prints a table of every child seen in the
  feed — id, resolved name, where the name came from (config / daily sheet /
  fallback), post count, resolved folder and album, and whether the folder
  exists yet — handy for checking a new child resolved the way you expect
  before (or after) turning `per_student` on.

## Google Photos upload (optional)

`sync` can also push everything it downloads up to Google Photos, so your
partner (or anyone else) can browse the same library there without needing
this tool at all. It's entirely optional and off by default (`gphotos_mode:
"off"`).

Three modes:

- **`off`** (default) — no upload happens.
- **`library`** — every downloaded photo/video is added straight to your main
  Google Photos library, no album.
- **`album`** — everything goes into one album (title from `gphotos_album`,
  default `"Goddard"`), which this tool creates the first time it's needed.

> **Google Photos API restriction:** this tool can only see and add to
> albums it created itself. It cannot see, and has no way to write to, an
> album you made by hand in the Google Photos app, or one created by some
> other app. If you want the photos in a hand-made album, move them there
> yourself in the Photos app after they land in the app-created one — there's
> no API for automating that last step.

### Re-uploading on upgrade

Google Photos offers no "replace an existing item" API. When `sync`'s
upgrade pass later swaps a `_display`-rendition photo for its full-resolution
original (see [Limitations](#limitations)), the uploader notices the
rendition changed and uploads it *again* as a brand-new item — it can't
update the one already up there. The older, lower-resolution copy is left in
your Google Photos library; if you don't want the duplicate, delete it by
hand (it's easy to spot — same photo, smaller/blurrier).

### Setup (Google Cloud side)

You need your own Google OAuth client — a one-time browser setup, about ten
minutes. Google reorganized this UI in 2025 under "Google Auth Platform", so
older guides will show different page names.

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a
   new project (e.g. `goddard-photo-sync`). A dedicated project is easier
   than reusing one: its consent screen has to be *published* (step 4), and
   that is a per-project switch.
2. **APIs & Services → Library** — enable the **Google Photos Library API**.
3. **Google Auth Platform → Overview → Get started** — app name, your email
   as support address, audience **External**, contact email, accept the User
   Data Policy. Then on the **Branding** page also fill in an *Application
   home page*, an *Application privacy policy link* (any site you own is
   fine; it is only ever shown to you) and add that site under *Authorized
   domains*. Save. Without those three fields the Publish button in the next
   step stays disabled with a misleading "complete your Branding" message.
4. **Audience → Publish app → Confirm.** This is important: while the app is
   in **Testing** status Google expires refresh tokens after 7 days, which
   silently breaks a daily job. In **Production** they last indefinitely. No
   verification is needed for personal use; you just click through a
   "Google hasn't verified this app" warning once during login.
5. **Clients → Create client** — application type **Desktop app**, any name.
   Copy the **Client ID** and **Client secret** from the dialog, or use its
   *Download JSON* button. The secret is shown **only once**; if you lose
   it, open the client and use *Add secret* to generate a new one (then
   disable and delete the old one).
6. Run the login flow:

   ```bash
   ./goddard_sync.py gphotos-login --client-id "<id>" --client-secret "<secret>"
   ```

   This opens your browser (or prints a URL with `--no-browser`, e.g. over
   SSH — the redirect still has to land on the machine running the command).
   Pick your account, click *Advanced → Go to Goddard Photo Sync* on the
   unverified-app warning, tick both permissions, Continue. The tool stores
   the resulting refresh token in your config file. The client id/secret can
   also be supplied via `GODDARD_GPHOTOS_CLIENT_ID` / `_SECRET` env vars.
7. Set a mode and upload:

   ```bash
   # edit ~/.config/goddard-photo-sync/config.json:
   #   "gphotos_mode": "library"   (or "album", with "gphotos_album": "Ada")

   ./goddard_sync.py upload --dry-run   # see what would be uploaded first
   ./goddard_sync.py upload
   ```

   From then on, every `sync` run also uploads anything new (unless you pass
   `sync --no-upload`).

### Other commands

```bash
./goddard_sync.py albums          # list albums this tool has created, with their ids
./goddard_sync.py upload --dry-run --mode album --album "2026 photos"
./goddard_sync.py status          # also shows gphotos mode/login/pending count
```

If your refresh token is ever revoked or expires, `sync` and `upload` report
it clearly (exit code `2`, and — for `sync` — a high-priority ntfy titled
"Goddard: Google Photos login expired") rather than failing silently; re-run
`gphotos-login` to fix it.

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
- **Google Photos credentials are yours, not baked in.** `gphotos_client_id`/
  `gphotos_client_secret`/`gphotos_refresh_token` live in the same `600`
  config file and are never logged or sent anywhere but Google's own OAuth
  and Photos Library API hosts.

## Daily sheets, lessons, and newsletters

```bash
python3 goddard_sync.py documents
# Re-fetch older documents after the school edits them:
python3 goddard_sync.py documents --refresh
```

Saves an offline `Documents/index.html` inside each configured child’s output
folder, with dated daily sheets, separate pages containing their Lessons
sections, full newsletters, and linked PDF/Office attachments. Images are
saved locally, so the documents can be read without logging in or connecting
to Kaymbu. Open the index in a browser; use the browser’s Print command if you
want a PDF copy. External links other than downloaded attachments still need
an internet connection.

The command supports the same config and per-student output routing as photos,
plus `--output-dir`. It skips complete older downloads, retries incomplete
ones, and refreshes today’s daily sheet. Use `--refresh` for later edits to
older sheets. Standalone lesson-plan posts are included when present; some
schools instead put their lesson content inside daily sheets and newsletters.
Document downloads run separately by default; enabling Google Drive sync
also includes them in `sync` and its existing photo timer.
They are not uploaded to Google Photos.

## Sync documents to Google Drive

`drive-upload` renders the offline HTML documents to PDFs and uploads them,
along with original attachments, under the configured destination for each
child. It creates Daily Sheets, Lesson Plans, Newsletters, and Attachments
subfolders as needed. Repeated runs skip identical files and update changed
files in place; unrelated Drive files are not overwritten or deleted.

1. Enable **Google Drive API** and **Google Picker API** in your existing
   Google Cloud OAuth project. The existing Desktop client used for Google
   Photos can be reused; its Photos refresh token remains separate.
2. Add `gdrive_folder_id` to each entry in the `students` config map. For
   single-folder mode, set `gdrive_folder_id` at the top level instead.
3. Run `python3 goddard_sync.py drive-login` and select the configured folders
   in Google's Picker. This requests only `drive.file` access to selected
   folders and app-created files, not your entire Drive.
4. Run the commands below, then set `"gdrive_sync_enabled": true` in config
   to include document downloads and Drive uploads in the existing `sync`
   command and its weekday timer.

```bash
python3 goddard_sync.py documents
python3 goddard_sync.py drive-upload --prepare-only  # local PDFs only
python3 goddard_sync.py drive-upload --dry-run      # verify writable destinations
python3 goddard_sync.py drive-upload
```

For example, merge the following into your existing private config, using IDs
from `students` and from each Drive folder's URL (`/drive/folders/<ID>`).
Keep the existing login and Google Photos settings:

```json
{
  "per_student": true,
  "output_dir": "~/Pictures/Goddard-{name}",
  "gdrive_sync_enabled": true,
  "students": {
    "<first-student-id>": {"name": "Ada", "gdrive_folder_id": "<Ada-folder-id>"},
    "<second-student-id>": {"name": "Ben", "gdrive_folder_id": "<Ben-folder-id>"}
  }
}
```

Each selected folder gets this structure (empty categories are omitted):

```text
Ada's existing Drive folder/
├── Daily Sheets/   # complete dated PDFs
├── Lesson Plans/   # lesson sections extracted from daily sheets
├── Newsletters/    # complete newsletter PDFs
└── Attachments/    # original PDF/Office/ZIP attachments
```

Install the optional PDF dependencies separately from the Python tool:

```bash
npm install --prefix "$HOME/.local/share/goddard-pdf" playwright
"$HOME/.local/share/goddard-pdf/node_modules/.bin/playwright" install chromium
```

Set `gdrive_node_modules` in config to the absolute path of that installation's
`node_modules` directory, for example `/home/you/.local/share/goddard-pdf/node_modules`.
Use an absolute `gdrive_node` path if Node is not on your systemd service's PATH.

PDF conversion requires Node.js, Playwright, and Chromium. Set `gdrive_node`,
`gdrive_node_modules` (the module directory used as `NODE_PATH`), and
`gdrive_chromium` to your installed runtimes if they aren't found by default.
PDFs and their content fingerprints are cached in `Documents/Drive PDFs/`.
The renderer blocks network requests and uses only the archived local images.
Standalone attachment files retain their original format.

`sync --no-drive` skips the document/Drive pass for one run. Separate
`gdrive_client_id` and `gdrive_client_secret` settings are optional; they
otherwise fall back to the Google Photos OAuth client during sign-in.
`gdrive_refresh_token` is stored in the private config, and access tokens stay
in memory. Do not share that config or the hidden state files.

Uploads use pre-generated Drive IDs, checksum checks, and a per-folder lock
so interrupted uploads can resume without creating duplicates. Moving or
trashing a managed Drive file causes an explicit error instead of silently
writing somewhere unexpected. The exporter does not change sharing settings; uploads inherit the destination
folder's existing sharing. PDF attachments are also saved separately under
Attachments; links in the original documents may still point to Kaymbu or local
archive paths.

### Drive troubleshooting

- **Access denied:** enable Google Drive API in the same Cloud project as the
  OAuth client. Run `drive-login` again if the configured folders were not
  selected or their permissions changed.
- **Both folders must be selected:** select all configured destinations in the
  Picker before clicking Insert. Authorization is checked against their IDs.
- **Cannot find Playwright/Chromium:** check `gdrive_node_modules` and install
  Chromium using the command above, or set `gdrive_chromium` to an existing
  browser executable.
- **A managed file was moved or trashed:** restore it to its original synced
  folder before retrying. The uploader will not follow it to another location.
- **An older sheet was edited:** run `documents --refresh`, then `drive-upload`.
  Normal runs refresh today's sheet and skip complete older downloads.

The existing timer runs on weekdays at 7 p.m. in the machine's local timezone,
with up to two minutes of jitter. With `gdrive_sync_enabled` set, `sync` runs
photos first, then downloads documents and uploads their PDFs. A failed document
pass prevents stale document uploads and returns a nonzero exit code. Drive
failures are printed to the service journal; there is no separate Drive push
notification. See `journalctl --user -u goddard-photo-sync.service` for details.

## Limitations

- **Separate document command.** `sync` downloads photos and videos;
  run `documents` for daily sheets, lessons, and newsletters.
- **Originals get archived, so sync promptly.** Kaymbu moves every
  full-resolution original into AWS Glacier Deep Archive (the CDN reports
  `x-amz-storage-class: DEEP_ARCHIVE` on all of them). A download then
  returns `403 InvalidObjectState`, and the app itself is served only the
  `_display` copy (~1024px) for such photos — its detail endpoint points
  `originalFile` at `_display.jpg`. The originals you *can* still fetch are
  the ones CloudFront happens to have cached, which is why coverage is patchy
  by date rather than a clean cutoff. Only the bucket owner (Kaymbu) can
  restore an archived object; no client-side trick works, and a `HEAD` on
  the original still succeeds (cached metadata), so don't be fooled by it.
  Practical consequences: run `sync` every day so each day's photos are
  grabbed while fresh; when an original isn't fetchable `sync` saves the
  `_display` copy and its upgrade pass retries cheaply on every run, but
  expect those to stay as they are unless you recover them from an export
  (next bullet).
- **Recovering archived originals from an export.** If you ever saved photos
  one at a time from the Goddard app (e.g. into a Google Photos album), those
  saves were the full-resolution originals. `tools/import_from_export.py
  <album.zip|dir> <library dir> --apply` matches an export against the
  library by EXIF capture time and perceptual hash and swaps in any file
  that is meaningfully larger than what's on disk; the next `upload`/`sync`
  re-uploads those. It needs Pillow and pillow-heif (the core tool stays
  stdlib-only); run it without `--apply` first to see the plan.
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
tools may add), and the one-time migration of pre-state-file libraries.
`tests/test_gphotos.py` covers the Google Photos uploader — pending-item
selection, batching at 50 items, state marking (including partial batch
failures), album resolution (cached/found/created), 401-triggered token
refresh, and that `--dry-run` performs no network calls or writes — with
`goddard_gphotos.request` (the module's sole HTTP entry point) mocked
throughout. `tests/test_students.py` covers per-student routing — name
derivation and sanitization, `{name}` template formatting (including the
auto-appended suffix), grouping posts by student id, per-child album-id
caching, `upload --student`, the combined per-run exit code/notification, and
an end-to-end synthetic two-student sync. No network access required for any
test. `tests/test_documents.py` covers composite daily-sheet IDs, offline
assets and attachments, lesson extraction, and resuming incomplete downloads.
`tests/test_gdrive.py` covers scoped folder authorization, per-child destinations,
create/skip/update behavior, recovery after lost responses, and scheduled sync.

## About Contributions

> *About Contributions:* Please don't take this the wrong way, but I do not accept outside contributions for any of my projects. I simply don't have the mental bandwidth to review anything, and it's my name on the thing, so I'm responsible for any problems it causes; thus, the risk-reward is highly asymmetric from my perspective. I'd also have to worry about other "stakeholders," which seems unwise for tools I mostly make for myself for free. Feel free to submit issues, and even PRs if you want to illustrate a proposed fix, but know I won't merge them directly. Instead, I'll have Claude or Codex review submissions via `gh` and independently decide whether and how to address them. Bug reports in particular are welcome. Sorry if this offends, but I want to avoid wasted time and hurt feelings. I understand this isn't in sync with the prevailing open-source ethos that seeks community contributions, but it's the only way I can move at this velocity and keep my sanity.

## License

MIT — see [LICENSE](LICENSE).
