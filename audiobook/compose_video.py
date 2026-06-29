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

from PIL import Image, ImageDraw, ImageFont, ImageFilter

REPO = Path(__file__).resolve().parents[2]
ASSETS = REPO / "assets.wheelofheaven.world"
AUDIO = ASSETS / "audio"
IMAGES = ASSETS / "images" / "cinematic"
STYLE_DIR = Path(__file__).resolve().parent / "style"
OUT_DIR = Path(__file__).resolve().parent / "out"
BRAND_DIR = Path(__file__).resolve().parent / "brand"  # logomark.svg + wordmark.svg (mirror bifrost)
SCORE_DIR = Path(__file__).resolve().parent / "score"  # subtle musical bed(s), video-export only
WIKI_DIR = ASSETS / "images" / "wiki"  # established character portraits

# Speaker -> portrait basename (in WIKI_DIR). Narrator IS Raël (the author
# looking back), so they share the likeness. The meta voice (AudioplayNarrator,
# kind=intro) shows no label, so it needs no portrait.
SPEAKER_PORTRAIT = {
    "Yahweh": "yahweh-eloha_thumb.webp",
    "Raël": "rael-claude-vorilhon_thumb.webp",
    "Rael": "rael-claude-vorilhon_thumb.webp",
    "Narrator": "rael-claude-vorilhon_thumb.webp",
}
_portrait_cache: dict = {}


def circle_portrait(speaker, diameter):
    """Return a circular RGBA portrait for a speaker at the given diameter, or
    None. Cached by (speaker, diameter)."""
    key = (speaker, diameter)
    if key in _portrait_cache:
        return _portrait_cache[key]
    name = SPEAKER_PORTRAIT.get(speaker)
    p = (WIKI_DIR / name) if name else None
    out = None
    if p and p.exists():
        try:
            im = Image.open(p).convert("RGB")
            sc = max(diameter / im.width, diameter / im.height)
            im = im.resize((max(1, int(im.width * sc)), max(1, int(im.height * sc))))
            im = im.crop(((im.width - diameter) // 2, (im.height - diameter) // 2,
                          (im.width - diameter) // 2 + diameter,
                          (im.height - diameter) // 2 + diameter)).convert("RGBA")
            mask = Image.new("L", (diameter, diameter), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
            im.putalpha(mask)
            # thin accent ring
            ring = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
            rd = ImageDraw.Draw(ring)
            rd.ellipse((1, 1, diameter - 2, diameter - 2), outline=(244, 244, 245, 230),
                       width=max(2, diameter // 40))
            im.alpha_composite(ring)
            out = im
        except Exception:
            out = None
    _portrait_cache[key] = out
    return out

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


def render_caption_png(cap, show_speaker, spec, font, speaker_font, out_path, W, H):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = spec["caption"]
    max_w = int(W * c["max_width_pct"] / 100)
    line_h = int(c["font_size"] * c["line_height"])
    lines = wrap_lines(d, cap["text"], font, max_w)

    avatar_d = int(speaker_font.size * 2.2) if show_speaker else 0
    avatar = circle_portrait(cap.get("speaker", ""), avatar_d) if show_speaker else None
    speaker_h = max(int(speaker_font.size * 1.8), avatar_d + int(speaker_font.size * 0.5)) if show_speaker else 0

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
        gap = int(speaker_font.size * 0.5) if avatar else 0
        group_w = (avatar.width + gap if avatar else 0) + spw
        gx = (W - group_w) / 2
        row_cy = y + speaker_h / 2          # vertical centre of the speaker row
        if avatar:
            img.alpha_composite(avatar, (int(gx), int(row_cy - avatar.height / 2)))
            gx += avatar.width + gap
        ty = row_cy - speaker_font.size * 0.62
        d.text((gx + 2, ty + 2), sp, font=speaker_font, fill=(0, 0, 0, 160))
        d.text((gx, ty), sp, font=speaker_font, fill=spc,
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


def build_caption_track(captions, total, spec, fps, tmp, W, H, offset=0.0):
    """Render caption PNGs + a concat list into a transparent qtrle .mov.
    `offset` prepends that many transparent seconds (so captions line up with
    audio that's been delayed behind an intro)."""
    font_path = find_font()
    c = spec["caption"]
    font = load_font(font_path, c["font_size"], c.get("font_weight"))
    sp_font = load_font(font_path, c["speaker_label"]["font_size"], 600)

    transparent = tmp / "blank.png"
    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(transparent)

    # Optional global nudge (seconds; negative = captions appear earlier). The
    # pipeline aligns captions to the audio by construction, so this defaults to
    # 0 — a safety valve if a perceived lag needs trimming.
    coff = float(c.get("offset_sec", 0.0))

    entries = []  # (file, duration)
    if offset > 0:
        entries.append((transparent, offset))
    cursor = 0.0
    prev_speaker = None
    for i, cap in enumerate(captions):
        start = max(0.0, float(cap["start"]) + coff)
        end = max(start + 0.05, float(cap["end"]) + coff)
        if end > total:
            end = total
        if start >= total:
            break
        if start > cursor:
            entries.append((transparent, start - cursor))
        # Label appears on a speaker change (not the intro), so each new
        # speaker's run is announced once (NARRATOR / RAËL / YAHWEH).
        show_speaker = (bool(cap.get("speaker")) and cap.get("kind") != "intro"
                        and cap.get("speaker") != prev_speaker)
        prev_speaker = cap.get("speaker")
        png = tmp / f"cap{i}.png"
        render_caption_png(cap, show_speaker, spec, font, sp_font, png, W, H)
        entries.append((png, max(0.05, end - start)))
        cursor = end
    # Always end on a transparent frame so the final caption is never the
    # held last frame (which would bleed over the outro during the crossfade).
    entries.append((transparent, max(0.3, total - cursor)))

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

def _rasterize_svg(svg_path, height, tmp, color):
    """Render a currentColor SVG to a transparent PNG at the given height,
    recolored to `color`, via resvg. Returns a Pillow RGBA image."""
    src = svg_path.read_text()
    src = src.replace("currentColor", color).replace("currentcolor", color)
    tmp_svg = tmp / f"{svg_path.stem}.recolor.svg"
    tmp_svg.write_text(src)
    png = tmp / f"{svg_path.stem}.{height}.png"
    run(["resvg", str(tmp_svg), str(png), "--height", str(int(height))])
    return Image.open(png).convert("RGBA")


def build_watermark(tmp, spec, W, H):
    """Top-left brand lockup watermark: the real logomark (wheel) + wordmark
    SVGs (so the typeface matches the brand), recolored opaque with a soft drop
    shadow for legibility. Mirrors the web cinematic view's .cinematic__brand."""
    wm = spec.get("watermark", {})
    color = wm.get("color", "#f4f4f5")
    mark_h = wm.get("mark_height", 32)
    word_h = wm.get("wordmark_height", 18)
    gap = wm.get("gap", 12)
    pad_x = wm.get("pad_x", 40)
    pad_y = wm.get("pad_y", 34)

    mark = _rasterize_svg(BRAND_DIR / "logomark.svg", mark_h, tmp, color)
    word = _rasterize_svg(BRAND_DIR / "wordmark.svg", word_h, tmp, color)

    lock_h = max(mark.height, word.height)
    lock_w = mark.width + gap + word.width
    lock = Image.new("RGBA", (lock_w, lock_h), (0, 0, 0, 0))
    lock.alpha_composite(mark, (0, (lock_h - mark.height) // 2))
    lock.alpha_composite(word, (mark.width + gap, (lock_h - word.height) // 2))

    # Soft drop shadow (≈ web's drop-shadow(0 1px 4px rgba(0,0,0,.7))).
    shadow_alpha = lock.split()[3].point(lambda a: int(a * 0.7))
    shadow = Image.new("RGBA", (lock_w, lock_h), (0, 0, 0, 0))
    blk = Image.new("RGBA", (lock_w, lock_h), (0, 0, 0, 255))
    blk.putalpha(shadow_alpha)
    shadow.alpha_composite(blk)
    shadow = shadow.filter(ImageFilter.GaussianBlur(2))

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.alpha_composite(shadow, (pad_x, pad_y + 1))
    layer.alpha_composite(lock, (pad_x, pad_y))
    out = tmp / "watermark.png"
    layer.save(out)
    return out


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


# --- Intro / title cards -----------------------------------------------------

def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i+2], 16) for i in (0, 2, 4))


# Localized "Chapter N" label for the title card. Falls back to English.
# CJK/Hebrew also need a glyph-capable caption font (Space Grotesk is Latin).
CHAPTER_LABEL = {
    "en": "CHAPTER {n}", "fr": "CHAPITRE {n}", "de": "KAPITEL {n}",
    "es": "CAPÍTULO {n}", "ru": "ГЛАВА {n}", "ja": "第{n}章",
    "ko": "제{n}장", "zh": "第{n}章", "zh-Hant": "第{n}章", "he": "פרק {n}",
}


def resolve_titles(book, lang, chapter):
    """(book_title, subtitle, chapter_title) from data-library meta + the audio
    manifest, falling back to a humanized slug."""
    book_title = book.replace("-", " ").title()
    subtitle = ""
    meta = REPO / "data-library" / book / "_meta.json"
    if meta.exists():
        m = json.loads(meta.read_text())
        book_title = (m.get("titles") or {}).get(lang) or book_title
        subtitle = (m.get("subtitles") or {}).get(lang, "")
    chapter_title = ""
    man = AUDIO / lang / book / "manifest.json"
    if man.exists():
        for c in json.loads(man.read_text()).get("chapters", []):
            if c.get("n") == chapter:
                chapter_title = c.get("title", "")
    return book_title, subtitle, chapter_title


# "Chapter N of M" label, localized. Falls back to the plain CHAPTER_LABEL when
# the total count is unknown.
CHAPTER_OF_LABEL = {
    "en": "CHAPTER {n} OF {m}", "fr": "CHAPITRE {n} SUR {m}", "de": "KAPITEL {n} VON {m}",
    "es": "CAPÍTULO {n} DE {m}", "ru": "ГЛАВА {n} ИЗ {m}", "ja": "第{n}章／全{m}章",
    "ko": "{m}장 중 제{n}장", "zh": "第{n}章／共{m}章", "zh-Hant": "第{n}章／共{m}章",
    "he": "פרק {n} מתוך {m}",
}


def resolve_chapter_count(book):
    meta = REPO / "data-library" / book / "_meta.json"
    if meta.exists():
        try:
            return int(json.loads(meta.read_text()).get("chapterCount") or 0)
        except (ValueError, json.JSONDecodeError):
            pass
    return 0


def chapter_label(lang, n, m):
    """'CHAPTER N OF M' (localized) when m is known, else 'CHAPTER N'."""
    if m and m > 0:
        return CHAPTER_OF_LABEL.get(lang, CHAPTER_OF_LABEL["en"]).format(n=n, m=m)
    return CHAPTER_LABEL.get(lang, CHAPTER_LABEL["en"]).format(n=n)


def build_intro_cards(spec, book, lang, chapter, tmp, W, H):
    """Render the brand card + the book/chapter title card. Returns a list of
    (png_path, seconds)."""
    intro = spec.get("intro", {})
    bg = _hex(intro.get("bg_color", "#05060a"))
    font_path = find_font()
    title_book, subtitle, title_chap = resolve_titles(book, lang, chapter)

    def paste_center(card, png, cx, cy):
        card.alpha_composite(png, (int(cx - png.width / 2), int(cy - png.height / 2)))

    # --- brand card: logomark + wordmark on the dark backdrop ---
    brand = Image.new("RGBA", (W, H), bg + (255,))
    mark = _rasterize_svg(BRAND_DIR / "logomark.svg", int(H * 0.16), tmp, "#f4f4f5")
    word = _rasterize_svg(BRAND_DIR / "wordmark.svg", int(H * 0.05), tmp, "#f4f4f5")
    paste_center(brand, mark, W / 2, H / 2 - word.height)
    paste_center(brand, word, W / 2, H / 2 + mark.height * 0.55)
    brand_png = tmp / "intro_brand.png"
    brand.save(brand_png)

    # --- title card: dimmed backdrop + titles ---
    title = Image.new("RGBA", (W, H), bg + (255,))
    bd = resolve_image(book, intro.get("backdrop", "default"))
    if bd:
        im = Image.open(bd).convert("RGB")
        sc = max(W / im.width, H / im.height)
        im = im.resize((int(im.width * sc), int(im.height * sc)))
        im = im.crop(((im.width - W) // 2, (im.height - H) // 2,
                      (im.width - W) // 2 + W, (im.height - H) // 2 + H))
        title.alpha_composite(im.convert("RGBA"), (0, 0))
        scrim = Image.new("RGBA", (W, H), (0, 0, 0, 150))
        title.alpha_composite(scrim)
    d = ImageDraw.Draw(title)
    smallmark = _rasterize_svg(BRAND_DIR / "logomark.svg", int(H * 0.06), tmp, "#f4f4f5")
    paste_center(title, smallmark, W / 2, H * 0.26)

    bt_font = load_font(font_path, int(H * 0.075), 600)
    ch_font = load_font(font_path, int(H * 0.032), 600)
    sub_font = load_font(font_path, int(H * 0.026), 400)
    accent = _hex(spec["caption"]["speaker_label"]["color"])

    def centered(text, font, y, fill):
        for line in wrap_lines(d, text, font, int(W * 0.8)):
            lw = d.textlength(line, font=font)
            d.text(((W - lw) / 2 + 2, y + 2), line, font=font, fill=(0, 0, 0, 170))
            d.text(((W - lw) / 2, y), line, font=font, fill=fill + (255,),
                   stroke_width=1, stroke_fill=(0, 0, 0, 160))
            y += int(font.size * 1.25)
        return y

    y = int(H * 0.40)
    y = centered(title_book, bt_font, y, _hex(spec["caption"]["color"]))
    y += int(H * 0.02)
    d.line([(W * 0.42, y), (W * 0.58, y)], fill=accent + (220,), width=2)
    y += int(H * 0.03)
    chap_word = chapter_label(lang, chapter, resolve_chapter_count(book))
    chap_line = chap_word + (f"   ·   {title_chap.upper()}" if title_chap else "")
    y = centered(chap_line, ch_font, y, accent)
    if subtitle:
        y += int(H * 0.015)
        centered(subtitle, sub_font, y, (200, 200, 210))
    title_png = tmp / "intro_title.png"
    title.save(title_png)

    return [(brand_png, float(intro.get("brand_seconds", 3.0))),
            (title_png, float(intro.get("title_seconds", 4.5)))]


def _cover(im, W, H):
    """Scale-cover an RGB image to WxH (center crop)."""
    sc = max(W / im.width, H / im.height)
    im = im.resize((max(1, int(im.width * sc)), max(1, int(im.height * sc))))
    x, y = (im.width - W) // 2, (im.height - H) // 2
    return im.crop((x, y, x + W, y + H))


def build_outro_card(spec, book, lang, chapter, tmp, W, H):
    """Render the closing credits card over the (dimmed) hero. Returns
    (png_path, seconds) or None if disabled."""
    outro = spec.get("outro", {})
    if not outro.get("enabled", False):
        return None
    bg = _hex(spec.get("intro", {}).get("bg_color", "#05060a"))
    title_book, subtitle, title_chap = resolve_titles(book, lang, chapter)
    count = resolve_chapter_count(book)
    subs = {"book_title": title_book, "author": outro.get("author", ""),
            "subtitle": subtitle or "",
            "chapter_of_n": chapter_label(lang, chapter, count),
            "chapter_title": title_chap or ""}

    card = Image.new("RGBA", (W, H), bg + (255,))
    bd = resolve_image(book, outro.get("backdrop", "intro-hero"))
    if bd:
        card.alpha_composite(_cover(Image.open(bd).convert("RGB"), W, H).convert("RGBA"))
        card.alpha_composite(Image.new("RGBA", (W, H), (0, 0, 0, 185)))  # heavier scrim
    d = ImageDraw.Draw(card)
    mark = _rasterize_svg(BRAND_DIR / "logomark.svg", int(H * 0.085), tmp, "#f4f4f5")
    card.alpha_composite(mark, (int(W / 2 - mark.width / 2), int(H * 0.18)))

    font_path = find_font()
    accent = _hex(spec["caption"]["speaker_label"]["color"])
    text_col = _hex(spec["caption"]["color"])
    styles = {  # role -> (rel_size, weight, color)
        "title":    (0.052, 600, text_col),
        "subtitle": (0.024, 400, (200, 200, 210)),
        "chapter":  (0.026, 600, accent),
        "byline":   (0.026, 400, (215, 215, 225)),
        "line":     (0.028, 500, (215, 215, 225)),
        "url":      (0.028, 600, accent),
        "cta":      (0.022, 500, (208, 208, 220)),
        "fine":     (0.017, 400, (150, 150, 160)),
    }
    y = int(H * 0.34)
    for item in outro.get("credits", []):
        role = item.get("role", "line")
        rel, weight, col = styles.get(role, styles["line"])
        text = item.get("text", "").format(**subs).strip(" ·")
        if not text:
            continue
        font = load_font(font_path, int(H * rel), weight)
        if role in ("byline", "cta", "fine"):
            y += int(H * 0.025)
        for line in wrap_lines(d, text, font, int(W * 0.82)):
            lw = d.textlength(line, font=font)
            d.text(((W - lw) / 2, y), line, font=font, fill=col + (255,),
                   stroke_width=1, stroke_fill=(0, 0, 0, 160))
            y += int(font.size * 1.3)
        y += int(H * 0.012)
    out_png = tmp / "outro.png"
    card.save(out_png)
    return out_png, float(outro.get("seconds", 8.0))


_NEXT_WORD = {"en": "NEXT", "fr": "À SUIVRE", "de": "ALS NÄCHSTES", "es": "A CONTINUACIÓN",
              "ru": "ДАЛЕЕ", "ja": "次回", "ko": "다음 편", "zh": "下一章", "zh-Hant": "下一章",
              "he": "בהמשך"}


def build_endcard(spec, book, lang, chapter, tmp, W, H):
    """Up-next teaser for the next chapter over the hero. Returns (png, seconds),
    or None if disabled or this is the final chapter."""
    ec = spec.get("endcard", {})
    if not ec.get("enabled", False):
        return None
    count = resolve_chapter_count(book)
    nxt = chapter + 1
    if count and nxt > count:
        return None   # final chapter — nothing to tease
    _bt, _sub, next_title = resolve_titles(book, lang, nxt)
    bg = _hex(spec.get("intro", {}).get("bg_color", "#05060a"))
    card = Image.new("RGBA", (W, H), bg + (255,))
    bd = resolve_image(book, ec.get("backdrop", "intro-hero"))
    if bd:
        card.alpha_composite(_cover(Image.open(bd).convert("RGB"), W, H).convert("RGBA"))
        card.alpha_composite(Image.new("RGBA", (W, H), (0, 0, 0, 165)))
    d = ImageDraw.Draw(card)
    font_path = find_font()
    accent = _hex(spec["caption"]["speaker_label"]["color"])
    text_col = _hex(spec["caption"]["color"])
    eyebrow = load_font(font_path, int(H * 0.030), 600)
    cf = load_font(font_path, int(H * 0.034), 600)
    nf = load_font(font_path, int(H * 0.060), 600)

    def center(text, font, y, fill):
        for line in wrap_lines(d, text, font, int(W * 0.8)):
            lw = d.textlength(line, font=font)
            d.text(((W - lw) / 2, y), line, font=font, fill=fill + (255,),
                   stroke_width=1, stroke_fill=(0, 0, 0, 160))
            y += int(font.size * 1.25)
        return y

    y = int(H * 0.34)
    # small filled play-triangle to the left of the eyebrow (Space Grotesk has
    # no ▶ glyph, so draw it instead of using the character)
    nxt_word = _NEXT_WORD.get(lang, "NEXT")
    ew = d.textlength(nxt_word, font=eyebrow)
    tri = int(eyebrow.size * 0.7)
    gap = int(eyebrow.size * 0.4)
    gx = (W - (tri + gap + ew)) / 2
    tri_y = y + (eyebrow.size - tri) / 2 + eyebrow.size * 0.12
    d.polygon([(gx, tri_y), (gx, tri_y + tri), (gx + tri * 0.9, tri_y + tri / 2)],
              fill=accent + (255,))
    d.text((gx + tri + gap, y), nxt_word, font=eyebrow, fill=accent + (255,),
           stroke_width=1, stroke_fill=(0, 0, 0, 160))
    y += int(eyebrow.size * 1.25)
    y += int(H * 0.02)
    y = center(chapter_label(lang, nxt, count), cf, y, text_col)
    y += int(H * 0.01)
    if next_title:
        center(next_title, nf, y, text_col)
    mark = _rasterize_svg(BRAND_DIR / "logomark.svg", int(H * 0.07), tmp, "#f4f4f5")
    card.alpha_composite(mark, (int(W / 2 - mark.width / 2), int(H * 0.80)))
    out_png = tmp / "endcard.png"
    card.save(out_png)
    return out_png, float(ec.get("seconds", 6.0))


def build_thumbnail(spec, book, lang, chapter, out_path):
    """Render a 1280x720 YouTube thumbnail: hero + title + chapter + logomark."""
    thumb = spec.get("thumbnail", {})
    if not thumb.get("enabled", False):
        return None
    W, H = 1280, 720
    bg = _hex(spec.get("intro", {}).get("bg_color", "#05060a"))
    title_book, _sub, title_chap = resolve_titles(book, lang, chapter)
    tmp = Path(tempfile.mkdtemp(prefix="woh_thumb_"))
    try:
        card = Image.new("RGBA", (W, H), bg + (255,))
        bd = resolve_image(book, thumb.get("backdrop", "intro-hero"))
        if bd:
            card.alpha_composite(_cover(Image.open(bd).convert("RGB"), W, H).convert("RGBA"))
        # left-weighted gradient scrim for text legibility
        grad = Image.new("L", (W, 1))
        for x in range(W):
            grad.putpixel((x, 0), int(200 * max(0, 1 - x / (W * 0.7))))
        scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        scrim.putalpha(grad.resize((W, H)))
        card.alpha_composite(Image.composite(Image.new("RGBA", (W, H), (5, 6, 10, 255)),
                                             Image.new("RGBA", (W, H), (0, 0, 0, 0)), scrim))
        d = ImageDraw.Draw(card)
        font_path = find_font()
        accent = _hex(spec["caption"]["speaker_label"]["color"])
        text_col = _hex(spec["caption"]["color"])
        mark = _rasterize_svg(BRAND_DIR / "logomark.svg", int(H * 0.11), tmp, "#f4f4f5")
        card.alpha_composite(mark, (int(W * 0.06), int(H * 0.10)))
        bt_font = load_font(font_path, int(H * 0.11), 600)
        ch_font = load_font(font_path, int(H * 0.05), 600)
        x = int(W * 0.06)
        y = int(H * 0.40)
        for line in wrap_lines(d, title_book, bt_font, int(W * 0.62)):
            d.text((x, y), line, font=bt_font, fill=text_col + (255,),
                   stroke_width=2, stroke_fill=(0, 0, 0, 200))
            y += int(bt_font.size * 1.15)
        chap_word = chapter_label(lang, chapter, resolve_chapter_count(book))
        chap_line = chap_word + (f" · {title_chap.upper()}" if title_chap else "")
        y += int(H * 0.02)
        d.text((x, y), chap_line, font=ch_font, fill=accent + (255,),
               stroke_width=1, stroke_fill=(0, 0, 0, 180))
        card.convert("RGB").save(out_path, quality=90)
        return out_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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

    # Optional subtle musical score bed (video export only — never in the web
    # player). Plays from t=0 under the intro cards, loops across the whole
    # chapter beneath voice + ambient, fades in/out. Spec: style.score.
    score_cfg = spec.get("score", {})
    score_file = score_cfg.get("file")
    score_path = (SCORE_DIR / score_file) if score_file else None
    has_score = bool(score_cfg.get("enabled", False)) and score_path and score_path.exists()
    if score_cfg.get("enabled") and not has_score:
        print(f"  c{chapter}: score enabled but {score_path} missing, skipping score",
              file=sys.stderr)

    tmp = Path(tempfile.mkdtemp(prefix=f"woh_cine_c{chapter}_"))
    try:
        intro_cfg = spec.get("intro", {})
        intro_on = bool(intro_cfg.get("enabled", True))
        intro_cards = build_intro_cards(spec, book, lang, chapter, tmp, W, H) if intro_on else []
        intro_total = sum(d for _, d in intro_cards)  # the chapter (scene 0) starts here

        outro_card = build_outro_card(spec, book, lang, chapter, tmp, W, H)
        outro_total = outro_card[1] if outro_card else 0.0
        endcard = build_endcard(spec, book, lang, chapter, tmp, W, H)
        endcard_total = endcard[1] if endcard else 0.0
        tail_total = outro_total + endcard_total   # everything after the chapter

        jingle_file = intro_cfg.get("jingle")
        jingle_path = (SCORE_DIR / jingle_file) if jingle_file else None
        has_jingle = bool(intro_cards) and jingle_path and jingle_path.exists()

        cap_mov = build_caption_track(captions, total, spec, fps, tmp, W, H, offset=intro_total)
        wm = build_watermark(tmp, spec, W, H)

        # --- inputs ---
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        # Supersample factor for the Ken Burns stage. zoompan computes its
        # crop window in this enlarged space and rounds x/y to whole pixels;
        # the bigger the space, the smaller that rounding is at output scale,
        # so the slow zoom glides instead of wobbling. 4x => ~0.25px jitter.
        ss = int(spec.get("scene", {}).get("supersample", 4))
        scale_to = f"{int(W*ss)}:{int(H*ss)}"
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
                    f"[{i}:v]scale={scale_to}:force_original_aspect_ratio=increase:flags=lanczos,"
                    f"crop={int(W*ss)}:{int(H*ss)},"
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

        # intro card inputs (added after the audio inputs)
        intro_seg, intro_labels = [], []
        next_idx = (ncap + 4) if has_ambient else (ncap + 3)
        for k, (png, dur) in enumerate(intro_cards):
            cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur + xfade:.3f}", "-i", str(png)]
            fin = ",fade=t=in:st=0:d=0.6" if k == 0 else ""   # fade up from black
            intro_seg.append(f"[{next_idx}:v]scale={W}:{H},setsar=1{fin},format=yuv420p[iv{k}]")
            off = None if k == 0 else sum(d for _, d in intro_cards[:k])
            intro_labels.append((f"iv{k}", off))
            next_idx += 1

        # outro card input (still image, appended after the chapter)
        outro_seg, outro_idx = [], None
        if outro_card:
            cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{outro_total + xfade:.3f}",
                    "-i", str(outro_card[0])]
            outro_idx = next_idx
            next_idx += 1
            outro_seg.append(f"[{outro_idx}:v]scale={W}:{H},setsar=1,format=yuv420p[ovid]")

        # endcard input (up-next teaser, after the outro)
        endcard_seg, endcard_idx = [], None
        if endcard:
            cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{endcard_total + xfade:.3f}",
                    "-i", str(endcard[0])]
            endcard_idx = next_idx
            next_idx += 1
            endcard_seg.append(f"[{endcard_idx}:v]scale={W}:{H},setsar=1,format=yuv420p[evid]")

        # jingle input (the brand-card sting)
        jingle_idx = None
        if has_jingle:
            cmd += ["-i", str(jingle_path)]
            jingle_idx = next_idx
            next_idx += 1

        # score input (looped to fill the whole video, trimmed in the graph)
        score_idx = None
        if has_score:
            cmd += ["-stream_loop", "-1", "-i", str(score_path)]
            score_idx = next_idx
            next_idx += 1

        # --- video filtergraph: intro cards then the scene xfade chain ---
        # `clips` = ordered (label, transition-offset); offset is the absolute
        # time the crossfade INTO that clip begins. Scenes shift by intro_total.
        fc = list(seg_filters) + intro_seg + outro_seg + endcard_seg
        clips = list(intro_labels)
        clips += [(f"v{i}", intro_total + float(scenes[i]["start"])) for i in range(len(scenes))]
        if outro_card:
            clips += [("ovid", intro_total + total)]   # xfades in as the chapter ends
        if endcard:
            clips += [("evid", intro_total + total + outro_total)]   # up-next, after the outro
        prev = clips[0][0]
        if len(clips) == 1:
            fc.append(f"[{prev}]copy[vslide]")
        else:
            for k in range(1, len(clips)):
                label, off = clips[k]
                out = "vslide" if k == len(clips) - 1 else f"x{k}"
                fc.append(f"[{prev}][{label}]xfade=transition=fade:duration={xfade}:offset={off:.3f}[{out}]")
                prev = out

        vig = ",vignette" if spec.get("overlay", {}).get("vignette") else ""
        fc.append(f"[vslide]format=yuv420p{vig}[vvig]")
        fc.append(f"[vvig][{cap_idx}:v]overlay=0:0:format=auto:eof_action=pass[vcap]")
        # watermark only over the chapter body — not the intro cards or the outro
        chap_end = intro_total + total
        if intro_total > 0 or outro_total > 0:
            wm_en = f":enable='between(t,{intro_total:.3f},{chap_end:.3f})'"
        else:
            wm_en = ""
        fc.append(f"[vcap][{wm_idx}:v]overlay=0:0:format=auto{wm_en},format=yuv420p[vout]")

        # --- audio ---
        # Voice + ambient are delayed behind the intro so narration starts
        # with the chapter; the score plays from t=0 under the title cards.
        d_ms = int(round(intro_total * 1000))
        ad = f"adelay={d_ms}:all=1," if d_ms > 0 else ""
        parts = []
        fc.append(f"[{voice_idx}:a]{ad}volume=1.0[va]")
        parts.append("[va]")
        if has_ambient:
            fc.append(f"[{amb_idx}:a]{ad}volume=0.30[aa]")
            parts.append("[aa]")
        if has_score:
            end = total + intro_total + tail_total   # score carries through outro + endcard
            sg = float(score_cfg.get("gain", 0.12))
            ig = float(score_cfg.get("intro_gain", max(sg, 0.28)))
            fin = float(score_cfg.get("fade_in", 2.0))
            fout = float(score_cfg.get("fade_out", 4.0))
            fo_start = max(0.0, end - fout)
            # Louder under the (voice-free) intro cards, ramping down to the
            # base bed level over `ramp` seconds, landing at base just as the
            # narration comes in. Constant gain when there's no intro.
            if intro_total > 0 and ig != sg:
                ramp = min(1.5, intro_total)
                r0 = max(0.0, intro_total - ramp)
                vol = (f"volume='if(lt(t,{r0:.3f}),{ig},"
                       f"if(lt(t,{intro_total:.3f}),"
                       f"{ig}+({sg}-{ig})*(t-{r0:.3f})/{ramp:.3f},{sg}))':eval=frame")
            else:
                vol = f"volume={sg}"
            fc.append(
                f"[{score_idx}:a]atrim=0:{end:.3f},asetpts=PTS-STARTPTS,"
                f"{vol},afade=t=in:st=0:d={fin:.3f},"
                f"afade=t=out:st={fo_start:.3f}:d={fout:.3f}[sc]"
            )
            parts.append("[sc]")
        if has_jingle:
            brand_sec = intro_cards[0][1]
            jg = float(intro_cfg.get("jingle_gain", 0.9))
            jfo = float(intro_cfg.get("jingle_fade_out", 1.5))
            jfo_start = max(0.0, brand_sec - jfo)
            fc.append(
                f"[{jingle_idx}:a]atrim=0:{brand_sec:.3f},asetpts=PTS-STARTPTS,"
                f"volume={jg},afade=t=out:st={jfo_start:.3f}:d={jfo:.3f}[jng]"
            )
            parts.append("[jng]")
        if len(parts) == 1:
            amap = parts[0]
        else:
            fc.append(f"{''.join(parts)}amix=inputs={len(parts)}:duration=longest:normalize=0[aout]")
            amap = "[aout]"

        cmd += ["-filter_complex", ";".join(fc),
                "-map", "[vout]", "-map", amap,
                "-c:v", out_spec["video_codec"], "-preset", out_spec["preset"],
                "-crf", str(out_spec["crf"]), "-pix_fmt", out_spec["pix_fmt"],
                "-r", str(fps),
                "-c:a", "aac", "-b:a", "192k",
                "-t", f"{total + intro_total + tail_total:.3f}"]
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
        if not preview and spec.get("thumbnail", {}).get("enabled"):
            thumb_path = out_book / f"c{chapter}.{lang}.thumb.jpg"
            try:
                build_thumbnail(spec, book, lang, chapter, thumb_path)
                print(f"    thumbnail -> {thumb_path.name}")
            except Exception as e:
                print(f"    thumbnail failed: {e}", file=sys.stderr)
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
