# Syncing videos and storyboards

**Videos are now implemented in `sync` itself** (`_download_video` in
`goddard_sync.py`) — the section below is kept as design-notes/history for how
that endpoint behaves, but you don't need to do anything extra to get videos;
just run `sync`.

**Storyboards are still not synced.** This note records exactly how you'd go
about it, so nobody has to spin up an emulator again to rediscover it.
Everything below was confirmed by calling the live API directly with a normal
account bearer token (no app or emulator needed).

Both features hang off one endpoint the app already uses:

```
GET /kaymbu-parentapp/api/feed/details/<type>/<id>
Authorization: Bearer <token>
x-parentapp-version: 3.4.1.28
```

`<type>` is the feed result's `type`; `<id>` depends on the type:

| type         | id to use                          |
|--------------|------------------------------------|
| `moment`     | the individual `moments[].._id`    |
| `storyboard` | the result's `storyboardId`        |
| `dailynote`  | `dailynoteId`                      |
| `portfolios` | `portfolioMessageId`               |

(Derived from the app's `getFeedDetail(type, id)` and its type switch.)

---

## Videos  — fully downloadable

In the feed, a video shows up as a moment whose image entry has `type: "video"`
(instead of `"image"`). The feed only gives you a `_thumb.jpg` still, not the
video. To get the actual file, call the detail endpoint for that moment id:

```
GET /kaymbu-parentapp/api/feed/details/moment/<video_moment_id>
```

Response (real example, trimmed):

```json
{
  "_id": "6a3c01cbd60b0f011341df65",
  "video": true,
  "videoSource":       "/moments/<classroom>/videos/<uuid>_encoded.mp4",
  "videoLowRendition": "/moments/<classroom>/videos/<uuid>_encoded.mp4",
  "videoStill":        "/moments/<classroom>/videos/<uuid>_still.jpg",
  "pictureSource":     "/moments/<classroom>/videos/<uuid>_still_overlay.jpg"
}
```

The full URL is the CDN host + `videoSource`:

```
https://d2k9f6tk478nyp.cloudfront.net<videoSource>
```

That MP4 downloads with no auth, exactly like the photo originals. It even works
for videos the classroom later deleted (`isClassroomDeleted: true`) — the file
stays on the CDN.

### Where this lives in the code

`_media_items` (in `goddard_sync.py`) collects video moments alongside images,
and `_download_video` does exactly what's described above: calls the detail
endpoint, reads `videoSource` (falling back to `videoLowRendition`), and
downloads `CDN + videoSource` to `<canonical-name>.mp4` with rendition
`"original"`. It runs in the same download pass and thread pool as photos.

### Caveats
- Only one rendition is exposed (`videoSource` == `videoLowRendition`); there is
  no separate high-res original to fetch.
- Like photos, an older video's file *could* have been lifecycled into Glacier
  Deep Archive and return `403 InvalidObjectState`. Handle it the same way the
  photo downloader does (catch the 403; there is no lower-res video fallback, so
  just record it as unavailable).

---

## Storyboards  — only the cover thumbnail is in the clear

Storyboards are the multi-page "newsletter" posts. The detail call works, but the
body is mostly encrypted:

```
GET /kaymbu-parentapp/api/feed/details/storyboard/<storyboardId>
```

```json
{
  "_id": "69ef6f3748187b0114e6e01a",
  "thumbnailUrl": "https://dw74ugoaeqr7w.cloudfront.net/media/.../..._display.jpg?fit=crop&w=230&h=230...",
  "encrypted": { "v": "0428...", "id": "2577...long hex..." }
}
```

- `thumbnailUrl` is a small cropped cover image — easy to grab, but only the
  cover.
- The storyboard's real content (its pages and the full-size photos on them) is
  inside the `encrypted` blob. The app doesn't parse it; it hands the storyboard
  to a **webview** at `https://my.kaymbu.com/storyboards/<...>`, which decrypts
  and renders it client-side.

So there is no plain JSON list of a storyboard's photos to loop over. Options, in
order of effort:

1. **Do nothing (recommended).** The photos featured in a storyboard are almost
   always also posted individually as normal `moment`s, so the regular photo
   `sync` already has them at full resolution. Storyboards mostly add layout and
   text, not unique images.
2. **Grab the cover only.** If you just want a copy of each newsletter's cover,
   download `thumbnailUrl` (strip the `?fit=crop...` query for the larger
   `_display.jpg`).
3. **Render the webview.** Drive `https://my.kaymbu.com/storyboards/<id>` in a
   headless browser (Playwright/Puppeteer) with the session, let it decrypt, and
   scrape the rendered `<img>`/page URLs or print each page to PDF. This is the
   only way to get storyboard-exclusive imagery, and it's the one part that needs
   a browser (not an emulator).
4. **Reverse the `encrypted` payload.** The `{v, id}` pair is decrypted by JS
   served to the webview. You could pull that JS from `my.kaymbu.com` and
   reproduce the decryption to get a clean content JSON. Most involved; only
   worth it for a fully headless storyboard export.

---

## How this was confirmed (for future reference)

No emulator required. With a valid token in `~/.config/goddard-photo-sync/config.json`:

```python
import json, os, urllib.request
cfg = json.load(open(os.path.expanduser("~/.config/goddard-photo-sync/config.json")))
hdr = {"Authorization": "Bearer " + cfg["token"], "x-parentapp-version": "3.4.1.28"}
url = "https://hq.kaymbu.com/kaymbu-parentapp/api/feed/details/moment/<a_video_moment_id>"
print(urllib.request.urlopen(urllib.request.Request(url, headers=hdr)).read().decode())
```

Find a video moment id by scanning the feed for `moments[].type == "video"`, and
a storyboard id from any `type == "storyboard"` result's `storyboardId`.
