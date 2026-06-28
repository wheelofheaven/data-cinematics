# Cinematic audiobook pipeline

Turns an existing audiobook (audio + forced-alignment timing + scene cues +
scene images) into:

1. a **cinematic timeline** (`c{n}.cinematic.json`) — the shared data model, and
2. a **YouTube-ready MP4** per chapter per language (the ffmpeg compositor).

It is the *video* counterpart to the live **web cinematic view** in the bifrost
player. Both renderers consume the same timeline + the same render spec
(`style/<book>.json`) so the video matches the web look without screen-capture.

Full design: [`.claude/plans/cinematic-audiobook.md`](../../.claude/plans/cinematic-audiobook.md).

## Inputs (all but the images already exist)

| Input | Location |
|-------|----------|
| Audio (voice + ambient) | `assets.wheelofheaven.world/audio/{lang}/{book}/c{n}.opus`, `c{n}.ambient.opus` |
| Word/paragraph timing | `assets.wheelofheaven.world/audio/{lang}/{book}/c{n}.timing.json` |
| Scene cue sheets | `data-library/{book}/audioplay/cues/c{n}.yaml` |
| Scene images | `data-library/{book}/audioplay/scenes/{scene}.jpg` (see that dir's README) |
| Render spec | `style/{book}.json` |

## Pipeline

```
                build_timeline.py
 timing.json  ┐
 cues/c*.yaml ┘──► c{n}.cinematic.json ──┬──► web cinematic view (bifrost)
                                         └──► compose_video.py ──► c{n}.{lang}.mp4
 scenes/*.jpg ───────────────────────────────────────┘
 style/<book>.json ──────────────────────► both renderers
```

## Phase 0 — build the timeline (implemented)

```bash
# One language, all chapters (writes c{n}.cinematic.json next to timing.json)
python build_timeline.py --book the-book-which-tells-the-truth --lang en

# Preview without writing
python build_timeline.py --book the-book-which-tells-the-truth --lang en --dry-run

# Every language present under assets/audio
python build_timeline.py --book the-book-which-tells-the-truth --all-langs
```

Output schema (`c{n}.cinematic.json`):

```jsonc
{
  "book": "...", "lang": "en", "chapter": 1, "duration_seconds": 780.737,
  "scenes":   [ { "scene": "elohim-vessel", "image": "elohim-vessel",
                  "start": 360.76, "end": 363.33 }, ... ],   // gap-filled, contiguous
  "captions": [ { "text": "...", "start": 1.0, "end": 3.39,
                  "speaker": "Narrator", "kind": "intro", "paragraph": 0,
                  "words": [ { "w": "The", "start": 1.0, "end": 1.15 }, ... ] }, ... ]
}
```

## Phase 3 — compose the video (implemented)

`compose_video.py` renders a YouTube-ready MP4 per chapter per language from the
`cinematic.json` + scene images + audio + `style/<id>.json`. It is the video
renderer of the same timeline the web cinematic view plays, so they match.

Per chapter: each scene becomes a Ken-Burns `zoompan` clip; an `xfade` chain
(offset = `scene.start`, clip = segment + crossfade) keeps the video
**sync-locked** to the audio (trimmed to the audio length); then `vignette`,
the caption overlay, and the watermark; audio is voice + ambient (`amix`).

Captions and the watermark are pre-rendered with **Pillow** into a transparent
caption track (qtrle) and a PNG — so no libass/drawtext is required (works on a
minimal ffmpeg). Caption styling/timing come straight from the render spec and
`captions[]`. Missing scene art falls back to `default.jpg`, then a gradient.

Requirements: `ffmpeg` with `libx264, aac, zoompan, xfade, vignette, qtrle`; a
Pillow venv (`mise run setup`); `resvg` (`brew install resvg`) to rasterize the
brand watermark SVGs (`brand/logomark.svg` + `brand/wordmark.svg`, mirrored from
bifrost). Caption font: Space Grotesk (matches the web `--font-family-lead`),
falling back to a system sans.

```bash
mise run setup            # one-time: create ./venv with Pillow
mise run compose-preview  # 30s smoke test of chapter 1 -> ./out/
mise run compose          # all EN chapters of the default book -> ./out/
mise run compose-all-langs

# direct:
./venv/bin/python compose_video.py --book <slug> --lang en --chapters 1
./venv/bin/python compose_video.py --book <slug> --lang en --chapters 1 --preview 25
```

Output: `./out/<book>/c<n>.<lang>.mp4` (gitignored). Deterministic and faster
than realtime; batchable over chapters × languages.

## mise tasks

```bash
mise run timeline         # build EN timelines for the default book
mise run timeline-all     # build all languages
mise run setup            # create the compositor venv (Pillow)
mise run compose-preview  # 30s smoke test
mise run compose          # render all EN chapters to MP4
```
