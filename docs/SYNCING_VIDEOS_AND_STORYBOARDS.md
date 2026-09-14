# Syncing videos and storyboards

**Videos are now implemented in `sync` itself** (`_download_video` in
`goddard_sync.py`) — the section below is kept as design-notes/history for how
that endpoint behaves, but you don't need to do anything extra to get videos;
just run `sync`.

**Full storyboards are now downloaded by `goddard_sync.py documents`.**
The app bundle supplies the missing step: request
`https://my.kaymbu.com/storyboards/shared?id=<encrypted.id>&v=<encrypted.v>`.
The server decrypts that pair and returns the full rendered HTML, including
attachment links. No client-side decryption, browser, or emulator is needed.
`goddard_documents.py` saves the content and linked assets for offline use.
The endpoint notes below describe the implemented export paths.

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

## Storyboards — full offline documents

`GET /feed/details/storyboard/<storyboardId>` returns an `encrypted` object
with `id` and `v`. These values are access parameters for the shared document:

```
https://my.kaymbu.com/storyboards/shared?id=<encrypted.id>&v=<encrypted.v>&excludeBanner=1&source=parentapp
```

The server returns the full rendered HTML. The `documents` command saves it,
localizes image URLs, and downloads linked PDF/Office attachments. The signed
URL is not printed or stored in the document index.

## Daily sheets and lesson plans

The daily-sheet detail ID is **not** the feed post `_id`. Join the following
fields from `row.dailysheet` with `|`, then URL-encode the result:

```
classroom|student|fromDateISOString|timezoneOffset|language
```

Request `/feed/details/dailysheet/<encoded-id>` and fetch the returned
`dailysheetUrl`. This is server-rendered HTML, including the full Lessons
section when available. The exporter also saves that section as a separate
page under `Documents/Lesson Plans/`.

Standalone `lesson-planner` posts use `lessonPlanMessageId`; their detail
response contains `lessonPlanUrl`, resolved against `https://my.kaymbu.com`.
The account used to verify this implementation had no standalone lesson-plan
posts; this URL mapping comes from the app bundle. Daily sheets and newsletter
attachments were verified with live downloads.

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
