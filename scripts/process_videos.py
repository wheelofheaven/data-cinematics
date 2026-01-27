#!/usr/bin/env python3
"""
Video Processing Pipeline for Wheel of Heaven

Converts source videos to WebM (AV1) format with optional cropping
and generates poster thumbnails in AVIF format.

Usage:
    python process_videos.py [--dry-run] [--force] [--verbose]
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml
from tqdm import tqdm

try:
    import pillow_avif  # noqa: F401 - registers AVIF plugin
    from PIL import Image
except ImportError:
    print("Error: Pillow and pillow-avif-plugin required")
    print("Run: pip install Pillow pillow-avif-plugin")
    sys.exit(1)


# Paths
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
RAW_DIR = PROJECT_DIR / "raw"
PROCESSED_DIR = PROJECT_DIR / "processed"
BACKUP_DIR = PROJECT_DIR / "backup"
MANIFEST_PATH = PROJECT_DIR / "manifest.yaml"
LOG_FILE = PROJECT_DIR / "video_processing.log"

# Quality presets (CRF values for SVT-AV1)
QUALITY_PRESETS = {
    "low": 45,      # Smaller file, lower quality
    "medium": 32,   # Balanced
    "high": 25,     # Larger file, higher quality
}

# SVT-AV1 preset (0-13, lower = slower but better compression)
ENCODER_PRESET = 6  # Good balance of speed and quality


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure logging to file and console."""
    logger = logging.getLogger("video_processor")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    # File handler
    fh = logging.FileHandler(LOG_FILE)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s"
    ))

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger


def check_ffmpeg() -> bool:
    """Check if FFmpeg is available with AV1 support."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders"],
            capture_output=True,
            text=True
        )
        if "libsvtav1" in result.stdout:
            return True
        elif "libaom-av1" in result.stdout:
            return True
        else:
            print("Error: No AV1 encoder found in FFmpeg")
            print("Install FFmpeg with SVT-AV1 or libaom-av1 support")
            return False
    except FileNotFoundError:
        print("Error: FFmpeg not found")
        print("Install FFmpeg: brew install ffmpeg")
        return False


def load_manifest() -> dict:
    """Load and validate manifest.yaml."""
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Manifest not found: {MANIFEST_PATH}")

    with open(MANIFEST_PATH, "r") as f:
        manifest = yaml.safe_load(f)

    if "videos" not in manifest:
        raise ValueError("Manifest missing 'videos' key")

    return manifest


def get_video_info(video_path: Path) -> dict:
    """Get video metadata using ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,codec_name",
        "-of", "json",
        str(video_path)
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {}

    import json
    data = json.loads(result.stdout)
    if "streams" in data and len(data["streams"]) > 0:
        stream = data["streams"][0]
        return {
            "width": int(stream.get("width", 0)),
            "height": int(stream.get("height", 0)),
            "duration": float(stream.get("duration", 0)),
            "codec": stream.get("codec_name", "unknown"),
        }
    return {}


def backup_original(video_path: Path, logger: logging.Logger) -> None:
    """Create backup of original video."""
    backup_path = BACKUP_DIR / video_path.name
    if not backup_path.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(video_path, backup_path)
        logger.debug(f"Backed up: {video_path.name}")


def process_video(
    config: dict,
    dry_run: bool = False,
    force: bool = False,
    logger: logging.Logger = None
) -> bool:
    """Process a single video according to its configuration."""

    filename = config.get("filename")
    if not filename:
        logger.error("Missing 'filename' in config")
        return False

    if not config.get("enabled", True):
        logger.info(f"Skipping (disabled): {filename}")
        return True

    # Paths
    input_path = RAW_DIR / filename
    output_name = config.get("output_name", input_path.stem)
    output_path = PROCESSED_DIR / f"{output_name}.webm"
    poster_path = PROCESSED_DIR / f"{output_name}-poster.avif"

    # Check input exists
    if not input_path.exists():
        logger.error(f"Source not found: {input_path}")
        return False

    # Check if already processed
    if output_path.exists() and not force:
        logger.info(f"Already processed (use --force to reprocess): {output_name}")
        return True

    # Get video info
    info = get_video_info(input_path)
    if not info:
        logger.error(f"Could not read video info: {filename}")
        return False

    logger.info(f"Processing: {filename}")
    logger.info(f"  Input: {info['width']}x{info['height']}, {info['duration']:.1f}s")

    # Build FFmpeg filter chain
    filters = []

    # Crop watermark from bottom
    crop_height = config.get("crop_height", 0)
    if config.get("crop_watermark", False) and crop_height > 0:
        new_height = info["height"] - crop_height
        filters.append(f"crop=iw:{new_height}:0:0")
        logger.info(f"  Cropping: {crop_height}px from bottom -> {info['width']}x{new_height}")

    # Quality settings
    quality = config.get("quality", "medium")
    crf = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["medium"])
    logger.info(f"  Quality: {quality} (CRF {crf})")

    if dry_run:
        logger.info(f"  [DRY RUN] Would create: {output_path.name}")
        logger.info(f"  [DRY RUN] Would create: {poster_path.name}")
        return True

    # Backup original
    backup_original(input_path, logger)

    # Ensure output directory exists
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    # Build FFmpeg command
    cmd = [
        "ffmpeg",
        "-y",  # Overwrite output
        "-i", str(input_path),
    ]

    # Add filter chain
    if filters:
        cmd.extend(["-vf", ",".join(filters)])

    # SVT-AV1 encoding settings
    cmd.extend([
        "-c:v", "libsvtav1",
        "-crf", str(crf),
        "-preset", str(ENCODER_PRESET),
        "-svtav1-params", "tune=0",  # Visual quality tuning
        "-pix_fmt", "yuv420p10le",   # 10-bit for better gradients
    ])

    # Audio settings (Opus)
    cmd.extend([
        "-c:a", "libopus",
        "-b:a", "128k",
    ])

    cmd.append(str(output_path))

    logger.info("  Encoding (this may take a while)...")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            logger.error(f"FFmpeg error: {result.stderr}")
            return False

        # Get output file size
        if output_path.exists():
            input_size = input_path.stat().st_size / (1024 * 1024)
            output_size = output_path.stat().st_size / (1024 * 1024)
            ratio = (1 - output_size / input_size) * 100
            logger.info(f"  Output: {output_size:.2f} MB ({ratio:.1f}% reduction)")

    except Exception as e:
        logger.error(f"Processing failed: {e}")
        return False

    # Generate poster thumbnail (first frame)
    logger.info("  Generating poster thumbnail...")
    try:
        poster_cmd = [
            "ffmpeg",
            "-y",
            "-i", str(input_path),
            "-vframes", "1",
        ]

        # Apply same crop to poster
        if filters:
            poster_cmd.extend(["-vf", ",".join(filters)])

        poster_cmd.extend([
            "-f", "image2",
            str(poster_path.with_suffix(".png"))  # Temp PNG
        ])

        subprocess.run(poster_cmd, capture_output=True)

        # Convert to AVIF
        temp_png = poster_path.with_suffix(".png")
        if temp_png.exists():
            img = Image.open(temp_png)
            img.save(poster_path, "AVIF", quality=80)
            temp_png.unlink()  # Remove temp PNG
            poster_size = poster_path.stat().st_size / 1024
            logger.info(f"  Poster: {poster_path.name} ({poster_size:.1f} KB)")

    except Exception as e:
        logger.warning(f"  Poster generation failed: {e}")

    logger.info(f"  Done: {output_path.name}")
    return True


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Process videos for Wheel of Heaven CDN"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without processing"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess existing files"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )

    args = parser.parse_args()
    logger = setup_logging(args.verbose)

    logger.info("=" * 60)
    logger.info("Video Processing Pipeline")
    logger.info(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # Check prerequisites
    if not check_ffmpeg():
        sys.exit(1)

    # Load manifest
    try:
        manifest = load_manifest()
    except Exception as e:
        logger.error(f"Failed to load manifest: {e}")
        sys.exit(1)

    videos = manifest.get("videos", [])
    logger.info(f"Found {len(videos)} video(s) in manifest")

    if args.dry_run:
        logger.info("[DRY RUN MODE - No files will be modified]")

    # Process each video
    success = 0
    failed = 0

    for config in tqdm(videos, desc="Processing", disable=args.verbose):
        if process_video(config, args.dry_run, args.force, logger):
            success += 1
        else:
            failed += 1

    logger.info("=" * 60)
    logger.info(f"Completed: {success} success, {failed} failed")
    logger.info("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
