#!/usr/bin/env python3
"""
Phase 3 — Cinematic audiobook compositor.

Renders a YouTube-ready MP4 per chapter per language from the artifacts the
rest of the pipeline already produces:

  - c{n}.cinematic.json   scene track + sentence captions (build_timeline.py)
  - scene images          assets/images/cinematic/{book}/{scene}.jpg
  - audio                 assets/audio/{lang}/{book}/c{n}.opus (+ .ambient.opus)
  - render spec           style/{id}.json  (shared with the web cinematic view)

It is the *video* renderer of the same timeline the web cinematic view plays,
so the two match. Output is deterministic and faster than realtime; batchable
over chapters x languages.

Pipeline per chapter:
  1. Resolve each scene's image (-> default.jpg -> solid fallback).
  2. Render caption frames (Pillow) into a transparent caption track (qtrle).
  3. ffmpeg filter_complex:
       per scene  : scale-cover -> Ken Burns zoompan
       xfade chain: offset = scene.start, clip = seg+crossfade  (sync-locked)
       -> vignette -> overlay captions -> overlay watermark
       audio      : voice + ambient (amix)
       encode     : H.264 per render spec, +faststart, trimmed to audio length

No libass/drawtext needed (captions are pre-rendered with Pillow), so it runs
on a minimal ffmpeg as long as: libx264, aac, zoompan, xfade, vignette, qtrle.

Usage:
    python compose_video.py --book the-book-which-tells-the-truth --lang en --chapters 1
    python compose_video.py --book the-book-which-tells-the-truth --lang en          # all chapters
    python compose_video.py --book ... --all-langs                                   # every lang
    python compose_video.py --book ... --chapters 1 --preview 25                     # 25s smoke test
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[2]
ASSETS = REPO / "assets.wheelofheaven.world"
AUDIO = ASSETS / "audio"
IMAGES = ASSETS / "images" / "cinematic"
STYLE_DIR = Path(__file__).resolve().parent / "style"
OUT_DIR = Path(__file__).resolve().parent / "out"

# Caption font: the web cinematic caption uses --font-family-lead (Space
# Grotesk). Match it; fall back to a common sans if absent.
FONT_CANDIDATES = [
    str(Path.home() / "Library/Fonts/SpaceGrotesk-VariableFont_wght.ttf"),
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def find_font() -> str:
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    raise SystemExit("No caption font found; install Space Grotesk or edit FONT_CANDIDATES.")


def load_font(path: str, size: int, weight: int | None = None) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(path, size)
    if weight is not None:
        try:
            font.set_variation_by_axes([weight])  # variable fonts (wght)
        except Exception:
            pass
    return font


# --- Caption rendering -------------------------------------------------------

def wrap_lines(draw, text, font, max_w):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_caption_png(cap, spec, font, speaker_font, out_path, W, H):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = spec["caption"]
    max_w = int(W * c["max_width_pct"] / 100)
    line_h = int(c["font_size"] * c["line_height"])
    lines = wrap_lines(d, cap["text"], font, max_w)

    show_speaker = bool(cap.get("speaker")) and cap.get("kind") not in (None, "body")
    speaker_h = int(speaker_font.size * 1.8) if show_speaker else 0

    block_h = line_h * len(lines) + speaker_h
    # Bottom of the text block sits at the safe-area line.
    bottom = H - int(H * c["safe_area_bottom_pct"] / 100)
    y = bottom - block_h

    color = tuple(int(c["color"].lstrip("#")[i:i+2], 16) for i in (0, 2, 4)) + (255,)
    stroke = tuple(int(c["stroke_color"].lstrip("#")[i:i+2], 16) for i in (0, 2, 4)) + (255,)
    sw = int(c["stroke_width"])

    if show_speaker:
        sp = cap["speaker"].upper()
        spc = tuple(int(c["speaker_label"]["color"].lstrip("#")[i:i+2], 16) for i in (0, 2, 4)) + (255,)
        spw = d.textlength(sp, font=speaker_font)
        # soft shadow + fill
        d.text(((W - spw) / 2 + 2, y + 2), sp, font=speaker_font, fill=(0, 0, 0, 160))
        d.text(((W - spw) / 2, y), sp, font=speaker_font, fill=spc,
               stroke_width=2, stroke_fill=(0, 0, 0, 200))
        y += speaker_h

    for line in lines:
        lw = d.textlength(line, font=font)
        x = (W - lw) / 2
        # drop shadow for legibility (in addition to the stroke)
        d.text((x + 2, y + 3), line, font=font, fill=(0, 0, 0, 170))
        d.text((x, y), line, font=font, fill=color, stroke_width=sw, stroke_fill=stroke)
        y += line_h

    img.save(out_path)


def build_caption_track(captions, total, spec, fps, tmp, W, H):
    """Render caption PNGs + a concat list into a transparent qtrle .mov."""
    font_path = find_font()
    c = spec["caption"]
    font = load_font(font_path, c["font_size"], c.get("font_weight"))
    sp_font = load_font(font_path, c["speaker_label"]["font_size"], 600)

    transparent = tmp / "blank.png"
    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(transparent)

    entries = []  # (file, duration)
    cursor = 0.0
    for i, cap in enumerate(captions):
        start, end = float(cap["start"]), float(cap["end"])
        if end > total:
            end = total
        if start >= total:
            break
        if start > cursor:
            entries.append((transparent, start - cursor))
        png = tmp / f"cap{i}.png"
        render_caption_png(cap, spec, font, sp_font, png, W, H)
        entries.append((png, max(0.05, end - start)))
        cursor = end
    if cursor < total:
        entries.append((transparent, total - cursor))

    listfile = tmp / "captions.concat"
    with open(listfile, "w") as f:
        for path, dur in entries:
            f.write(f"file '{path}'\n")
            f.write(f"duration {dur:.3f}\n")
        # concat demuxer holds the last image only 1 frame unless repeated
        f.write(f"file '{entries[-1][0]}'\n")

    cap_mov = tmp / "captions.mov"
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listfile),
        "-vf", f"fps={fps},format=rgba",
        "-c:v", "qtrle", str(cap_mov),
    ])
    return cap_mov


# --- Watermark ---------------------------------------------------------------

def build_watermark(tmp, spec, W, H):
    """Top-left brand wordmark watermark (Pillow text; opaque with shadow)."""
    font_path = find_font()
    font = load_font(font_path, 30, 600)
    pad = 36
    text = "WHEEL OF HEAVEN"
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((pad + 2, pad + 2), text, font=font, fill=(0, 0, 0, 180))
    d.text((pad, pad), text, font=font, fill=(244, 244, 245, 255),
           stroke_width=1, stroke_fill=(0, 0, 0, 160))
    wm = tmp / "watermark.png"
    img.save(wm)
    return wm


# --- ffmpeg helpers ----------------------------------------------------------

def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[-4000:])
        raise SystemExit(f"command failed ({p.returncode}): {' '.join(cmd[:6])} ...")
    return p


def ffprobe_duration(path):
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(p.stdout.strip())


def resolve_image(book, scene_image):
    book_dir = IMAGES / book
    if scene_image:
        p = book_dir / f"{scene_image}.jpg"
        if p.exists():
            return p
    d = book_dir / "default.jpg"
    return d if d.exists() else None


# --- Compose one chapter -----------------------------------------------------

def compose_chapter(book, lang, chapter, spec, preview=None, keep=False):
    cj = AUDIO / lang / book / f"c{chapter}.cinematic.json"
    if not cj.exists():
        print(f"  c{chapter}: no cinematic.json, skip", file=sys.stderr)
        return None
    data = json.loads(cj.read_text())
    out_spec = spec["output"]
    W, H, fps = out_spec["width"], out_spec["height"], out_spec["fps"]
    xfade = float(spec["scene"]["crossfade"])
    kb = spec["scene"]["ken_burns"]

    total = float(data["duration_seconds"])
    if preview:
        total = min(total, float(preview))

    scenes = [s for s in data["scenes"] if float(s["start"]) < total]
    for s in scenes:
        s["_end"] = min(float(s["end"]), total)
    captions = [c for c in data["captions"] if float(c["start"]) < total]

    voice = AUDIO / lang / book / f"c{chapter}.opus"
    ambient = AUDIO / lang / book / f"c{chapter}.ambient.opus"
    if not voice.exists():
        print(f"  c{chapter}: no audio ({voice.name}), skip", file=sys.stderr)
        return None
    has_ambient = ambient.exists()

    tmp = Path(tempfile.mkdtemp(prefix=f"woh_cine_c{chapter}_"))
    try:
        cap_mov = build_caption_track(captions, total, spec, fps, tmp, W, H)
        wm = build_watermark(tmp, spec, W, H)

        # --- inputs ---
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        scale_to = f"{int(W*2)}:{int(H*2)}"  # 2x for crisp Ken Burns
        seg_filters = []
        for i, s in enumerate(scenes):
            dur = (s["_end"] - float(s["start"])) + xfade
            frames = max(1, round(dur * fps))
            img = resolve_image(book, s.get("image"))
            if img:
                cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", str(img)]
                # zoom in on even scenes, out on odd, for variety
                if not kb.get("enabled", True):
                    z = "1"
                elif i % 2 == 0:
                    z = f"min(1+{kb['zoom_to']-1:.3f}*on/{frames},{kb['zoom_to']})"
                else:
                    z = f"max({kb['zoom_to']:.3f}-{kb['zoom_to']-1:.3f}*on/{frames},1)"
                seg_filters.append(
                    f"[{i}:v]scale={scale_to}:force_original_aspect_ratio=increase,"
                    f"crop={int(W*2)}:{int(H*2)},"
                    f"zoompan=z='{z}':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                    f"s={W}x{H}:fps={fps},setsar=1,format=yuv420p[v{i}]"
                )
            else:
                # gradient fallback (no published art yet)
                cmd += ["-f", "lavfi", "-t", f"{dur:.3f}",
                        "-i", f"color=c=0x0b0d18:s={W}x{H}:r={fps}"]
                seg_filters.append(f"[{i}:v]setsar=1,format=yuv420p[v{i}]")

        ncap = len(scenes)
        cmd += ["-i", str(cap_mov), "-i", str(wm), "-i", str(voice)]
        if has_ambient:
            cmd += ["-i", str(ambient)]
        cap_idx, wm_idx, voice_idx = ncap, ncap + 1, ncap + 2
        amb_idx = ncap + 3 if has_ambient else None

        # --- video filtergraph ---
        fc = list(seg_filters)
        if len(scenes) == 1:
            fc.append("[v0]copy[vslide]")
        else:
            prev = "v0"
            for i in range(1, len(scenes)):
                off = float(scenes[i]["start"])
                out = f"x{i}" if i < len(scenes) - 1 else "vslide"
                fc.append(
                    f"[{prev}][v{i}]xfade=transition=fade:duration={xfade}:offset={off:.3f}[{out}]"
                )
                prev = out

        vig = ",vignette" if spec.get("overlay", {}).get("vignette") else ""
        fc.append(f"[vslide]format=yuv420p{vig}[vvig]")
        fc.append(f"[vvig][{cap_idx}:v]overlay=0:0:format=auto[vcap]")
        fc.append(f"[vcap][{wm_idx}:v]overlay=0:0:format=auto,format=yuv420p[vout]")

        # --- audio ---
        if has_ambient:
            fc.append(
                f"[{voice_idx}:a]volume=1.0[va];[{amb_idx}:a]volume=0.30[aa];"
                f"[va][aa]amix=inputs=2:duration=longest:normalize=0[aout]"
            )
            amap = "[aout]"
        else:
            amap = f"{voice_idx}:a"

        cmd += ["-filter_complex", ";".join(fc),
                "-map", "[vout]", "-map", amap,
                "-c:v", out_spec["video_codec"], "-preset", out_spec["preset"],
                "-crf", str(out_spec["crf"]), "-pix_fmt", out_spec["pix_fmt"],
                "-r", str(fps),
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{total:.3f}"]
        if out_spec.get("faststart"):
            cmd += ["-movflags", "+faststart"]

        out_book = OUT_DIR / book
        out_book.mkdir(parents=True, exist_ok=True)
        suffix = f".preview{int(preview)}s" if preview else ""
        out_path = out_book / f"c{chapter}.{lang}{suffix}.mp4"
        cmd.append(str(out_path))

        print(f"  c{chapter} [{lang}]: {len(scenes)} scenes, {len(captions)} captions, "
              f"{total:.0f}s -> {out_path.name}")
        run(cmd)
        return out_path
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Render cinematic audiobook MP4s from cinematic.json.")
    ap.add_argument("--book", required=True)
    ap.add_argument("--lang", default="en")
    ap.add_argument("--all-langs", action="store_true")
    ap.add_argument("--chapters", type=int, nargs="*")
    ap.add_argument("--style", default="tbwtt", help="render spec id in style/<id>.json")
    ap.add_argument("--preview", type=float, help="render only the first N seconds (smoke test)")
    ap.add_argument("--keep-temp", action="store_true")
    args = ap.parse_args()

    spec = json.loads((STYLE_DIR / f"{args.style}.json").read_text())

    langs = ([p.name for p in sorted(AUDIO.iterdir()) if (p / args.book).is_dir()]
             if args.all_langs else [args.lang])

    for lang in langs:
        book_dir = AUDIO / lang / args.book
        if not book_dir.is_dir():
            print(f"[{lang}] no audio dir, skip", file=sys.stderr)
            continue
        chapters = args.chapters or sorted(
            int(m.group(1)) for f in book_dir.glob("c*.cinematic.json")
            if (m := re.match(r"c(\d+)\.cinematic\.json$", f.name))
        )
        print(f"[{lang}] {args.book}: chapters {chapters}")
        for ch in chapters:
            compose_chapter(args.book, lang, ch, spec, preview=args.preview, keep=args.keep_temp)


if __name__ == "__main__":
    main()
