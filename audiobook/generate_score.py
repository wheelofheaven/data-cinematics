#!/usr/bin/env python3
"""Generate a subtle musical score bed for the cinematic VIDEO export.

Video-export ONLY — this bed is never added to the web audiobook player
(which stays narration + ambient SFX). compose_video.py loops the result
under voice + ambient at a low gain (style.score.gain) with fades.

Primary path: the ElevenLabs Music API (/v1/music) — real composed,
minimal cinematic underscore. Falls back to /v1/sound-generation
ambient-pad segments concatenated with crossfades if the Music API is
unavailable on this account. Output is a seamless-ish loop written to
score/<name>.opus.

Reads ELEVENLABS_API_KEY from data-library/.env or the environment.

Usage:
    python3 generate_score.py --name woh-underscore --length 120
    python3 generate_score.py --name woh-underscore --force   # ignore cache
"""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("Install requests: pip install requests", file=sys.stderr)
    sys.exit(1)

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SCORE_DIR = HERE / "score"
CACHE = HERE / ".score_cache"
ENV = REPO / "data-library" / ".env"
API = "https://api.elevenlabs.io/v1"

# A reverent, cosmic, awe-tinged underscore that matches the contact theme.
# Deliberately minimal so the narration always carries.
MUSIC_PROMPT = (
    "Minimal cinematic ambient underscore for a reverent science-fiction "
    "audiobook about humanity's creators returning from the stars. Slow, "
    "weightless, in D minor: a warm sustained synth pad, faint distant "
    "ethereal choir, occasional soft low strings and a single glassy "
    "high tone that drifts. Spacious reverb, sense of awe and quiet wonder. "
    "No drums, no percussion, no rhythm, no obvious melody hook, no vocals. "
    "Steady dynamics suitable to sit far beneath a spoken voice. Seamless, "
    "loopable texture."
)

# Fallback (sound-generation) — three same-key pad variations, concatenated.
PAD_PROMPTS = [
    "slow cinematic ambient pad in D minor, warm sustained synth, distant "
    "ethereal choir, reverent and cosmic, no drums, no percussion, no melody "
    "hook, seamless looping texture",
    "evolving ambient drone in D minor, soft glassy synth shimmer, awe and "
    "wonder, weightless, no rhythm, no vocals, seamless ambient bed",
    "deep warm pad in D minor with subtle low strings, spacious reverb, "
    "contemplative, no drums, seamless looping cinematic underscore",
]


def load_api_key() -> str:
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("ELEVENLABS_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    import os
    k = os.environ.get("ELEVENLABS_API_KEY")
    if k:
        return k
    print("No ELEVENLABS_API_KEY in data-library/.env or environment", file=sys.stderr)
    sys.exit(1)


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def music_api(prompt: str, length_ms: int, key: str) -> bytes | None:
    """Try the ElevenLabs Music API. Return mp3 bytes, or None if unavailable."""
    url = f"{API}/music"
    headers = {"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"}
    body = {"prompt": prompt, "music_length_ms": length_ms}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=300)
    except requests.RequestException as e:
        print(f"  music API request failed: {e}", file=sys.stderr)
        return None
    if r.status_code == 200 and r.content[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3"):
        return r.content
    print(f"  music API unavailable ({r.status_code}): {r.text[:200]}", file=sys.stderr)
    return None


def sound_gen(prompt: str, seconds: float, key: str) -> bytes:
    url = f"{API}/sound-generation"
    headers = {"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"}
    r = requests.post(url, headers=headers, json={"text": prompt, "duration_seconds": seconds}, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"sound-generation {r.status_code}: {r.text[:200]}")
    return r.content


def fallback_bed(key: str, tmp: Path, seg_seconds: float, xf: float) -> Path:
    """Concatenate pad variations with crossfades into one wav."""
    wavs = []
    for i, p in enumerate(PAD_PROMPTS):
        mp3 = tmp / f"seg{i}.mp3"
        mp3.write_bytes(sound_gen(p, seg_seconds, key))
        wav = tmp / f"seg{i}.wav"
        run(["ffmpeg", "-y", "-i", str(mp3), "-ar", "48000", "-ac", "2", str(wav)])
        wavs.append(wav)
    out = tmp / "bed.wav"
    if len(wavs) == 1:
        return wavs[0]
    inputs = []
    for w in wavs:
        inputs += ["-i", str(w)]
    # chain acrossfade across all segments
    fc, prev = [], "0:a"
    for i in range(1, len(wavs)):
        lbl = "bed" if i == len(wavs) - 1 else f"x{i}"
        fc.append(f"[{prev}][{i}:a]acrossfade=d={xf}:c1=tri:c2=tri[{lbl}]")
        prev = lbl
    run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(fc), "-map", "[bed]", str(out)])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="woh-underscore", help="output basename (score/<name>.opus)")
    ap.add_argument("--length", type=float, default=120.0, help="target seconds (music API)")
    ap.add_argument("--force", action="store_true", help="ignore cache")
    ap.add_argument("--bitrate", default="96k")
    args = ap.parse_args()

    key = load_api_key()
    SCORE_DIR.mkdir(exist_ok=True)
    CACHE.mkdir(exist_ok=True)
    out = SCORE_DIR / f"{args.name}.opus"

    sig = hashlib.sha256(f"{MUSIC_PROMPT}|{args.length}".encode()).hexdigest()[:16]
    raw = CACHE / f"{sig}.mp3"

    if raw.exists() and not args.force:
        print(f"using cached {raw.name}")
        mode = "cache"
    else:
        print("requesting Music API …")
        data = music_api(MUSIC_PROMPT, int(args.length * 1000), key)
        mode = "music"
        if data is None:
            print("falling back to sound-generation pad bed …")
            import tempfile
            tmp = Path(tempfile.mkdtemp(prefix="woh_score_"))
            bed = fallback_bed(key, tmp, seg_seconds=22.0, xf=3.0)
            run(["ffmpeg", "-y", "-i", str(bed), "-c:a", "libmp3lame", "-q:a", "4", str(raw)])
            mode = "sound-gen"
        else:
            raw.write_bytes(data)
        (CACHE / f"{sig}.prompt.txt").write_text(MUSIC_PROMPT + f"\n\nlength={args.length}\nmode={mode}\n")

    run(["ffmpeg", "-y", "-i", str(raw), "-c:a", "libopus", "-b:a", args.bitrate,
         "-ac", "2", str(out)])
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nk=1:nw=1", str(out)],
                         capture_output=True, text=True).stdout.strip()
    print(f"[{mode}] wrote {out}  ({dur}s)")


if __name__ == "__main__":
    main()
