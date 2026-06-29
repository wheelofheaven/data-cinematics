#!/usr/bin/env python3
"""
Phase 0 — Cinematic timeline builder.

Derives a per-chapter `c{n}.cinematic.json` from data the project already
produces:

  - word/paragraph forced-alignment timing
        assets.wheelofheaven.world/audio/{lang}/{book}/c{n}.timing.json
  - scene cue sheet (the "shot list")
        data-library/{book}/audioplay/cues/c{n}.yaml

The output is the single source of truth consumed by BOTH renderers:

  - the web cinematic view (fetched client-side like timing.json)
  - the ffmpeg compositor (read locally)

It contains two tracks:

  scenes[]    {scene, image, start, end}      <- cue YAML x paragraph timings
  captions[]  {text, start, end, speaker, kind, paragraph, words[]}
                                              <- timing.json words -> sentences

Nothing here depends on the actual scene images existing yet. `image` is a
slot (the scene id); the image-generation pipeline fills it later. Scenes
with no cue active become a `default` segment so the video never goes black.

Usage:
    python build_timeline.py --book the-book-which-tells-the-truth --lang en
    python build_timeline.py --book the-book-which-tells-the-truth --lang en --chapters 1
    python build_timeline.py --book the-book-which-tells-the-truth --all-langs --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Repo root = two levels up from this file (data-cinematics/audiobook/..)
REPO = Path(__file__).resolve().parents[2]
ASSETS = REPO / "assets.wheelofheaven.world" / "audio"
LIBRARY = REPO / "data-library"

# Sentence-splitting heuristic ------------------------------------------------
# We split a paragraph into sentences by walking the *word tokens* (each of
# which already carries start/end), so sentence times fall out for free. The
# heuristic is deliberately conservative and tunable; the goal is "the right
# sentence is on screen", not perfect linguistics.

SENTENCE_END = re.compile(r'[.!?…]["”’\')\]]*$')
# Tokens that end in "." but should NOT end a sentence.
ABBREV = {
    "mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "etc.", "e.g.", "i.e.",
    "no.", "jr.", "sr.", "a.m.", "p.m.", "cf.", "al.", "fig.", "ca.",
}
# Single capital letter + dot, e.g. "J." (an initial) — don't split.
INITIAL = re.compile(r'^[A-Z]\.$')


def is_sentence_end(token: str, next_token: str | None) -> bool:
    """Decide whether `token` terminates a sentence."""
    if not SENTENCE_END.search(token):
        return False
    bare = token.lower()
    if bare in ABBREV or INITIAL.match(token):
        return False
    # If the next token starts lowercase, the period was probably an
    # abbreviation or mid-sentence; keep going. (No next token => end.)
    if next_token is not None:
        first = next_token[0:1]
        if first and first.islower():
            return False
    return True


def split_paragraph_sentences(words: list[dict]) -> list[dict]:
    """Group a paragraph's timed words into timed sentences."""
    sentences: list[dict] = []
    buf: list[dict] = []
    for i, w in enumerate(words):
        buf.append(w)
        nxt = words[i + 1]["w"] if i + 1 < len(words) else None
        if is_sentence_end(w["w"], nxt):
            sentences.append(_emit_sentence(buf))
            buf = []
    if buf:
        sentences.append(_emit_sentence(buf))
    return sentences


def _emit_sentence(buf: list[dict]) -> dict:
    text = " ".join(w["w"] for w in buf)
    # Light cleanup: no space before sentence punctuation, collapse doubles.
    text = re.sub(r'\s+([,.;:!?…])', r'\1', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return {
        "text": text,
        "start": round(buf[0]["start"], 3),
        "end": round(buf[-1]["end"], 3),
        "words": [
            {"w": w["w"], "start": round(w["start"], 3), "end": round(w["end"], 3)}
            for w in buf
        ],
    }


# Track builders --------------------------------------------------------------

def build_captions(timing: dict) -> list[dict]:
    captions: list[dict] = []
    for p in timing["paragraphs"]:
        words = p.get("words") or []
        if not words:
            continue
        for s in split_paragraph_sentences(words):
            s["speaker"] = p.get("speaker")
            s["kind"] = p.get("kind", "body")
            s["paragraph"] = p["n"]
            captions.append(s)
    return captions


def load_cues(book: str, chapter: int) -> list[dict]:
    """Parse a cue sheet without a YAML dep (the schema is trivial & flat)."""
    path = LIBRARY / book / "audioplay" / "cues" / f"c{chapter}.yaml"
    if not path.exists():
        return []
    cues: list[dict] = []
    cur: dict | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("- paragraph:"):
            if cur:
                cues.append(cur)
            cur = {"paragraph": int(s.split(":", 1)[1].strip())}
        elif s.startswith("scene:") and cur is not None:
            val = s.split(":", 1)[1].strip().strip('"')
            cur["scene"] = val
    if cur:
        cues.append(cur)
    return cues


def build_scenes(timing: dict, cues: list[dict], default_scene: str = "default") -> list[dict]:
    """
    Turn sparse cues into a continuous, gap-filled scene track over the whole
    chapter. Each cue's paragraph number maps to that paragraph's start time;
    a scene runs until the next cue boundary. scene:"" ends the current run.
    """
    duration = float(timing["duration_seconds"])
    # paragraph n -> start time
    p_start = {p["n"]: float(p["start"]) for p in timing["paragraphs"]}

    # Build (time, scene) boundary points from cues.
    points: list[tuple[float, str]] = []
    missing: list[int] = []
    for c in cues:
        n = c["paragraph"]
        if n not in p_start:
            missing.append(n)
            continue
        scene = c.get("scene", "") or default_scene
        points.append((p_start[n], scene))
    if missing:
        print(f"  ! cue paragraphs not found in timing: {missing}", file=sys.stderr)

    points.sort(key=lambda x: x[0])

    # The chapter opens on the default scene until the first cue.
    segments: list[dict] = []
    cursor = 0.0
    current = default_scene
    for t, scene in points:
        if t > cursor:
            segments.append({"scene": current, "start": cursor, "end": t})
        cursor = t
        current = scene
    if cursor < duration:
        segments.append({"scene": current, "start": cursor, "end": duration})

    # Merge adjacent identical scenes; attach the image slot.
    merged: list[dict] = []
    for seg in segments:
        if merged and merged[-1]["scene"] == seg["scene"]:
            merged[-1]["end"] = seg["end"]
        else:
            merged.append(dict(seg))

    # Coalesce away too-short segments so the slideshow never flips faster than
    # the eye can settle (e.g. the ~1s 'default' pre-pause sliver before the
    # meta-narration). A short segment is absorbed into its previous neighbour;
    # if it's the very first segment, the next one is extended back to cover it.
    MIN_SCENE_SEC = 2.5
    i = 0
    while len(merged) > 1 and i < len(merged):
        if (merged[i]["end"] - merged[i]["start"]) >= MIN_SCENE_SEC:
            i += 1
            continue
        if i > 0:
            merged[i - 1]["end"] = merged[i]["end"]
            merged.pop(i)
            i = max(0, i - 1)
        else:
            merged[i + 1]["start"] = merged[i]["start"]
            merged.pop(i)
    # Re-merge any adjacent identical scenes the coalescing may have created.
    remerged: list[dict] = []
    for seg in merged:
        if remerged and remerged[-1]["scene"] == seg["scene"]:
            remerged[-1]["end"] = seg["end"]
        else:
            remerged.append(dict(seg))
    merged = remerged

    for seg in merged:
        seg["start"] = round(seg["start"], 3)
        seg["end"] = round(seg["end"], 3)
        # image slot: the scene id IS the image basename; null for default.
        seg["image"] = None if seg["scene"] == default_scene else seg["scene"]
    return merged


# Driver ----------------------------------------------------------------------

def build_chapter(book: str, lang: str, chapter: int, write: bool) -> dict | None:
    timing_path = ASSETS / lang / book / f"c{chapter}.timing.json"
    if not timing_path.exists():
        print(f"  - c{chapter}: no timing.json, skip", file=sys.stderr)
        return None
    timing = json.loads(timing_path.read_text(encoding="utf-8"))
    cues = load_cues(book, chapter)
    captions = build_captions(timing)
    scenes = build_scenes(timing, cues)

    out = {
        "book": book,
        "lang": lang,
        "chapter": chapter,
        "duration_seconds": timing["duration_seconds"],
        "scene_count": len(scenes),
        "caption_count": len(captions),
        "scenes": scenes,
        "captions": captions,
    }

    distinct = sorted({s["scene"] for s in scenes if s["scene"] != "default"})
    print(
        f"  c{chapter}: {len(captions)} captions, {len(scenes)} scene segments, "
        f"distinct scenes={distinct or '(none)'}"
    )

    if write:
        out_path = ASSETS / lang / book / f"c{chapter}.cinematic.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"    -> {out_path.relative_to(REPO)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Build cinematic timeline JSON from timing + cues.")
    ap.add_argument("--book", required=True)
    ap.add_argument("--lang", default="en")
    ap.add_argument("--all-langs", action="store_true", help="Build every lang present under assets/audio.")
    ap.add_argument("--chapters", type=int, nargs="*", help="Specific chapter numbers (default: all found).")
    ap.add_argument("--dry-run", action="store_true", help="Compute and report, but do not write files.")
    args = ap.parse_args()

    langs = []
    if args.all_langs:
        langs = sorted(p.name for p in ASSETS.iterdir() if (p / args.book).is_dir())
    else:
        langs = [args.lang]

    if not langs:
        print(f"No languages found for book {args.book!r}", file=sys.stderr)
        return 1

    for lang in langs:
        book_dir = ASSETS / lang / args.book
        if not book_dir.is_dir():
            print(f"[{lang}] no audio dir, skip", file=sys.stderr)
            continue
        if args.chapters:
            chapters = args.chapters
        else:
            chapters = sorted(
                int(m.group(1))
                for f in book_dir.glob("c*.timing.json")
                if (m := re.match(r"c(\d+)\.timing\.json$", f.name))
            )
        print(f"[{lang}] {args.book}: chapters {chapters}{' (dry-run)' if args.dry_run else ''}")
        for ch in chapters:
            build_chapter(args.book, lang, ch, write=not args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
