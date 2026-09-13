#!/usr/bin/env python3
"""
import_from_export — recover full-resolution originals from a Google Photos
(or any) export and swap them into a goddard_sync library.

Why: Kaymbu archives older originals (see README "Limitations"), so a first
backfill may only get ~1024px `_display` copies. But if you ever saved photos
one at a time from the Goddard app, those saves *were* the originals — and a
Google Photos album "Download all" zip of them can be used to upgrade the
library.

How it matches (needs Pillow + pillow-heif, unlike the stdlib-only core):
  1. EXIF DateTimeOriginal equal to a library photo's, disambiguated by a
     64-bit perceptual dHash (burst shots share a second);
  2. else nearest dHash within a strict distance (for files with no EXIF).
A match only counts as an upgrade if the export file has >= 20% more pixels
than what's on disk and the library entry isn't already "original".

For each upgrade it writes the export bytes over the library file (same
canonical name; extension re-sniffed), sets mtime to the feed date, and sets
the state entry to rendition "original" with `"source": "export"`. Because
the rendition changed, `goddard_sync.py upload` (or the next `sync`) will
re-upload it to Google Photos — the old low-res copy stays there, as with
any upgrade.

Usage:
  tools/import_from_export.py <export.zip|dir> <library dir> [--apply]
Without --apply it only reports. Prints one line per unmatched export file so
you can see what the tool never saw (non-Goddard photos in the album, etc).
"""
import io, json, os, sys, zipfile, collections
from PIL import Image, ImageOps, ExifTags
import pillow_heif; pillow_heif.register_heif_opener()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import goddard_sync as gs

STATE = gs.STATE_FILENAME


def dhash(img, size=8):
    g = ImageOps.exif_transpose(img).convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(g.get_flattened_data()) if hasattr(g, "get_flattened_data") else list(g.getdata())
    w = size + 1; bits = 0
    for r in range(size):
        for c in range(size):
            bits = (bits << 1) | (1 if px[r * w + c] < px[r * w + c + 1] else 0)
    return bits


def probe(fp):
    img = Image.open(fp); img.load()
    ex = img.getexif(); dto = None
    try:
        dto = ex.get_ifd(ExifTags.IFD.Exif).get(ExifTags.Base.DateTimeOriginal)
    except Exception:
        pass
    dto = dto or ex.get(ExifTags.Base.DateTime)
    return {"w": img.size[0], "h": img.size[1], "dto": dto, "dhash": dhash(img)}


def iter_export(src):
    if src.lower().endswith(".zip"):
        with zipfile.ZipFile(src) as zf:
            for zi in zf.infolist():
                if not zi.is_dir() and zi.filename.lower().endswith((".jpg", ".jpeg", ".heic", ".png")):
                    yield zi.filename, zf.read(zi)
    else:
        for n in sorted(os.listdir(src)):
            if n.lower().endswith((".jpg", ".jpeg", ".heic", ".png")):
                with open(os.path.join(src, n), "rb") as f:
                    yield n, f.read()


def ham(a, b):
    return bin(a ^ b).count("1")


def main(argv):
    if len(argv) < 3:
        print(__doc__); return 2
    src, lib = argv[1], os.path.expanduser(argv[2]); apply = "--apply" in argv
    state, _ = gs._load_state(lib)
    local = []
    for mid, e in state["items"].items():
        if e.get("type") != "image" or not e.get("file"):
            continue
        p = os.path.join(lib, e["file"])
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "rb") as f:
                info = probe(f)
        except Exception as ex:
            print(f"skip unreadable {e['file']}: {ex}", file=sys.stderr); continue
        info.update(mid=mid, entry=e); local.append(info)
    by_dto = collections.defaultdict(list)
    for r in local:
        if r["dto"]:
            by_dto[r["dto"]].append(r)
    print(f"library: {len(local)} readable photos; export: scanning {src}")

    best_for = {}  # mid -> (pixels, name, data)
    unmatched, matched = [], 0
    for name, data in iter_export(src):
        try:
            z = probe(io.BytesIO(data))
        except Exception as ex:
            unmatched.append((name, f"unreadable: {ex}")); continue
        hit = None
        cands = by_dto.get(z["dto"], []) if z["dto"] else []
        if cands:
            c = min(cands, key=lambda r: ham(r["dhash"], z["dhash"]))
            if ham(c["dhash"], z["dhash"]) <= 16:
                hit = c
        if hit is None:
            c = min(local, key=lambda r: ham(r["dhash"], z["dhash"]))
            if ham(c["dhash"], z["dhash"]) <= 5:
                hit = c
        if hit is None:
            unmatched.append((name, f"no match (dto={z['dto']}, {z['w']}x{z['h']})")); continue
        matched += 1
        zp, lp = z["w"] * z["h"], hit["w"] * hit["h"]
        if hit["entry"].get("rendition") != "original" and zp >= lp * 1.2:
            if hit["mid"] not in best_for or zp > best_for[hit["mid"]][0]:
                best_for[hit["mid"]] = (zp, name, data, z, hit)

    print(f"matched {matched} export file(s) to library photos; {len(unmatched)} unmatched; "
          f"{len(best_for)} upgrade candidate(s)")
    for name, why in unmatched:
        print(f"  unmatched: {name.split('/')[-1]}  {why}")
    if not apply:
        for mid, (_, name, data, z, hit) in sorted(best_for.items(), key=lambda kv: kv[1][4]["entry"]["date"]):
            e = hit["entry"]
            print(f"  would upgrade {e['file']}  {hit['w']}x{hit['h']} -> {z['w']}x{z['h']}  from {name.split('/')[-1]}")
        print("\n(dry run — re-run with --apply to write)")
        return 0

    done = 0
    for mid, (_, name, data, z, hit) in best_for.items():
        e = state["items"][mid]
        ext = gs._sniff_ext(data)
        new_name = gs._filename(e["date"], mid, ext)
        new_path, old_path = os.path.join(lib, new_name), os.path.join(lib, e["file"])
        gs._atomic_write(new_path, data)
        gs._set_mtime(new_path, e["date"])
        if new_path != old_path and os.path.isfile(old_path):
            os.remove(old_path)
        e.update(file=new_name, rendition="original", bytes=len(data), source="export")
        done += 1
    gs._save_state_atomic(lib, state)
    gs._write_manifest(lib, state)
    print(f"upgraded {done} photo(s) in {lib}; run `goddard_sync.py upload` to push them.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
