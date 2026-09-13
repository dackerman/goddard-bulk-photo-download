# goddard-bulk-photo-download

Download every photo of your child from the **Goddard Family Hub** app, at full
resolution, and keep a local folder in sync automatically.

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
API. This tool reproduces the two calls that matter:

1. **Login (passwordless).** You submit your phone or email; Kaymbu sends a
   4-digit code; you exchange the code for a bearer token. The token is
   long-lived, so you do this once.
2. **Feed.** `POST /feed` returns your timeline of "moments" (photo posts),
   paged with a cursor. Each image comes back as a `..._thumb.jpg` URL on a
   public CDN; dropping the `_thumb` suffix yields the full-resolution original.

`login` stores the token locally; `sync` reads the feed and downloads any photos
you don't already have. `sync` is what you schedule.

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

The first run downloads your entire history (this can be several GB). Files are
named `YYYY-MM-DD_HHMMSS_<id>.jpg` so they sort chronologically, and a
`manifest.csv` with dates and captions is written alongside them.

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

### Push notifications (optional)

Set `ntfy_topic` (and optionally `ntfy_server`) to get a
[ntfy](https://ntfy.sh) push whenever new photos are pulled:

```json
{ "ntfy_topic": "my-goddard-photos" }
```

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

- **Photos only.** The feed also contains daily sheets, storyboards
  (newsletters), and the occasional video; these are not downloaded by `sync`.
  Videos *are* fully recoverable via a separate detail endpoint, and storyboard
  covers are too — see
  [docs/SYNCING_VIDEOS_AND_STORYBOARDS.md](docs/SYNCING_VIDEOS_AND_STORYBOARDS.md)
  for the exact method and code sketch if you want to add them.
- **Older originals get archived.** Kaymbu lifecycles some full-resolution
  originals into AWS Glacier Deep Archive. Those return `403 InvalidObjectState`
  on download, so for them the tool saves the next-best `_display` rendition
  (~1080px). This mostly affects historical photos on a first backfill; because
  the daily run grabs each day's photos while they're still "hot", ongoing syncs
  generally capture full resolution.
- **Draft graphics** (newsletter/invitation art) have no full-resolution
  original, so the medium `_display` rendition is saved for those.
- If your token ever stops working, `sync` will tell you to re-run `login`.

## License

MIT — see [LICENSE](LICENSE).
